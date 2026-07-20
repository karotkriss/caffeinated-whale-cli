"""``core.update`` - the app-update state machine, UI-pure.

Owns everything ``commands/update.py:_update_project`` used to do between "ensure
running" and the printed summary: the per-app ``git pull``, the post-pull recache,
the affected-site discovery, the ``--site`` narrowing, the maintenance-mode
lifecycle and its unconditional ``finally``, the migration fan-out, the optional
build and cache/lock clears, and the seven-way failure aggregation - **returned as
an** :class:`UpdateReport` **rather than printed**.

**It is NOT a generator, and that is deliberate.** The obvious shape, by analogy
with ``core.run_stream``, is ``update_stream(plan) -> Iterator[UpdateEvent]``. That
would put the maintenance-mode ``try/finally`` inside a generator, where cleanup
stops being guaranteed and starts depending on the consumer. Probed, all five
consumer shapes::

    consumer exhausts it .................... finally ran: True
    consumer breaks early (refcount drops) .. finally ran: True
    consumer breaks early, HOLDS a reference. finally ran: FALSE  <- maintenance ON
    generator caught in a reference CYCLE ... finally ran: FALSE  <- maintenance ON
    contextlib.closing(...) ................. finally ran: True

Cases 3 and 4 are not exotic: a GUI pumping events from an event loop holds the
iterator on ``self``, and a widget tree is a reference cycle. A window closed
mid-update would leave a site in maintenance mode until the garbage collector
happened to run - and enabling that GUI is what this rework exists for. So the
state machine stays in a plain function, whose ``finally`` cannot be skipped, and
progress rides an optional ``on_event`` callback instead. ``contextlib.closing``
restores determinism but relocates a safety-critical guarantee into every
frontend's hands, including ones not yet written; that is the wrong place for it.

This is a deliberate reading of the locked "streaming operations return typed event
iterators" decision: that governs genuine STREAMING operations (``logs``, raw exec
output - which :mod:`.exec_stream` already is and remains). ``update`` is a state
machine that emits progress and whose terminal value is a report. ``run_stream``
has the same GC exposure and is fine: an abandoned ``run_stream`` leaks a socket
until GC, not a stuck site.

**A lost stream continues the fan-out and is UNKNOWN, never a failure.** Two
behaviours used to fall out of one real-world event depending on whether Docker
happened to record an exit code: a migration returning non-zero was recorded and
the fan-out continued, while a migration whose stream was lost aborted it. They are
one behaviour here. Unknown is kept apart from failed because a lost stream means
the command MAY STILL BE RUNNING: an agent branching on this report will retry a
failure, and retrying a live migration is harmful.

**The layering signal is SETTLED** (openspec ``migrate-inspect-core``): the
mid-fan-out recache still calls ``utils.cache.recache_project`` (it runs
mid-state-machine, between the pull and the discovery that depends on it, so it
cannot be hoisted into the frontend), but that seam now re-points at
``core.inspect`` - no module under ``core/`` imports the CLI layer at runtime
any more.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..utils import cache, db_utils
from . import bench_ops, credbridge, resolvers
from . import docker as core_docker
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_stream

# Sites bench keeps under sites/ that are not sites.
_NON_SITE_ENTRIES = {"apps.txt", "assets", "common_site_config.json", "example.com", "apps.json"}

# Let a migration finish releasing its locks before the next step touches the site.
_POST_MIGRATE_SETTLE = 0.5


# ------------------------------------------------------------------------------ DTOs


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateReport:
    """The typed outcome of an update run (serializable; no live objects).

    The seven-way aggregation `_update_project` used to print, returned instead.
    ``failed_maintenance_disable`` is the field that justifies an agent-facing
    verb: it is the only outcome that leaves the user's site DOWN, and today it is
    reachable only by scraping rich markup off stdout.
    """

    project: str
    bench_path: str
    apps: list[str]
    frappe_reset: bool  # the bench-wide path ran; the per-site fields are empty
    affected_sites: list[str]
    migrated_sites: list[str]  # the load-bearing gate: only what entered maintenance
    failed_apps: list[str]
    unknown_apps: list[str]  # its stream was lost; it MAY still be running
    failed_maintenance_enable: list[str]  # affected but NOT migrated
    failed_migrations: list[str]
    unknown_migrations: list[str]  # may still be running: do NOT retry blindly
    failed_builds: list[str]
    unknown_builds: list[str]
    failed_cache_clears: list[str]
    failed_website_cache_clears: list[str]
    failed_maintenance_disable: list[str]  # STUCK: the site is DOWN, needs manual action
    aborted: bool  # the fan-out stopped early (Ctrl-C / an unexpected error)
    ok: bool  # pre-computed aggregate; the frontends' exit code reads THIS


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateStepStart:
    """A step began. ``index``/``total`` drive an "(2/5)"-style progress label."""

    phase: str  # "pull" | "migrate" | "build" | "frappe_reset" | ...
    item: str | None = None  # the app or site, when the phase has one
    index: int = 1
    total: int = 1
    message: str | None = None  # something specific to say HERE, in run order


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateOutput:
    """A chunk of a bench command's output, tagged with the stream it came from."""

    phase: str
    item: str | None
    stream: str  # "stdout" | "stderr", carried through from ExecChunk
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateStepEnd:
    """A step finished. ``status`` is the honest three-way outcome.

    ``message`` carries WHY this step ended this way when the reason is not implied
    by the phase (an app with no directory, a lost stream). The events are the run's
    narration in order, so the text a frontend must show at that moment rides here;
    the same facts also ride the envelope's ``warnings`` for structured consumers,
    which have no use for ordering.
    """

    phase: str
    item: str | None = None
    status: str = "ok"  # "ok" | "failed" | "unknown"
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateAborted:
    """Terminal event when an exception is unwinding: the report, before it is lost.

    A RETURNED report cannot survive an unwinding ``KeyboardInterrupt``, and a
    Ctrl-C mid-update must still surface a site left stuck in maintenance mode with
    its remediation. The ``finally`` builds the report either way; on the abort path
    it hands it here instead of returning it.
    """

    report: UpdateReport


