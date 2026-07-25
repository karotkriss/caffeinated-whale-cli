"""``core.status`` - real per-bench, per-process health for an instance, one-shot.

Replaces the old blind ``curl localhost:8000`` (three flat tokens) with honest
per-process health read from the shared supervision substrate: the live ``ps``
discovery (up/uptime/CPU/RSS), the web HTTP probe kept as one field, the
supervisor marker, and a PRE-COMPUTED ``overall`` aggregate so no frontend
re-derives it (openspec ``migrate-start-status-core`` D3).

One instance is ONE report carrying ``benches: list[BenchStatus]``, the shape
``InspectReport.benches`` already uses, uniform whether the instance holds one bench
or six (openspec ``report-status-per-bench``). ``--bench``/``--path`` narrows that
list to a single entry; the bare form reports every cached bench instead of the old
``NEEDS_CHOICE`` refusal, so ``status`` NEVER returns ``NEEDS_CHOICE`` on any path.

**The web probe is per bench and its port is never guessed.** Benches are siblings
under ``/workspace`` sharing one container, each serving the port bench's own
``make_ports`` assigned it (its ``sites/common_site_config.json``, which cwcli reads
but never writes). The probe used to hardcode ``localhost:8000``, so on any bench
past the first it measured a DIFFERENT bench's web server: a fully healthy bench 1
reported ``degraded`` (its port was never asked), and a bench 1 serving nothing
reported bench 0's live HTTP code. Each bench's port now comes from
``resolvers.resolve_assigned_ports(..., fill_defaults=False)`` and is passed
explicitly to :func:`supervision.web_http_code`; a bench whose port cannot be
resolved reports ``web_port=None``/``web_port_verified=False``, is NOT probed, and
does NOT degrade on that account (see ``_overall``'s ``web_probed``) - a gap in
cwcli's knowledge is not a fault in the bench, and falling back to 8000 IS the bug.

**The probe also names the bench's site**, as the ``Host`` header. Frappe routes by
Host, so a host-less request names no site and is correctly answered 404 - a healthy
bench used to report ``web :8000 -> 404`` on every read. The aggregate was never
wrong (any code counts as serving), but a health number whose normal value is an
error code trains its reader to ignore the field, and that habit costs a real
``degraded`` its audience. The site comes from
``resolvers.resolve_representative_site`` and is REPORTED as ``web_site``, so the
code stays attributable; a bench with no site at all probes host-less, as before.

The per-bench ``overall`` distinguishes the four lifecycle states:

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

When NO cwcli supervisord manages the bench, ``status`` does NOT blindly report
all-down: it falls back to :func:`supervision.discover_unsupervised_stack`, which
detects a bench still running under honcho / ``bench start`` (how every pre-v3
instance, or a plain ``bench start``, looks) and reports each process's TRUE
``up``/pid/uptime from ``ps``. Such a report is flagged ``not_cwcli_supervised``
(a running-but-not-yet-migrated state, NOT ``offline``) with an actionable hint
to run ``cwcli start``, and ``overall`` reflects the real process state
(``running``/``degraded``). Per-process supervisord ``state`` is unavailable here
(``None``) - correct, since supervisord is not what manages these. The fallback
is a pure READ: it never launches supervisord (migrating is ``cwcli start``'s job).

**Each reported bench's path is cross-checked against the container.** The bench
list is read from the cache, which outlives the benches it describes, and no
amount of live health probing can tell a DELETED bench from one that was never
started - both have no marker and no supervisord, so a removed bench reported
``online``. ``resolvers.present_bench_paths`` answers it in ONE exec for the whole
list, so the cost is flat in bench count and adds exactly one round trip per
report, on the fused tier too; the answer lands on each row as ``bench_present``
(``present``/``absent``/``unverified``) rather than folding into ``overall``.

A real-but-stopped project (containers exist but frappe is not running) is
``offline`` with an EMPTY ``benches`` list and is RETURNED (never raised),
preserving today's "offline, exit 0" contract. A truly-nonexistent project (no
containers with the label at all, or no frappe service among them) instead RAISES a
``NOT_FOUND`` :class:`CwcliError`, so a frontend can distinguish a typo/never-created
name (non-zero exit) from a stopped instance. Only an unreachable Docker daemon
raises ``DOCKER``. No print/prompt/``typer.Exit``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from docker.errors import APIError, NotFound

from . import resolvers, supervision
from .docker import get_project_containers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .supervision import ProcessHealth

OFFLINE = "offline"
ONLINE = "online"
RUNNING = "running"
DEGRADED = "degraded"


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchStatus:
    """One bench's health inside an instance report (serializable, no live object).

    ``index`` is the bench's durable numeric identity - the same number ``--bench
    <index>`` takes and ``cwcli axi benches`` reports, so an index means the same
    thing everywhere and across later bench additions. It is None for a bench that
    is not in that list (a ``--path`` override, or the synthetic default on a
    never-inspected project):
    reporting 0 there would name a bench ``--bench 0`` resolves somewhere else, which
    is the attributed-lie class this change exists to remove.

    ``web_port`` is the CONTAINER-side port this bench serves, read from its own
    ``sites/common_site_config.json``, and it is the port ``web_http_code`` was
    actually measured on. ``web_port_verified`` is False when that read failed: the
    port is then None, no probe was made, ``web_http_code`` is None, and a
    ``status.web_port_unknown`` warning names the bench. The honest value/verified
    pair ``scale.BenchPortMap`` already uses - never a guessed 8000 reported as fact.

    ``web_site`` is the site the probe named in its ``Host`` header, and it makes
    ``web_http_code`` ATTRIBUTABLE the same way ``web_port`` does. Frappe routes by
    Host, so the code answered is the code FOR THAT SITE; a bench with several
    undefaulted sites has one picked for it (see
    ``resolvers.resolve_representative_site``), and reporting which one is what keeps
    that a disclosed pick rather than a silent claim about the bench as a whole. None
    means no site could be resolved and the probe named none.

    ``bench_present`` says whether the bench directory this row is ABOUT still
    exists: ``present`` | ``absent`` | ``unverified``. ``overall`` cannot answer
    that and must not be asked to - a deleted bench has no marker and no
    supervisord, which is indistinguishable from a bench that simply was never
    started, so it reported ``online`` and read as "here, just not up". The
    remembered path was the unverified claim, not the health; the token sits next
    to the health rather than folding into it, which is why ``overall`` keeps its
    four tokens and gains no fifth.
    """

    index: int | None
    bench_path: str
    label: str | None
    overall: str
    #: ``present`` | ``absent`` | ``unverified`` - see the class docstring.
    bench_present: str = resolvers.BENCH_UNVERIFIED
    supervisor_up: bool
    web_port: int | None
    web_port_verified: bool
    web_site: str | None
    web_http_code: str | None
    processes: list[ProcessHealth]
    # True when the bench is running under honcho / ``bench start`` rather than
    # cwcli's supervisord (pre-v3, or a plain ``bench start``): the processes are
    # genuinely up but not yet under cwcli supervision. ``supervisor_up`` (which
    # means *cwcli's* supervisord) stays False in this state.
    not_cwcli_supervised: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusReport:
    """A one-shot health snapshot for a project INSTANCE (serializable).

    ``overall`` is the FIRST field so ``dataclasses.asdict`` -> the TOON serializer
    emits the pre-computed aggregate up front (the ``cwcli axi status`` contract).
    It is an instance-level fold over the per-bench aggregates (see ``_fold``).

    ``benches`` always carries the list form, even for one bench, so there is exactly
    one document shape and one parse path (the ``InspectReport.benches`` model). A
    stopped instance carries an EMPTY list.
    """

    overall: str
    project: str
    container_running: bool
    benches: list[BenchStatus]


# The hint surfaced when the bench runs under honcho / ``bench start`` (not cwcli).
NOT_CWCLI_SUPERVISED_HINT = (
    "running under honcho / not under cwcli supervision - run `cwcli start` to "
    "bring it under cwcli's supervisor."
)


def _offline(project_name: str) -> Result[StatusReport]:
    """A stopped instance: ``offline`` and an EMPTY bench list.

    Populating the list from the cache with every bench marked down was considered
    and rejected: ``overall: offline`` plus ``container_running: false`` is already a
    complete answer to "how is this instance", and manufacturing per-bench rows
    nothing was probed for adds claim-shaped structure backed by no observation.
    ``inspect`` is the verb for "what benches does this instance have".
    """
    return Result(
        status=Status.OK,
        data=StatusReport(
            project=project_name,
            overall=OFFLINE,
            container_running=False,
            benches=[],
        ),
    )


def status(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    probe_web: bool = True,
    fused: bool = False,
) -> Result[StatusReport]:
    """Report a project's per-bench health. See module docstring.

    With no ``bench``/``bench_path`` selector this reports EVERY cached bench; with
    one it reports exactly that bench. Either way the shape is the same and it never
    returns ``NEEDS_CHOICE`` - the multi-bench refusal is gone.

    ``probe_web=False`` suppresses the in-container ``curl`` web probe entirely
    (every bench's ``web_http_code`` comes back ``None``) so a repeated caller - the
    ``status --watch`` loop - leaves ZERO HTTP requests in the bench's access logs.
    Per-program liveness + state still come from the ``ps`` read and
    ``supervisorctl`` (neither touches the web port), so ``overall`` stays honest:
    with no web signal it is driven by supervisor-up + every program healthy.

    ``fused=True`` reads each SUPERVISED bench through
    :func:`supervision.fused_probe` - ONE ``docker exec`` instead of the default
    path's five - for a repeating caller that cannot afford the round trips
    (``cwcli serve``'s FAST tier polls every RUNNING instance every few seconds;
    measured 758ms -> 252ms per instance). It is an exec-budget choice ONLY: the
    returned :class:`StatusReport` is the same shape with the same tokens, and an
    UNSUPERVISED bench falls back to the full read per bench, so the never-started
    vs supervisor-died distinction and the honcho fallback are never traded away.
    Defaults False, so every existing caller is byte-unchanged.
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

    # Which benches to report. `--bench`/`--path` narrows to one (the SAME selector
    # core.start uses, so the two verbs always agree); the bare form reports every
    # cached bench - the enumeration the old NEEDS_CHOICE refusal used to send the
    # caller away to reconstruct by hand.
    targets, target_warnings = _targets(project_name, bench, bench_path)
    warnings.extend(target_warnings)

    # Each bench's own serving port, read from its own config. fill_defaults=False:
    # an omitted key is UNRESOLVED here, never Frappe's 8000 - this answer becomes a
    # probe target, and a guessed 8000 measures whichever bench happens to serve it.
    assigned = resolvers.resolve_assigned_ports(
        frappe_container, [path for _, path, _ in targets], fill_defaults=False
    )

    # Does each of those remembered paths still exist? ONE exec for the whole list,
    # so the cost is flat in bench count on every tier including the fused one. It
    # answers the question no amount of live health probing can: a deleted bench and
    # a never-started bench look identical to the marker and to supervisord.
    present = resolvers.present_bench_paths(frappe_container, [path for _, path, _ in targets])

    benches: list[BenchStatus] = []
    for index, path, label in targets:
        ports = assigned.get(path)
        web_port = ports[0] if ports is not None else None
        if web_port is None:
            warnings.append(
                Message(
                    "status.web_port_unknown",
                    f"Could not read the web port for bench {path}; its web server was "
                    f"not probed. Run 'cwcli inspect {project_name}' to refresh.",
                )
            )
        read = _bench_status_fused if fused else _bench_status
        benches.append(
            replace(
                read(
                    frappe_container,
                    index=index,
                    bench_path=path,
                    label=label,
                    web_port=web_port,
                    web_site=resolvers.resolve_representative_site(project_name, path),
                    probe_web=probe_web,
                    warnings=warnings,
                ),
                bench_present=_present_state(present, path),
            )
        )

    stale = [b.bench_path for b in benches if b.bench_present == resolvers.BENCH_ABSENT]
    if stale:
        warnings.append(
            Message(
                "status.stale_benches",
                f"{len(stale)} bench(es) reported here no longer exist: {', '.join(stale)}. "
                f"Run 'cwcli inspect {project_name} --update' to refresh the cache.",
                detail={"benches": stale},
            )
        )

    return Result(
        status=Status.OK,
        data=StatusReport(
            project=project_name,
            overall=_fold(benches),
            container_running=True,
            benches=benches,
        ),
        warnings=warnings,
    )


def _present_state(present: set[str] | None, bench_path: str) -> str:
    """One path's existence token. ``None`` (unaskable) is never a confirmation."""
    if present is None:
        return resolvers.BENCH_UNVERIFIED
    return resolvers.BENCH_PRESENT if bench_path in present else resolvers.BENCH_ABSENT


def _targets(
    project_name: str, bench: str | None, bench_path: str | None
) -> tuple[list[tuple[int | None, str, str | None]], list[Message]]:
    """The ``(index, bench_path, label)`` benches to report, plus resolver warnings.

    A selector narrows to one entry; without one, every cached bench is reported in
    durable identity order, while each tuple carries its durable numeric identity.

    That list is REMEMBERED, so the caller cross-checks it (see ``status``'s
    ``present_bench_paths`` call and ``BenchStatus.bench_present``). This used to
    carry no verification token on the reasoning that each bench's health is read
    LIVE, so a stale path would self-correct into a visible no-processes bench.
    It does not: a deleted bench has no marker and no supervisord, which is
    exactly what a bench that was never started looks like, so it reported
    ``online`` - "here, just not up" - about a directory that was gone.

    ``resolve_bench``'s errors and warnings are unchanged; only the multi-bench
    ``NEEDS_CHOICE`` is now impossible, because the bare form is answered instead of
    refused. A project with nothing cached keeps today's single synthetic entry at
    ``DEFAULT_BENCH_PATH`` with its ``bench.default_used`` warning.
    """
    warnings: list[Message] = []
    cached = resolvers.cached_benches(project_name)

    if bench is None and bench_path is None:
        if not cached:
            warnings.append(
                Message(
                    "bench.default_used",
                    f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
                )
            )
            return [(None, resolvers.DEFAULT_BENCH_PATH, None)], warnings
        return [
            (b.get("index", position), b["path"], b.get("label"))
            for position, b in enumerate(cached)
        ], warnings

    # A selector: resolve_bench raises USAGE on --bench + --path together and
    # NOT_FOUND on an unknown selector, exactly as before. It can no longer return
    # NEEDS_CHOICE here, since that only happens with no selector at all.
    resolved = resolvers.resolve_bench(project_name, bench, bench_path)
    if resolved is None:
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
        return [(None, resolvers.DEFAULT_BENCH_PATH, None)], warnings
    assert resolved.data is not None
    path = resolved.data
    warnings.extend(resolved.warnings)
    index = next(
        (b.get("index", position) for position, b in enumerate(cached) if b["path"] == path),
        None,
    )
    label = next((b.get("label") for b in cached if b["path"] == path), None)
    return [(index, path, label)], warnings


def _bench_status(
    frappe_container,
    *,
    index: int | None,
    bench_path: str,
    label: str | None,
    web_port: int | None,
    web_site: str | None,
    probe_web: bool,
    warnings: list[Message],
) -> BenchStatus:
    """One bench's live health, probed on ITS OWN port (or not probed at all).

    The probe is skipped both when the caller suppressed it (``--watch``) and when
    the port could not be resolved, and ``_overall``'s ``web_probed`` covers both:
    with no web signal the aggregate is driven by supervisor-up + every program
    healthy. Degrading on an unreadable config would manufacture a fresh instance of
    the very defect being fixed - a bench with every program RUNNING reported broken
    because cwcli could not read a JSON file.
    """
    probed = probe_web and web_port is not None
    marker = supervision.read_marker(frappe_container, bench_path)
    snapshot = supervision.discover_stack(frappe_container, bench_path)
    expected = supervision.expected_labels(frappe_container, bench_path)
    web_code = (
        supervision.web_http_code(frappe_container, port=web_port, site=web_site)
        if probed and web_port is not None
        else None
    )
    not_cwcli_supervised = False

    if snapshot.supervisor_up:
        # cwcli's supervisord manages this bench (the v3 path): use supervisord's
        # authoritative per-program state (RUNNING/BACKOFF/FATAL/...), read over the
        # unix control socket - it never touches the bench web server, so it is safe
        # even in the quiet ``--watch`` loop.
        states = supervision.states_by_label(
            supervision.supervisorctl_states(frappe_container, bench_path)
        )
        processes = _merge_health(expected, snapshot.processes, states)
        overall = _overall(
            started=marker is not None,
            supervisor_up=True,
            all_healthy=_all_healthy(processes),
            web_code=web_code,
            web_probed=probed,
        )
    else:
        # No cwcli supervisord for this bench. Before reporting all-down, fall back
        # to detecting whatever DOES run the Procfile (honcho / ``bench start`` - how
        # every pre-v3 instance looks). A READ-ONLY probe: it reports the true state
        # but never launches supervisord (that stays ``cwcli start``'s job).
        fallback = supervision.discover_unsupervised_stack(frappe_container, bench_path)
        if fallback.manager_up:
            not_cwcli_supervised = True
            processes = _merge_health(expected, fallback.processes, {})
            # A manager is alive, so drive ``overall`` off the real process state
            # (there is no supervisord ``state``; ``up`` comes from ``ps``).
            overall = _overall(
                started=True,
                supervisor_up=True,
                all_healthy=_all_healthy(processes),
                web_code=web_code,
                web_probed=probed,
            )
            # One hint per report, not per bench: the text is generic, so N benches
            # under honcho would otherwise repeat it verbatim N times.
            if not any(w.code == "supervisor.not_cwcli" for w in warnings):
                warnings.append(Message("supervisor.not_cwcli", NOT_CWCLI_SUPERVISED_HINT))
        else:
            # Genuinely not running under any manager: keep the marker-based
            # never-started (online) vs supervisor-died (degraded) distinction.
            processes = _merge_health(expected, [], {})
            overall = _overall(
                started=marker is not None,
                supervisor_up=False,
                all_healthy=_all_healthy(processes),
                web_code=web_code,
                web_probed=probed,
            )

    return BenchStatus(
        index=index,
        bench_path=bench_path,
        label=label,
        overall=overall,
        supervisor_up=snapshot.supervisor_up,
        web_port=web_port,
        web_port_verified=web_port is not None,
        web_site=web_site if probed else None,
        web_http_code=web_code,
        processes=processes,
        not_cwcli_supervised=not_cwcli_supervised,
    )


def _bench_status_fused(
    frappe_container,
    *,
    index: int | None,
    bench_path: str,
    label: str | None,
    web_port: int | None,
    web_site: str | None,
    probe_web: bool,
    warnings: list[Message],
) -> BenchStatus:
    """:func:`_bench_status`'s one-exec twin for a repeating caller (``fused=True``).

    ``supervisorctl status`` enumerates every program supervisord manages, so a
    FATAL crash-loop with no live process is still reported down WITH its state -
    the completeness ``_merge_health`` buys from the extra ``Procfile`` read on the
    default path. That is why this path needs neither ``expected_labels`` nor
    ``_merge_health``.

    An UNSUPERVISED bench DELEGATES to the full :func:`_bench_status`. The marker
    read (never-started ``online`` vs supervisor-died ``degraded``) and the honcho
    fallback (a bench genuinely serving under ``bench start`` must not read as
    all-down) are exactly the answers the fused script cannot give, and they are
    honesty properties, not speed ones - so the exec budget yields to them rather
    than the other way round. The cost lands only OFF the supervised steady state,
    which is the case the fused path exists for.
    """
    probed = probe_web and web_port is not None
    fused = supervision.fused_probe(
        frappe_container,
        bench_path,
        web_port=web_port,
        web_site=web_site,
        probe_web=probe_web,
    )
    if not fused.supervisor_up:
        return _bench_status(
            frappe_container,
            index=index,
            bench_path=bench_path,
            label=label,
            web_port=web_port,
            web_site=web_site,
            probe_web=probe_web,
            warnings=warnings,
        )

    return BenchStatus(
        index=index,
        bench_path=bench_path,
        label=label,
        overall=_overall(
            started=True,
            supervisor_up=True,
            all_healthy=_all_healthy(fused.processes),
            web_code=fused.web_http_code,
            web_probed=probed,
        ),
        supervisor_up=True,
        web_port=web_port,
        web_port_verified=web_port is not None,
        web_site=web_site if probed else None,
        web_http_code=fused.web_http_code,
        processes=fused.processes,
    )


def _fold(benches: list[BenchStatus]) -> str:
    """The instance aggregate over the per-bench aggregates. Four tokens, no fifth.

    ``degraded`` dominates, so a real fault is never masked by a healthy sibling.
    The one non-obvious rule is that **a ``running`` bench beats a never-started
    ``online`` one**, and it is the rule the whole change turns on: after
    ``cwcli start <p> --bench 1`` bench 0 was never started (honestly ``online``)
    while bench 1 genuinely serves. A plain worst-wins fold ordered
    ``degraded > online > running`` would call the instance ``online``, which reads
    as "nothing is started" while a bench serves real traffic - a correct-looking
    token that is wrong about a healthy bench, the same defect in a new costume.

    The per-bench rows carry the detail either way: the instance token is a summary,
    and a summary must be neither more alarming nor more reassuring than its rows.
    """
    if not benches:
        return OFFLINE
    tokens = {b.overall for b in benches}
    if DEGRADED in tokens:
        return DEGRADED
    if RUNNING in tokens:
        return RUNNING
    if ONLINE in tokens:
        return ONLINE
    return OFFLINE


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
