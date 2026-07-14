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
running      marker present, honcho up, and the web probe answers
degraded     marker present and honcho down (started, supervisor died),
             OR honcho up but the web probe is not answering
===========  ===========================================================

``running`` keys on honcho-up + the web probe answering rather than requiring
every Procfile label to be individually detected as up, because honcho is
all-or-nothing (one process dies, it tears the rest down and never restarts one),
so honcho-up already implies the stack is up; keying on the reliable web signal
keeps ``overall`` robust across frappe versions' cmdline shapes while the
per-process list still reports each label's liveness for detail.

A real-but-stopped project (containers exist but frappe is not running) is
``offline`` and is RETURNED (never raised), preserving today's "offline, exit 0"
contract. A truly-nonexistent project (no containers with the label at all, or no
frappe service among them) instead RAISES a ``NOT_FOUND`` :class:`CwcliError`, so
a frontend can distinguish a typo/never-created name (non-zero exit) from a
stopped instance. Only an unreachable Docker daemon raises ``DOCKER``. No
print/prompt/``typer.Exit``.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    logs. Per-process liveness still comes from the single ``ps`` read, so
    ``overall`` stays honest: with no web signal it is driven by honcho-up alone
    (honcho is all-or-nothing, so honcho-up already implies the stack is up).
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
    web_code = supervision.web_http_code(frappe_container) if probe_web else None

    processes = _merge_health(expected, snapshot.processes)
    overall = _overall(
        started=marker is not None,
        supervisor_up=snapshot.supervisor_up,
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


def _merge_health(expected: list[str], discovered: list[ProcessHealth]) -> list[ProcessHealth]:
    """Every expected label (down if not discovered) plus any extra live process."""
    by_label = {p.label: p for p in discovered}
    out: list[ProcessHealth] = []
    seen: set[str] = set()
    for label in expected:
        if label in seen:
            continue
        seen.add(label)
        out.append(by_label.get(label) or ProcessHealth(label=label, up=False))
    for p in discovered:
        if p.label not in seen:
            seen.add(p.label)
            out.append(p)
    return out


def _overall(
    *, started: bool, supervisor_up: bool, web_code: str | None, web_probed: bool = True
) -> str:
    """The pre-computed lifecycle aggregate (see module docstring's table).

    When ``web_probed`` is False (watch mode suppressed the web probe), honcho-up
    alone decides ``running`` vs ``degraded`` - a missing web code must NOT falsely
    degrade the aggregate, since honcho-up already implies the stack is up.
    """
    if not started:
        return ONLINE
    if not supervisor_up:
        return DEGRADED
    if not web_probed:
        return RUNNING
    web_ok = web_code not in (None, "000")
    return RUNNING if web_ok else DEGRADED