UpdateEvent = UpdateStepStart | UpdateOutput | UpdateStepEnd | UpdateAborted

OnEvent = Callable[[UpdateEvent], None]


# --------------------------------------------------------------------------- helpers


def _noop(_event: UpdateEvent) -> None:
    """The drain-and-discard consumption mode: what `axi` and `--json` pass."""


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


def _require_bench_dirs(frappe_container, bench_path: str) -> None:
    """Raise NOT_FOUND unless BOTH ``{bench_path}/apps`` and ``/sites`` exist.

    Deliberately NOT ``resolvers.require_bench_dir``: that probes ``sites/`` only,
    so reusing it would silently drop update's ``apps/`` check, and widening it
    would add a check to ``backup``/``unlock``, which this batch does not touch.
    Update keeps its own probe; the resolver stays unbent.

    Uses the shell-free argv form (``["test", "-d", path]``) the resolver already
    uses, rather than the ``["sh", "-c", "test -d " + shlex.quote(path)]`` this
    replaces: no shell at all is strictly safer than a correctly quoted one.
    """
    for sub in ("apps", "sites"):
        exit_code, _ = frappe_container.exec_run(["test", "-d", f"{bench_path}/{sub}"])
        if exit_code != 0:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "bench.dir_missing",
                f"Bench directory not found at {bench_path}",
                hint=f"Make sure the bench path is correct. Current path: {bench_path}",
            )


def _stream_step(
    container,
    cmd: str,
    *,
    workdir: str,
    phase: str,
    item: str | None,
    emit: OnEvent,
    warnings: list[Message],
) -> tuple[int | None, str | None]:
    """Run one streamed exec, emitting its output. Returns ``(exit_code, lost_reason)``.

    An exit code of ``None`` means the outcome is genuinely UNKNOWABLE (the stream
    was lost, or the daemon recorded no code), never that it failed. See
    :mod:`.exec_stream`. The reason comes back too, so the frontend can say WHAT was
    lost at the moment it happened rather than only in the final report.
    """
    exit_code = 1
    try:
        for event in exec_stream(container, cmd, workdir=workdir):
            if isinstance(event, ExecChunk):
                emit(UpdateOutput(phase=phase, item=item, stream=event.stream, text=event.text))
            else:
                exit_code = event.exit_code
    except CwcliError as e:
        # Do NOT let this abort the fan-out, and do NOT call it a failure: the
        # command may still be running in the container. Every exec-stream error is
        # treated as unknown, INCLUDING exec.start_failed - claiming "it never ran"
        # from an API call whose own outcome is uncertain would be a confident guess,
        # and the safe direction for a retry decision is unknown.
        where = phase + (f" '{item}'" if item else "")
        reason = f"Lost track of {where}: {e.message}"
        warnings.append(Message(e.code, reason, detail={"phase": phase, "item": item}))
        return None, reason
    return exit_code, None


