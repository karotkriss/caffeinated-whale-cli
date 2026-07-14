"""``core.status`` - real per-process health for a bench, one-shot, on the core.

Replaces the old blind ``curl localhost:8000`` (three flat tokens) with honest
per-process health read from the shared supervision substrate: the live ``ps``
discovery (up/uptime/CPU/RSS), the web HTTP probe kept as one field, the
supervisor marker, and a PRE-COMPUTED ``overall`` aggregate so no frontend
re-derives it (openspec ``migrate-start-status-core`` D3).

The ``overall`` aggregate distinguishes the four lifecycle states:

===========  ===========================================================
``overall``  condition
===========  ===========================================================
offline      a real-but-stopped project: containers exist, frappe not running
online       container up, no supervisor marker (bench never started)
running      marker present, supervisord up, every expected program healthy
             (RUNNING/STARTING), and the web probe answers
degraded     marker present and supervisord down (started, supervisor died),
             OR up but a program is not healthy (BACKOFF/FATAL/EXITED/STOPPED
             /down), OR the web probe is not answering
===========  ===========================================================

Unlike the honcho model this replaced, ``running`` now requires every expected
program to be healthy, because supervisord keeps siblings alive when one dies -
so "web serving while a worker is FATAL" is a real, STABLE partial stack that
must report ``degraded``, honestly (honcho made this impossible: one death tore
the whole stack down, so supervisor-up implied the stack was up). Per-program
state (RUNNING/STARTING/BACKOFF/EXITED/FATAL/STOPPED) comes from supervisord
itself (``supervisorctl status``), which distinguishes a crash-looping BACKOFF
and a give-up FATAL from a clean down - detail a ``ps``-only view cannot. The
crash-loop/FATAL detail rides in each process's ``state`` field; ``overall``
keeps its four tokens (a FATAL program folds into ``degraded``, no fifth token).

With ``probe_web=False`` (the ``status --watch`` loop, so repeated ticks never
hit the bench's web server) the web probe is skipped entirely; the per-program
health still comes from ``ps`` + ``supervisorctl`` (neither touches :8000), so a
missing web code must not by itself ``degrade`` the aggregate.

A real-but-stopped project (containers exist but frappe is not running) is
``offline`` and is RETURNED (never raised), preserving today's "offline, exit 0"
contract. A truly-nonexistent project (no containers with the label at all, or no
frappe service among them) instead RAISES a ``NOT_FOUND`` :class:`CwcliError`, so
a frontend can distinguish a typo/never-created name (non-zero exit) from a
stopped instance. Only an unreachable Docker daemon raises ``DOCKER``. No
print/prompt/``typer.Exit``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from docker.errors import APIError, NotFound

from ..utils.docker_utils import get_project_containers
from . import resolvers, supervision
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .supervision import ProcessHealth

OFFLINE = "offline"
ONLINE = "online"
RUNNING = "running"
DEGRADED = "degraded"


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusReport:
    """A one-shot health snapshot for a project's resolved bench (serializable).

    ``overall`` is the FIRST field so ``dataclasses.asdict`` -> the TOON serializer
    emits the pre-computed aggregate up front (the ``cwcli axi status`` contract).
    """

    overall: str
    project: str
    container_running: bool
    supervisor_up: bool
    web_http_code: str | None
    processes: list[ProcessHealth]


def _offline(project_name: str) -> Result[StatusReport]:
    return Result(
        status=Status.OK,
        data=StatusReport(
            project=project_name,
            overall=OFFLINE,
            container_running=False,
            supervisor_up=False,
            web_http_code=None,
            processes=[],
        ),
    )


def status(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    probe_web: bool = True,
) -> Result[StatusReport]:
    """Report a project's bench health. See module docstring.

    ``probe_web=False`` suppresses the in-container ``curl localhost:8000`` web
    probe entirely (``web_http_code`` comes back ``None``) so a repeated caller -
    the ``status --watch`` loop - leaves ZERO HTTP requests in the bench's access
    logs. Per-program liveness + state still come from the ``ps`` read and
    ``supervisorctl`` (neither touches :8000), so ``overall`` stays honest: with no
    web signal it is driven by supervisor-up + every program healthy.
    """
    warnings: list[Message] = []

    containers = get_project_containers(project_name)
    if containers is None:
        raise CwcliError(
            ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker daemon."
        )
    # A truly-nonexistent project (typo / never created) has NO containers with the
    # label at all - distinct from a real-but-stopped project, which has a non-empty
    # container list. Raise NOT_FOUND so a frontend can exit non-zero and say so,
    # while the stopped case below still returns "offline"/exit 0 (the documented
    # contract).
    if not containers:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "project.not_found", f"No such project '{project_name}'."
        )

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "project.no_frappe_service",
            f"No 'frappe' service found for project '{project_name}'.",
        )

    try:
        frappe_container.reload()
    except (APIError, NotFound):
        return _offline(project_name)
    if frappe_container.status != "running":
        return _offline(project_name)

    # Resolve which bench to report - the SAME selector as core.start, so the two
    # verbs always agree on the bench.
    resolved = resolvers.resolve_bench(project_name, bench, bench_path)
    if resolved is None:
        resolved_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif resolved.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=resolved.choice)
    else:
        assert resolved.data is not None
        resolved_path = resolved.data
        warnings.extend(resolved.warnings)

    marker = supervision.read_marker(frappe_container, resolved_path)
    snapshot = supervision.discover_stack(frappe_container, resolved_path)
    expected = supervision.expected_labels(frappe_container, resolved_path)
    # supervisord's authoritative per-program state (RUNNING/BACKOFF/FATAL/...),
    # read over the unix control socket - it never touches the bench web server, so
    # it is safe even in the quiet ``--watch`` loop. Only meaningful when up.
    states = (
        supervision.states_by_label(
            supervision.supervisorctl_states(frappe_container, resolved_path)
        )
        if snapshot.supervisor_up
        else {}
    )
    web_code = supervision.web_http_code(frappe_container) if probe_web else None

    processes = _merge_health(expected, snapshot.processes, states)
    overall = _overall(
        started=marker is not None,
        supervisor_up=snapshot.supervisor_up,
        all_healthy=_all_healthy(processes),
        web_code=web_code,
        web_probed=probe_web,
    )

    return Result(
        status=Status.OK,
        data=StatusReport(
            project=project_name,
            overall=overall,
            container_running=True,
            supervisor_up=snapshot.supervisor_up,
            web_http_code=web_code,
            processes=processes,
        ),
        warnings=warnings,
    )


# supervisord states that count as "not a stable failure": RUNNING is up,
# STARTING is a program still coming up (a one-shot status shouldn't degrade over
# a transient). Everything else (BACKOFF/EXITED/FATAL/STOPPED) is a real down.
_HEALTHY_STATES = {"RUNNING", "STARTING"}


def _merge_health(
    expected: list[str],
    discovered: list[ProcessHealth],
    states: dict[str, tuple[str, int | None]],
) -> list[ProcessHealth]:
    """Every expected label (down if not discovered) plus any extra live process.

    Each process is annotated with supervisord's authoritative ``state`` (from
    ``supervisorctl status``); a program supervisord knows about but ``ps`` did not
    catch (e.g. a FATAL crash-loop with no live process) is reported down WITH its
    ``state``, so the failure is visible rather than a bare "down".
    """
    by_label = {p.label: p for p in discovered}
    out: list[ProcessHealth] = []
    seen: set[str] = set()

    def _annotate(p: ProcessHealth) -> ProcessHealth:
        state = states.get(p.label, (None, None))[0]
        return replace(p, state=state) if state is not None else p

    for label in expected:
        if label in seen:
            continue
        seen.add(label)
        p = by_label.get(label)
        if p is not None:
            out.append(_annotate(p))
        else:
            state = states.get(label, (None, None))[0]
            out.append(ProcessHealth(label=label, up=False, state=state))
    for p in discovered:
        if p.label not in seen:
            seen.add(p.label)
            out.append(_annotate(p))
    return out


def _all_healthy(processes: list[ProcessHealth]) -> bool:
    """True iff every process is healthy (RUNNING/STARTING by state, else ``up``)."""
    for p in processes:
        if p.state is not None:
            if p.state not in _HEALTHY_STATES:
                return False
        elif not p.up:
            return False
    return True


def _overall(
    *,
    started: bool,
    supervisor_up: bool,
    all_healthy: bool,
    web_code: str | None,
    web_probed: bool = True,
) -> str:
    """The pre-computed lifecycle aggregate (see module docstring's table).

    Unlike the honcho model, a stable partial stack (``all_healthy`` False - a
    program is BACKOFF/FATAL/EXITED/STOPPED/down while supervisord keeps the rest
    alive) is a genuine ``degraded``. When ``web_probed`` is False (watch mode
    suppressed the web probe), a missing web code alone must NOT degrade the
    aggregate - only supervisor-down or an unhealthy program does.
    """
    if not started:
        return ONLINE
    if not supervisor_up:
        return DEGRADED
    if not all_healthy:
        return DEGRADED
    if not web_probed:
        return RUNNING
    web_ok = web_code not in (None, "000")
    return RUNNING if web_ok else DEGRADED