def _run_step(
    container,
    cmd: str,
    *,
    workdir: str,
    phase: str,
    item: str | None,
    index: int,
    total: int,
    emit: OnEvent,
    warnings: list[Message],
    failed: list[str],
    unknown: list[str],
) -> str:
    """Emit start, stream, classify honestly, emit end. Returns the status token.

    The one place the failed/unknown distinction is made, so every streamed phase
    makes it the same way.
    """
    emit(UpdateStepStart(phase=phase, item=item, index=index, total=total))
    code, lost = _stream_step(
        container, cmd, workdir=workdir, phase=phase, item=item, emit=emit, warnings=warnings
    )
    key = item if item is not None else phase
    if code is None:
        unknown.append(key)
        status = "unknown"
    elif code != 0:
        failed.append(key)
        status = "failed"
    else:
        status = "ok"
    emit(UpdateStepEnd(phase=phase, item=item, status=status, message=lost))
    return status


def _set_maintenance(container, bench_path: str, site: str, *, enable: bool) -> bool:
    """Turn maintenance mode on/off for one site. True when bench accepted it.

    PROMOTED to :func:`core.bench_ops.set_maintenance` when the standalone
    ``migrate`` verb became its second caller (openspec ``add-axi-bench-exec-verbs``).
    This stays as the module's own spelling so every call site below is unchanged;
    the implementation is now shared. Do NOT re-inline it - two copies of this
    lifecycle drift until one stops disabling, and a site stuck in maintenance mode
    is a site that is down.
    """
    return bench_ops.set_maintenance(container, bench_path, site, enable=enable)


def _sites_with_app(project_name: str, bench_path: str, app: str, container) -> list[str]:
    """Sites with ``app`` installed: the cache when it knows, else a live query."""
    cached = db_utils.get_cached_project_data(project_name)
    if cached:
        found = [
            site.get("name")
            for bench in cached.get("bench_instances", [])
            if bench.get("path") == bench_path
            for site in bench.get("sites", [])
            if app in site.get("installed_apps", [])
        ]
        if found:
            return found

    if not container:
        return []

    exit_code, output = container.exec_run(
        f"ls -1 {shlex.quote(bench_path)}/sites", workdir=bench_path
    )
    if exit_code != 0:
        return []
    all_sites = [
        item.strip()
        for item in _decode(output).split("\n")
        if item.strip() and item.strip() not in _NON_SITE_ENTRIES
    ]

    found = []
    for site in all_sites:
        exit_code, output = container.exec_run(
            f"bench --site {shlex.quote(site)} list-apps", workdir=bench_path
        )
        if exit_code != 0:
            continue
        installed = [
            line.strip().split()[0] for line in _decode(output).split("\n") if line.strip()
        ]
        if app in installed:
            found.append(site)
    return found


def _build_report(
    *,
    project_name: str,
    bench_path: str,
    apps: list[str],
    frappe_reset: bool,
    affected: set[str],
    migrated: list[str],
    failed_apps: list[str],
    unknown_apps: list[str],
    failed_maintenance_enable: list[str],
    failed_migrations: list[str],
    unknown_migrations: list[str],
    failed_builds: list[str],
    unknown_builds: list[str],
    failed_cache_clears: list[str],
    failed_website_cache_clears: list[str],
    failed_maintenance_disable: list[str],
    aborted: bool,
) -> UpdateReport:
    """Assemble the report and pre-compute ``ok``.

    ``ok`` is pre-computed here rather than re-derived per frontend, matching the
    ``"ok"`` key `apps`'s other subcommands already emit. An UNKNOWN outcome makes
    ``ok`` false: an unknowable exit code is not a success.
    """
    ok = not (
        aborted
        or failed_apps
        or unknown_apps
        or failed_maintenance_enable
        or failed_migrations
        or unknown_migrations
        or failed_builds
        or unknown_builds
        or failed_cache_clears
        or failed_website_cache_clears
        or failed_maintenance_disable
    )
    return UpdateReport(
        project=project_name,
        bench_path=bench_path,
        apps=list(apps),
        frappe_reset=frappe_reset,
        affected_sites=sorted(affected),
        migrated_sites=list(migrated),
        failed_apps=list(failed_apps),
        unknown_apps=list(unknown_apps),
        failed_maintenance_enable=list(failed_maintenance_enable),
        failed_migrations=list(failed_migrations),
        unknown_migrations=list(unknown_migrations),
        failed_builds=list(failed_builds),
        unknown_builds=list(unknown_builds),
        failed_cache_clears=list(failed_cache_clears),
        failed_website_cache_clears=list(failed_website_cache_clears),
        failed_maintenance_disable=list(failed_maintenance_disable),
        aborted=aborted,
        ok=ok,
    )


def _recache(project_name: str, warnings: list[Message], emit: OnEvent) -> None:
    """Refresh the cache so the site discovery that follows is accurate."""
    emit(UpdateStepStart(phase="recache"))
    ok = cache.recache_project(project_name)
    emit(UpdateStepEnd(phase="recache", status="ok" if ok else "failed"))
    if not ok:
        warnings.append(
            Message(
                "recache.failed",
                "Failed to recache project. Site detection may be inaccurate.",
            )
        )


def _frappe_reset(
    frappe_container,
    *,
    project_name: str,
    bench_path: str,
    apps: list[str],
    no_recache: bool,
    ignored: list[str],
    emit: OnEvent,
    warnings: list[Message],
) -> Result[UpdateReport]:
    """The bench-wide ``bench update --reset`` path (the ``frappe`` app special-case).

    The framework is not updated with a per-app ``git pull``; the correct path is
    bench's own reset, which resets every app's repo, pulls, migrates every site,
    and rebuilds. It therefore ignores the per-app/per-site options, and it returns
    BEFORE the maintenance-mode state machine is ever entered - it enables no
    maintenance and has no ``finally``; ``bench update --reset`` manages its own.
    """
    note = None
    if ignored:
        note = (
            "updating 'frappe' runs a bench-wide 'bench update --reset'; ignoring "
            f"{', '.join(ignored)} (not applicable)."
        )
        warnings.append(
            Message("frappe_reset.options_ignored", note, detail={"ignored": list(ignored)})
        )

    failed: list[str] = []
    unknown: list[str] = []
    # The note rides the START event so a frontend shows "ignoring X" BEFORE the
    # reset runs, where it is actionable, rather than after it from the envelope.
    emit(UpdateStepStart(phase="frappe_reset", item="frappe", message=note))
    code, lost = _stream_step(
        frappe_container,
        "bench update --reset",
        workdir=bench_path,
        phase="frappe_reset",
        item="frappe",
        emit=emit,
        warnings=warnings,
    )
    if code is None:
        unknown.append("frappe")
        status = "unknown"
    elif code != 0:
        failed.append("frappe")
        status = "failed"
    else:
        status = "ok"
    emit(UpdateStepEnd(phase="frappe_reset", item="frappe", status=status, message=lost))

    # Recache BEFORE the exit code is consulted, exactly as this path always has: a
    # partially-applied reset genuinely changes the cache, so a failed reset still
    # warrants a refresh. Defensible and untested either way; preserved deliberately
    # rather than "fixed" inside a migration.
    if not no_recache:
        _recache(project_name, warnings, emit)

    report = _build_report(
        project_name=project_name,
        bench_path=bench_path,
        apps=apps,
        frappe_reset=True,
        affected=set(),
        migrated=[],
        failed_apps=failed,
        unknown_apps=unknown,
        failed_maintenance_enable=[],
        failed_migrations=[],
        unknown_migrations=[],
        failed_builds=[],
        unknown_builds=[],
        failed_cache_clears=[],
        failed_website_cache_clears=[],
        failed_maintenance_disable=[],
        aborted=False,
    )
    return Result(status=Status.OK if report.ok else Status.WARNING, data=report, warnings=warnings)


# ------------------------------------------------------------------------------ verb


def update(
    project_name: str,
    apps: list[str],
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    sites: list[str] | None = None,
    clear_cache: bool = False,
    clear_website_cache: bool = False,
    build: bool = False,
    skip_maintenance: bool = False,
    no_recache: bool = False,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[UpdateReport]:
    """Update ``apps`` and migrate every affected site. See the module docstring.

    Returns ``NEEDS_CHOICE`` for the multi-bench and stopped-container forks, raises
    :class:`CwcliError` for hard failures, and otherwise returns the report - which
    names every per-phase failure rather than raising on it, because reporting all
    of them IS the job.
    """
    emit: OnEvent = on_event or _noop
    warnings: list[Message] = []

    if not apps:
        raise CwcliError(ErrorKind.USAGE, "apps.required", "At least one app must be specified.")

    # 1. Resolve the frappe container (raises NOT_FOUND / DOCKER).
    frappe_container = core_docker.get_frappe_container(project_name)

    # 2. Container must be running; a stopped container is a confirm_start fork.
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # 3. Resolve which bench to update (--bench/--path, else single, else the
    # historical default when nothing is cached).
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        bench_path = bench_result.data
        warnings.extend(bench_result.warnings)

    assert bench_path is not None  # narrowed by the branches above

    # 4. Both bench directories must exist (update's own probe - see _require_bench_dirs).
    _require_bench_dirs(frappe_container, bench_path)

    # Bridge the container's git back to the host's gh/glab so a `git pull` /
    # `bench update --reset` against a PRIVATE app remote authenticates without any
    # gh/glab or stored token in the container. Inert for public repos (git only
    # calls a credential helper on 401), so every update is wrapped; see
    # core.credbridge.
    with credbridge.credential_bridge(frappe_container, bench_path):
        # 5. The frappe framework special-case: bench-wide, and it returns BEFORE the
        # maintenance-mode state machine is entered.
        if any(app.lower() == "frappe" for app in apps):
            ignored = [
                name
                for name, active in (
                    ("--site", bool(sites)),
                    ("--clear-cache", clear_cache),
                    ("--clear-website-cache", clear_website_cache),
                    ("--build", build),
                    ("--skip-maintenance", skip_maintenance),
                )
                if active
            ]
            return _frappe_reset(
                frappe_container,
                project_name=project_name,
                bench_path=bench_path,
                apps=apps,
                no_recache=no_recache,
                ignored=ignored,
                emit=emit,
                warnings=warnings,
            )

        return _update_apps(
            frappe_container,
            project_name=project_name,
            bench_path=bench_path,
            apps=apps,
            sites_filter=sites,
            clear_cache=clear_cache,
            clear_website_cache=clear_website_cache,
            build=build,
            skip_maintenance=skip_maintenance,
            no_recache=no_recache,
            emit=emit,
            warnings=warnings,
        )


def _update_apps(  # noqa: C901 - the state machine's phases are the function
    frappe_container,
    *,
    project_name: str,
    bench_path: str,
    apps: list[str],
    sites_filter: list[str] | None,
    clear_cache: bool,
    clear_website_cache: bool,
    build: bool,
    skip_maintenance: bool,
    no_recache: bool,
    emit: OnEvent,
    warnings: list[Message],
) -> Result[UpdateReport]:
    """The per-app pull + per-site migrate state machine, with its maintenance-mode
    lifecycle. Kept in a PLAIN function so the ``finally`` is unconditional."""
    affected: set[str] = set()
    failed_apps: list[str] = []
    unknown_apps: list[str] = []
    failed_migrations: list[str] = []
    unknown_migrations: list[str] = []
    failed_builds: list[str] = []
    unknown_builds: list[str] = []
    failed_cache_clears: list[str] = []
    failed_website_cache_clears: list[str] = []
    failed_maintenance_disable: list[str] = []  # sites left stuck in maintenance mode
    failed_maintenance_enable: list[str] = []  # never in maintenance, so not migrated
    maintenance_sites: set[str] = set()  # sites we actually turned maintenance ON for
    sites_to_migrate: list[str] = []  # the fan-out set; empty until we get that far
    aborted = False

    try:
        # --- ONE pull pass and ONE discovery pass. The presentation fork that used
        # to duplicate both is gone: rendering is the frontend's business now, so
        # the data path cannot be mis-bound to a data condition again. ---
        for i, app in enumerate(apps, 1):
            app_path = f"{bench_path}/apps/{app}"
            exit_code, _ = frappe_container.exec_run(["test", "-d", app_path])
            if exit_code != 0:
                # A missing app directory is a DIFFERENT failure from a pull that
                # ran and failed, and it says so: no pull is attempted.
                not_found = f"App '{app}' not found at {app_path}"
                emit(UpdateStepStart(phase="pull", item=app, index=i, total=len(apps)))
                failed_apps.append(app)
                warnings.append(Message("app.not_found", not_found))
                emit(UpdateStepEnd(phase="pull", item=app, status="failed", message=not_found))
                continue
            _run_step(
                frappe_container,
                "git pull",
                workdir=app_path,
                phase="pull",
                item=app,
                index=i,
                total=len(apps),
                emit=emit,
                warnings=warnings,
                failed=failed_apps,
                unknown=unknown_apps,
            )

        # An app whose outcome is unknown is not known to have updated, so it is not
        # discovered against - the same treatment a failed pull gets.
        skipped = set(failed_apps) | set(unknown_apps)
        if no_recache:
            emit(UpdateStepStart(phase="recache_skipped"))
        elif len(skipped) < len(apps):
            _recache(project_name, warnings, emit)

        for app in apps:
            if app in skipped:
                continue
            emit(UpdateStepStart(phase="discover", item=app))
            sites = _sites_with_app(project_name, bench_path, app, frappe_container)
            affected.update(sites)
            emit(
                UpdateStepEnd(
                    phase="discover",
                    item=app,
                    message=(
                        f"Found {len(sites)} site(s) with '{app}' installed"
                        if sites
                        else f"No sites found with '{app}' installed"
                    ),
                )
            )

        # Narrow to the sites named with --site (if any); no --site keeps them all.
        unfiltered = set(affected)
        if sites_filter:
            allowed = set(sites_filter)
            affected = {s for s in affected if s in allowed}
        # Refuse when --site narrows an actually-affected set to empty: that is a
        # typo/mismatch, distinct from "no site has the app at all" (which exits 0).
        # It cannot live in a resolve phase - it needs the discovered sites.
        if sites_filter and unfiltered and not affected:
            raise CwcliError(
                ErrorKind.USAGE,
                "site_filter.matched_nothing",
                "--site matched no affected site(s). "
                f"Requested: {', '.join(sorted(sites_filter))}; "
                f"affected: {', '.join(sorted(unfiltered))}",
            )

        # Enable maintenance mode per site, recording each success AS it happens so
        # a mid-loop failure still leaves an accurate record of what to undo.
        if not skip_maintenance and affected:
            for i, site in enumerate(sorted(affected), 1):
                emit(
                    UpdateStepStart(
                        phase="maintenance_enable", item=site, index=i, total=len(affected)
                    )
                )
                ok = _set_maintenance(frappe_container, bench_path, site, enable=True)
                if ok:
                    maintenance_sites.add(site)
                emit(
                    UpdateStepEnd(
                        phase="maintenance_enable", item=site, status="ok" if ok else "failed"
                    )
                )
            failed_maintenance_enable = sorted(affected - maintenance_sites)
            # A phase-level End (item=None) distinct from the per-site ones above:
            # this is the unconditional (non-verbose-gated) confirmation that live
            # sites actually went down before migrations start.
            emit(
                UpdateStepEnd(
                    phase="maintenance_enable",
                    message=f"Maintenance mode enabled for {len(maintenance_sites)} site(s)",
                )
            )

        # THE LOAD-BEARING GATE: migrate only the sites actually in maintenance mode
        # (or every affected site when maintenance is skipped). Never migrate, and
        # never clear caches/locks for, a site we could not put into maintenance.
        sites_to_migrate = sorted(affected) if skip_maintenance else sorted(maintenance_sites)

        # A phase-level Start/End pair wrapping the loop below, so the renderer sees
        # the batch boundary (and its total, including 0) even when the loop body
        # never runs - inferring it from index==total cannot cover the empty case.
        emit(UpdateStepStart(phase="migrate_batch", total=len(sites_to_migrate)))
        for i, site in enumerate(sites_to_migrate, 1):
            _run_step(
                frappe_container,
                f"bench --site {shlex.quote(site)} migrate",
                workdir=bench_path,
                phase="migrate",
                item=site,
                index=i,
                total=len(sites_to_migrate),
                emit=emit,
                warnings=warnings,
                failed=failed_migrations,
                unknown=unknown_migrations,
            )
            time.sleep(_POST_MIGRATE_SETTLE)
        emit(UpdateStepEnd(phase="migrate_batch"))

        if build:
            buildable = [a for a in apps if a not in skipped]
            for i, app in enumerate(buildable, 1):
                _run_step(
                    frappe_container,
                    f"bench build --app {shlex.quote(app)}",
                    workdir=bench_path,
                    phase="build",
                    item=app,
                    index=i,
                    total=len(buildable),
                    emit=emit,
                    warnings=warnings,
                    failed=failed_builds,
                    unknown=unknown_builds,
                )

        for active, subcmd, phase, failures in (
            (clear_cache, "clear-cache", "clear_cache", failed_cache_clears),
            (
                clear_website_cache,
                "clear-website-cache",
                "clear_website_cache",
                failed_website_cache_clears,
            ),
        ):
            if not active:
                continue
            for i, site in enumerate(sites_to_migrate, 1):
                emit(UpdateStepStart(phase=phase, item=site, index=i, total=len(sites_to_migrate)))
                exit_code, _ = frappe_container.exec_run(
                    f"bench --site {shlex.quote(site)} {subcmd}", workdir=bench_path
                )
                if exit_code != 0:
                    failures.append(site)
                emit(
                    UpdateStepEnd(
                        phase=phase, item=site, status="ok" if exit_code == 0 else "failed"
                    )
                )

        for i, site in enumerate(sites_to_migrate, 1):
            emit(
                UpdateStepStart(
                    phase="clear_locks", item=site, index=i, total=len(sites_to_migrate)
                )
            )
            locks_path = f"{bench_path}/sites/{site}/locks"
            exit_code, _ = frappe_container.exec_run(
                f"rm -rf {shlex.quote(locks_path)}", workdir=bench_path
            )
            emit(
                UpdateStepEnd(
                    phase="clear_locks", item=site, status="ok" if exit_code == 0 else "failed"
                )
            )

    except BaseException:
        # Only records that the fan-out stopped early, then re-raises untouched.
        # BaseException (not Exception) so a Ctrl-C is reported the same way: it
        # leaves sites in maintenance exactly as any other interruption does.
        aborted = True
        raise
    finally:
        # CRITICAL, and the reason this is a plain function: always disable
        # maintenance for every site we enabled, even if the update blew up, and
        # record any site that cannot be taken back out so it is never left silent.
        if not skip_maintenance and maintenance_sites:
            for i, site in enumerate(sorted(maintenance_sites), 1):
                emit(
                    UpdateStepStart(
                        phase="maintenance_disable",
                        item=site,
                        index=i,
                        total=len(maintenance_sites),
                    )
                )
                try:
                    ok = _set_maintenance(frappe_container, bench_path, site, enable=False)
                except Exception as e:  # noqa: BLE001 - a stuck site must still be reported
                    warnings.append(
                        Message(
                            "maintenance.disable_error",
                            f"Failed to disable maintenance mode for '{site}': {e}",
                        )
                    )
                    ok = False
                if not ok:
                    failed_maintenance_disable.append(site)
                emit(
                    UpdateStepEnd(
                        phase="maintenance_disable",
                        item=site,
                        status="ok" if ok else "failed",
                    )
                )

        # An abort is only worth reporting once there was a fan-out to abandon:
        # raising earlier (a --site typo, a failed pull) leaves nothing half-done, so
        # the raise's own error stands alone rather than being dressed up as an
        # interrupted update - while any failure already accumulated still reports.
        report = _build_report(
            project_name=project_name,
            bench_path=bench_path,
            apps=apps,
            frappe_reset=False,
            affected=affected,
            migrated=sites_to_migrate,
            failed_apps=failed_apps,
            unknown_apps=unknown_apps,
            failed_maintenance_enable=failed_maintenance_enable,
            failed_migrations=failed_migrations,
            unknown_migrations=unknown_migrations,
            failed_builds=failed_builds,
            unknown_builds=unknown_builds,
            failed_cache_clears=failed_cache_clears,
            failed_website_cache_clears=failed_website_cache_clears,
            failed_maintenance_disable=failed_maintenance_disable,
            aborted=aborted and bool(sites_to_migrate),
        )
        if aborted:
            # The exception is still unwinding, so this report can never be RETURNED.
            # Hand it over before it is lost: a stuck site must keep its remediation
            # even when the run ends in a Ctrl-C.
            emit(UpdateAborted(report=report))

    return Result(status=Status.OK if report.ok else Status.WARNING, data=report, warnings=warnings)
