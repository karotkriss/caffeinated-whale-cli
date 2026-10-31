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

import dataclasses
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..utils import bench_sites, cache, db_utils
from . import app_deps, bench_ops, bench_read, credbridge, resolvers, supervision, update_preflight
from . import apps as core_apps
from . import docker as core_docker
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_stream
from .update_preflight import PreflightFinding

# Let a migration finish releasing its locks before the next step touches the site.
_POST_MIGRATE_SETTLE = 0.5


# ------------------------------------------------------------------------------ DTOs


# The core apps (the backups spec's "core updates": frappe / erpnext). A core update
# resets only the core apps it is NAMED for, so `apps update <p> frappe` never touches
# erpnext; every other app is left alone.
CORE_APPS = ("frappe", "erpnext")


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
    # A core update (frappe / erpnext): those apps were RESET to their remote branch
    # (fetch + reset --hard, those apps only) after the preflight and the data-loss
    # guard, every site's locks were cleared first and the bench's requirements
    # reinstalled; then the ordinary per-site migrate. Nothing bench-wide.
    frappe_reset: bool
    affected_sites: list[str]
    migrated_sites: list[str]  # the load-bearing gate: only what entered maintenance
    failed_apps: list[str]
    unknown_apps: list[str]  # its stream was lost; it MAY still be running
    force_reset_apps: list[str]  # --force hard-reset these to upstream on a conflict (DESTRUCTIVE)
    failed_maintenance_enable: list[str]  # affected but NOT migrated
    failed_migrations: list[str]
    unknown_migrations: list[str]  # may still be running: do NOT retry blindly
    failed_builds: list[str]
    unknown_builds: list[str]
    failed_cache_clears: list[str]
    failed_website_cache_clears: list[str]
    failed_maintenance_disable: list[str]  # STUCK: the site is DOWN, needs manual action
    # The bench's requirements step before any migrate (`bench setup requirements`,
    # `--python` for a non-core update): "ok" | "failed" | "unknown" | "skipped"
    # (--skip-requirements), None when no app was updated so it never ran. Failed or
    # unknown means no site was migrated onto the new code.
    requirements: str | None
    # A failed requirements step's own lines that name the failure (the installer's
    # `error:`/`cause:` lines, not bench's traceback after them); empty otherwise.
    requirements_cause: list[str]
    # Sites discovery could not open (a rejected database login, a broken config):
    # the code moved under them and they were NOT migrated, so the run is not ok.
    unreadable_sites: list[str]
    # The post-update resynchronisation (core.supervision.resync_after_code_change):
    # `git pull` moved the code under processes that were already running, so they
    # are cycled and the migrated sites must genuinely serve afterwards.
    restarted_processes: list[str]  # the bench programs actually cycled
    unserved_sites: list[str]  # migrated sites that did NOT come back with HTTP 200
    resync_error: str | None  # the resync could not be completed or proved at all
    # BUG-10: where each updated app physically lives vs where the bench imports it
    # from. An app flagged `diverged` was pulled into a copy the running site does
    # not load, so `ok` is False even though the git pull "succeeded".
    app_imports: list[resolvers.AppImport]
    # Always False now: cwcli never runs bench's bench-wide update, so nothing can
    # set maintenance_mode/pause_scheduler bench-wide. Kept so documents keep their shape.
    bench_left_in_maintenance: bool | None
    # The checks before anything moves: the frappe path's (core.update_preflight)
    # plus, for every named app, the dependency tier (what its new code declares).
    # Under --no-preflight, only the data-loss guard's findings. A blocked preflight
    # ran NOTHING: no backup, no maintenance flag, no reset, no pull.
    preflight: list[PreflightFinding]
    preflight_blocked: bool
    # The frappe path's data-loss guard (core.update_preflight.local_work): apps whose
    # uncommitted or unpushed work `bench update --reset` discards, or whose work could
    # not be checked. Without --force that refuses with NOTHING run
    # (`data_loss_refused`), even under --no-preflight; with --force the reset ran
    # over them and these are the apps it was allowed to discard work in.
    data_loss_refused: bool
    data_loss_apps: list[str]
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


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdatePreflight:
    """The preflight finished: every finding, BEFORE the reset starts.

    The end of the ``preflight`` phase its ``UpdateStepStart`` opened. It carries the
    findings rather than a message because a frontend must show them at this moment:
    a data-loss warning printed after the reset has run warns about nothing.
    """

    findings: list[PreflightFinding]
    summary: str
    blocked: bool  # the update will not run: a blocker, or local work without --force
    local_only: bool = False  # --no-preflight: only the data-loss guard ran


UpdateEvent = UpdateStepStart | UpdateOutput | UpdateStepEnd | UpdatePreflight | UpdateAborted

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
    environment: dict[str, str] | None = None,
) -> tuple[int | None, str | None]:
    """Run one streamed exec, emitting its output. Returns ``(exit_code, lost_reason)``.

    An exit code of ``None`` means the outcome is genuinely UNKNOWABLE (the stream
    was lost, or the daemon recorded no code), never that it failed. See
    :mod:`.exec_stream`. The reason comes back too, so the frontend can say WHAT was
    lost at the moment it happened rather than only in the final report.

    ``environment`` rides through to the exec; the migrate phase stamps its interrupt
    marker there (see :func:`core.bench_ops.migrate_env`).
    """
    exit_code = 1
    try:
        for event in exec_stream(container, cmd, workdir=workdir, environment=environment):
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


def _failure_cause(output: str) -> list[str]:
    """The lines of a failed step's output that name the failure.

    bench wraps a failed subcommand in its own CommandFailedError traceback, so the
    last lines are always bench's frames; the installer's own ``error:``/``cause:``
    lines (uv, pip) sit earlier. Each such line is kept with its deeper-indented
    continuation lines. Falls back to the last lines when none match.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    picked: list[str] = []
    indent: int | None = None
    for line in lines:
        depth = len(line) - len(line.lstrip())
        if line.strip().lower().startswith(("error:", "cause:")):
            picked.append(line)
            indent = depth
        elif indent is not None and depth > indent:
            picked.append(line)
        else:
            indent = None
    # ponytail: capped so a step spewing many error lines stays one readable block.
    return picked[:8] or lines[-4:]


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
    environment: dict[str, str] | None = None,
) -> str:
    """Emit start, stream, classify honestly, emit end. Returns the status token.

    The one place the failed/unknown distinction is made, so every streamed phase
    makes it the same way.
    """
    emit(UpdateStepStart(phase=phase, item=item, index=index, total=total))
    code, lost = _stream_step(
        container,
        cmd,
        workdir=workdir,
        phase=phase,
        item=item,
        emit=emit,
        warnings=warnings,
        environment=environment,
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


def _restore_pre_maintenance_on_abort(
    frappe_container,
    bench_path: str,
    site: str,
    pre_state: bool | None,
    *,
    project_name: str,
    emit: OnEvent,
    warnings: list[Message],
) -> None:
    """Restore ``site``'s pre-migrate maintenance state after an interrupted
    ``--skip-maintenance`` run whose orphan cwcli ended.

    Under ``--skip-maintenance`` cwcli never enabled maintenance, so the finally-block
    disable does not run - but cwcli still ENDED the orphaned migrate (which on v15+
    self-manages maintenance and would otherwise clear it as it finished). Left alone
    that strands the site at 503. Rather than blindly clearing under a flag whose whole
    point is operator-managed maintenance, restore the pre-migrate state per site:

    - was OFF before the migrate: clear it LAST (settle/re-check), so it is not stuck;
    - was ON before (operator-managed): LEAVE it on, and say so in one line;
    - unknown (pre-state unreadable): clear it and name how to turn it back on.
    """
    if pre_state:
        emit(
            UpdateStepEnd(
                phase="interrupt_cleanup",
                item=site,
                status="ok",
                message=(
                    f"Left '{site}' in maintenance mode: it was already in maintenance "
                    "before the migrate, and --skip-maintenance leaves operator-managed "
                    "maintenance state alone."
                ),
            )
        )
        return
    ok = bench_ops.clear_maintenance(frappe_container, bench_path, site, recheck=True)
    if pre_state is None:
        emit(
            UpdateStepEnd(
                phase="interrupt_cleanup",
                item=site,
                status="ok" if ok else "failed",
                message=(
                    f"Cleared maintenance mode for '{site}' after the interrupt (its "
                    "pre-migrate state could not be read). If it should stay in "
                    f"maintenance, run: cwcli run {project_name} --site {site} "
                    "set-maintenance-mode on"
                ),
            )
        )
    elif not ok:
        warnings.append(
            Message(
                "maintenance.disable_error",
                f"Could not take '{site}' back out of maintenance mode after the interrupt.",
            )
        )


def _sites_with_app(
    project_name: str,
    bench_path: str,
    app: str,
    container,
    warnings: list[Message],
    unreadable: list[str] | None = None,
) -> list[str]:
    """Sites with ``app`` installed: the cache when it knows, else a live query.

    The live query is one exec and one Frappe process for the whole bench
    (``core.bench_read``). A site it could not read is left out, reported in
    ``warnings`` with its cause and, when ``unreadable`` is given, named there: the
    app's code has moved under a site that will not be migrated, which is a failure
    of the run, not a footnote (captain, a staging server, 2026-10-28).
    """
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

    batch = bench_read.read_benches(container, [bench_path], list_apps=True)
    read = batch.benches.get(bench_path)
    if read is None or read.sites is None:
        cause = batch.errors[bench_path] if read is None else "its sites could not be listed"
        warnings.append(
            Message(
                "discover.bench_unreadable",
                f"Could not read the sites of {bench_path} to find those with '{app}' "
                f"installed ({cause}).",
            )
        )
        return []
    found = []
    for site in read.sites:
        if site.list_apps is None:
            if unreadable is not None and site.name not in unreadable:
                unreadable.append(site.name)
            err = str(site.error or "")
            refused = "Access denied" in err or "1045" in err
            warnings.append(
                Message(
                    "db.login_refused" if refused else "discover.site_unreadable",
                    (
                        f"database login refused for site {site.name}: {err}. Its database "
                        "user is probably host-scoped to an address the frappe container no "
                        f"longer has; repair with: cwcli db-access repair {shlex.quote(project_name)} --yes"
                        if refused
                        else f"Could not read the installed apps on site '{site.name}', so it was "
                        f"not checked for '{app}' ({bench_path}: {site.error})."
                    ),
                )
            )
        elif app in [line.strip().split()[0] for line in site.list_apps if line.strip()]:
            found.append(site.name)
    return found


def _tree_is_dirty(container, app_path: str) -> bool:
    """Does the app have local changes? The CONFLICT signal after a failed ``git pull``.

    A pull that failed on a conflict - local uncommitted changes that would be
    overwritten, or a merge that hit conflict markers - leaves ``git status
    --porcelain`` non-empty; a pull that failed on the network or on auth leaves the
    tree clean. So a dirty tree is what distinguishes a conflict ``--force`` can
    resolve by resetting from a failure it cannot. FAILS CLOSED: an unreadable
    status is treated as NOT dirty, so ``--force`` never hard-resets (and discards
    work) on an unknown - the ``core.where`` fail-honest rule, in the safe direction
    for a destructive action.
    """
    exit_code, output = container.exec_run("git status --porcelain", workdir=app_path)
    if exit_code != 0:
        return False
    return bool(_decode(output).strip())


def _force_reset_app(
    container, app: str, app_path: str, *, emit: OnEvent, warnings: list[Message]
) -> str:
    """Hard-reset ``app`` to its upstream, discarding local changes. Returns the status token.

    The ``--force`` recovery for a ``git pull`` that hit a conflict: ``git fetch``
    then ``git reset --hard @{u}`` (``@{u}`` is the SAME remote-tracking branch
    ``git pull`` merges, so no remote/branch name has to be resolved), which also
    aborts an in-progress conflicted merge. DESTRUCTIVE - it discards local changes -
    so the caller runs it ONLY on a real conflict and only when ``--force`` was given.
    Untracked files are left in place: ``git reset --hard`` does not delete them.
    """
    warnings.append(
        Message(
            "app.force_reset",
            f"--force: app '{app}' hit a git conflict, so it is being HARD-RESET to its "
            "remote tracking branch, DISCARDING local changes.",
        )
    )
    emit(
        UpdateStepStart(
            phase="force_reset",
            item=app,
            message=(
                f"--force: '{app}' hit a git conflict; hard-resetting it to its remote "
                "tracking branch and DISCARDING local changes"
            ),
        )
    )
    status = "ok"
    lost: str | None = None
    for cmd in ("git fetch", "git reset --hard @{u}"):
        code, lost = _stream_step(
            container,
            cmd,
            workdir=app_path,
            phase="force_reset",
            item=app,
            emit=emit,
            warnings=warnings,
        )
        if code is None:
            status = "unknown"
            break
        if code != 0:
            status = "failed"
            break
    emit(UpdateStepEnd(phase="force_reset", item=app, status=status, message=lost))
    return status


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
    force_reset_apps: list[str] | None = None,
    failed_maintenance_enable: list[str],
    failed_migrations: list[str],
    unknown_migrations: list[str],
    failed_builds: list[str],
    unknown_builds: list[str],
    failed_cache_clears: list[str],
    failed_website_cache_clears: list[str],
    failed_maintenance_disable: list[str],
    unreadable_sites: list[str] | None = None,
    requirements: str | None = None,
    requirements_cause: list[str] | None = None,
    app_imports: list[resolvers.AppImport] | None = None,
    resync: supervision.ResyncOutcome | None = None,
    bench_left_in_maintenance: bool | None = False,
    preflight: list[PreflightFinding] | None = None,
    preflight_blocked: bool = False,
    data_loss_refused: bool = False,
    data_loss_apps: list[str] | None = None,
    aborted: bool,
) -> UpdateReport:
    """Assemble the report and pre-compute ``ok``.

    ``ok`` is pre-computed here rather than re-derived per frontend, matching the
    ``"ok"`` key `apps`'s other subcommands already emit. An UNKNOWN outcome makes
    ``ok`` false: an unknowable exit code is not a success.

    A failed resynchronisation counts against ``ok`` for the reason the whole change
    exists: an update whose migrated site no longer answers is not a success, and it
    is the one failure mode the user cannot see from the per-phase lists.
    """
    resync = resync or supervision.ResyncOutcome(
        attempted=False, restarted=[], unserved_sites=[], error=None
    )
    app_imports = app_imports or []
    ok = not (
        aborted
        or preflight_blocked
        or data_loss_refused
        or failed_apps
        or unknown_apps
        or failed_maintenance_enable
        or failed_migrations
        or unknown_migrations
        or failed_builds
        or unknown_builds
        or unreadable_sites
        or requirements in ("failed", "unknown")
        or failed_cache_clears
        or failed_website_cache_clears
        or failed_maintenance_disable
        or any(imp.diverged for imp in app_imports)
        or not resync.ok
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
        force_reset_apps=list(force_reset_apps or []),
        failed_maintenance_enable=list(failed_maintenance_enable),
        failed_migrations=list(failed_migrations),
        unknown_migrations=list(unknown_migrations),
        failed_builds=list(failed_builds),
        unknown_builds=list(unknown_builds),
        unreadable_sites=list(unreadable_sites or []),
        requirements=requirements,
        requirements_cause=list(requirements_cause or []),
        failed_cache_clears=list(failed_cache_clears),
        failed_website_cache_clears=list(failed_website_cache_clears),
        failed_maintenance_disable=list(failed_maintenance_disable),
        restarted_processes=list(resync.restarted),
        unserved_sites=list(resync.unserved_sites),
        resync_error=resync.error,
        app_imports=list(app_imports),
        bench_left_in_maintenance=bench_left_in_maintenance,
        preflight=list(preflight or []),
        preflight_blocked=preflight_blocked,
        data_loss_refused=data_loss_refused,
        data_loss_apps=list(data_loss_apps or []),
        aborted=aborted,
        ok=ok,
    )


def _recache(project_name: str, bench_path: str, warnings: list[Message], emit: OnEvent) -> None:
    """Refresh the updated bench's cache so the site discovery that follows is accurate."""
    emit(UpdateStepStart(phase="recache"))
    ok = cache.recache_project(project_name, bench_path=bench_path, warnings=warnings)
    emit(UpdateStepEnd(phase="recache", status="ok" if ok else "failed"))
    if not ok:
        warnings.append(
            Message(
                "recache.failed",
                "Failed to recache project. Site detection may be inaccurate.",
            )
        )


_SITES_WITH_LOCKS = (
    'for d in sites/*/locks; do [ -d "$d" ] || continue; s=${d#sites/}; echo "${s%/locks}"; done'
)
_CLEAR_LOCKS = (
    'for d in "$@"; do for f in "$d"/* "$d"/.[!.]*; do [ -e "$f" ] || continue; '
    'echo "$f"; rm -rf "$f"; done; done'
)


def _unlock_every_site(
    container, bench_path: str, *, project_name: str, emit: OnEvent, warnings: list[Message]
) -> None:
    """Clear the locks folder of EVERY site on the bench (what ``cwcli unlock`` does per
    site) before a core update, and list what was cleared.

    Each site's migrate lock is probed first, whatever the preflight setting: a held
    lock refuses the update before anything runs, and a lock that cannot be checked
    keeps that site's locks folder, so frappe's own lock still guards its migrate."""
    emit(UpdateStepStart(phase="unlock_all", message="Clearing stale locks on every site"))
    exit_code, output = container.exec_run(["sh", "-c", _SITES_WITH_LOCKS], workdir=bench_path)
    text = _decode(output)
    clearable: list[str] = []
    if exit_code == 0:
        for site in (line.strip() for line in text.splitlines()):
            if not site:
                continue
            try:
                held = bench_ops.migrate_lock_held(container, bench_path, site)
            except CwcliError as error:
                warnings.append(
                    Message(
                        "unlock_all.lock_unverified",
                        f"Kept the locks of site '{site}': {error.message}",
                    )
                )
                continue
            if held:
                emit(
                    UpdateStepEnd(
                        phase="unlock_all",
                        status="failed",
                        message=f"site '{site}' holds an active migrate lock",
                    )
                )
                raise CwcliError(
                    ErrorKind.CONFLICT,
                    "site.migrate_locked",
                    f"Site '{site}' holds an active migrate lock, so the core update did "
                    "not start.",
                    hint=(
                        "Wait for the running migrate to finish, then retry. To check: "
                        f"cwcli status {shlex.quote(project_name)}"
                    ),
                )
            clearable.append(site)
        if clearable:
            exit_code, output = container.exec_run(
                ["sh", "-c", _CLEAR_LOCKS, "sh", *(f"sites/{s}/locks" for s in clearable)],
                workdir=bench_path,
            )
            text = _decode(output)
    cleared = [line.strip() for line in text.splitlines() if line.strip()] if clearable else []
    if exit_code != 0:
        warnings.append(
            Message(
                "unlock_all.failed",
                "Could not clear every site's locks before the core update; a stale "
                "document lock can still stop a migrate.",
            )
        )
        emit(UpdateStepEnd(phase="unlock_all", status="failed", message=text[-400:]))
        return
    emit(
        UpdateStepEnd(
            phase="unlock_all",
            status="ok",
            message=(
                f"cleared {len(cleared)} lock{'s' if len(cleared) != 1 else ''}: "
                + ", ".join(cleared)
                if cleared
                else "no locks to clear"
            ),
        )
    )


def _tracked_branch(container, app_path: str, app: str) -> tuple[str, str]:
    """The remote and branch the core reset targets: the app's own remote (``upstream``
    first, as bench clones it) and its checked-out branch, resolved explicitly rather
    than through ``@{u}``, which a ``checkout -B ... FETCH_HEAD`` branch does not have."""
    remote = core_apps._resolve_remote(container, app_path, app)
    code, out = container.exec_run("git rev-parse --abbrev-ref HEAD", workdir=app_path)
    branch = _decode(out).strip() if code == 0 else ""
    if not branch or branch == "HEAD":
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "app.detached_head",
            f"'{app}' is not on a branch, so there is no remote branch to reset it to.",
            hint=f"Check out a release branch first: cwcli apps checkout <project> {app} version-N",
        )
    return remote, branch


def _widen_untracked_refspecs(
    container, bench_path: str, facts: list[dict], emit: OnEvent, warnings: list[Message]
) -> bool:
    """(B) ``git remote set-branches --add <remote> <branch>`` + a fetch of it for every
    app whose remote fetch refspec does not cover its checked-out branch, logged."""
    fixed = False
    for app, remote, branch, shallow in update_preflight.untracked_branches(facts):
        refspec = shlex.quote(f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        q = (
            f"{{ git config --get-all remote.{remote}.fetch | grep -qxF {refspec} || "
            f"git remote set-branches --add {shlex.quote(remote)} {shlex.quote(branch)}; }} && "
            f"git fetch {'--depth=1 ' if shallow else ''}{shlex.quote(remote)} {shlex.quote(branch)}"
        )
        emit(
            UpdateStepStart(
                phase="fix_refspec",
                item=app,
                message=f"'{app}': remote '{remote}' did not fetch '{branch}'; adding it to the fetch refspec",
            )
        )
        code, out = container.exec_run(["sh", "-c", q], workdir=f"{bench_path}/apps/{app}")
        ok = code == 0
        emit(
            UpdateStepEnd(
                phase="fix_refspec",
                item=app,
                status="ok" if ok else "failed",
                message=(
                    f"remote.{remote}.fetch now covers {branch}; {remote}/{branch} fetched"
                    if ok
                    else _decode(out)[-400:]
                ),
            )
        )
        warnings.append(
            Message(
                "preflight.refspec_widened" if ok else "preflight.refspec_widen_failed",
                (
                    f"'{app}': added '{branch}' to remote '{remote}''s fetch refspec "
                    f"(git remote set-branches --add {remote} {branch}) so {remote}/{branch} exists."
                    if ok
                    else f"'{app}': could not add '{branch}' to remote '{remote}''s fetch refspec."
                ),
            )
        )
        fixed = fixed or ok
    return fixed


def _switch_mismatched_apps(
    project_name: str,
    bench_path: str,
    facts: list[dict],
    emit: Callable[[Any], None],
    warnings: list[Message],
) -> bool:
    """(A) ``--fix-branches``: check out the matching ``version-N`` branch in every app
    whose declared frappe-dependencies the bench cannot satisfy, through the same
    ``cwcli apps checkout`` core (dirty-tree guarded; it also tracks the branch)."""
    switched = False
    for app, branch in update_preflight.fixable_branches(facts):
        emit(
            UpdateStepStart(
                phase="fix_branch",
                item=app,
                message=f"--fix-branches: switching '{app}' to {branch} (its declared dependencies do not fit this bench)",
            )
        )
        try:
            result = core_apps.checkout_app(
                project_name, app, branch, bench_path=bench_path, on_event=emit
            )
            ok = result.data is not None and bool(getattr(result.data, "ok", False))
            detail = "; ".join(w.text for w in result.warnings) if not ok else ""
        except CwcliError as exc:
            ok, detail = False, exc.message
        emit(
            UpdateStepEnd(
                phase="fix_branch",
                item=app,
                status="ok" if ok else "failed",
                message=f"switched '{app}' to {branch}" if ok else detail,
            )
        )
        warnings.append(
            Message(
                "preflight.branch_switched" if ok else "preflight.branch_switch_failed",
                (
                    f"--fix-branches: switched '{app}' to {branch}."
                    if ok
                    else f"--fix-branches: could not switch '{app}' to {branch}: {detail}"
                ),
            )
        )
        switched = switched or ok
    return switched


def _reown_dubious_apps(
    frappe_container, bench_path: str, findings: list[PreflightFinding], warnings: list[Message]
) -> bool:
    """``--force``: re-own every app repo git refused for dubious ownership, exactly as
    the per-app path does before its pull. True when at least one was re-owned."""
    reowned = False
    for app in dict.fromkeys(f.app for f in findings if f.code == "git.dubious_ownership"):
        if not app:
            continue
        done, error = core_docker.reown_app_repo_to_frappe(
            frappe_container, f"{bench_path}/apps/{app}"
        )
        if done:
            reowned = True
            warnings.append(
                Message(
                    "app.force_reown",
                    f"--force: re-owned '{app}' repository to the container user to clear "
                    "a git ownership refusal before the core reset.",
                )
            )
        elif error:
            warnings.append(Message("app.force_reown_failed", f"--force: '{app}': {error}"))
    return reowned


_REQUIRED_CODES = frozenset({"app.required_app_missing", "app.required_app_not_installed"})


def _dependency_facts(
    container,
    *,
    bench_path: str,
    apps: list[str],
    sites_filter: list[str] | None,
    ranges: bool,
) -> list[dict]:
    """The dependency tier, for EVERY named app: what its new code declares.

    Each app's tracked branch is fetched (objects and FETCH_HEAD only, never the
    working tree, so a refusal leaves the checkout as it was; the pull that follows
    fetches nothing new) and its ``pyproject.toml``, ``hooks.py`` and ``__init__.py``
    are read at that tip. A required app the bench does not hold, or that a site with
    the app installed lacks, is a blocker: neither bench nor the migrate would fetch
    or install it. ``ranges`` adds the ``[tool.bench.frappe-dependencies]`` check (the
    core path's content tier already makes it for every app on the bench).

    Fail honest: an app whose tip could not be read is ``app.dependencies_unverified``.
    """
    bench = app_deps.read_bench_apps(container, bench_path)
    if bench is None:
        return [
            {
                "code": "app.dependencies_unverified",
                "app": None,
                "error": "the bench's apps could not be read",
            }
        ]
    facts: list[dict] = []
    try:
        sites = sorted(bench_sites.list_sites(container, bench_path) or [])
    except Exception:  # noqa: BLE001 - an unknown site set is reported as unverified
        sites = []
        facts.append(
            {
                "code": "app.dependencies_unverified",
                "app": None,
                "error": "the bench's sites could not be listed",
            }
        )
    if sites_filter:
        sites = [s for s in sites if s in sites_filter]
    installed = core_apps._read_installed_apps(container, bench_path, sites, emit=lambda _e: None)
    unread = [s for s in sites if installed.get(s, (None, None))[0] is None]
    tips: dict[str, tuple[app_deps.Declarations, str]] = {}
    for app in apps:
        if app not in bench:
            continue  # the pull reports app.not_found itself
        app_path = f"{bench_path}/apps/{app}"
        try:
            remote, branch = _tracked_branch(container, app_path, app)
        except CwcliError as error:
            facts.append(
                {"code": "app.dependencies_unverified", "app": app, "error": error.message}
            )
            continue
        code, out = container.exec_run(
            ["git", "fetch", "--quiet", "--no-tags", remote, branch], workdir=app_path
        )
        if code != 0:
            cause = (_decode(out).strip().splitlines() or ["git fetch failed"])[-1]
            facts.append({"code": "app.dependencies_unverified", "app": app, "error": cause})
            continue

        def show(path: str, app_path: str = app_path) -> str | None:
            code, out = container.exec_run(["git", "show", f"FETCH_HEAD:{path}"], workdir=app_path)
            return _decode(out) if code == 0 else None

        decl = app_deps.declarations_from(
            app,
            pyproject=show("pyproject.toml"),
            hooks=show(f"{app}/hooks.py"),
            init=show(f"{app}/__init__.py"),
            branch=branch,
        )
        tips[app] = (decl, f"{remote}/{branch}")
    versions = {name: d.version for name, d in bench.items()}
    versions.update({app: d.version or versions.get(app) for app, (d, _) in tips.items()})
    for app, (decl, ref) in tips.items():
        if not decl.readable:
            facts.append({"code": "app.dependencies_unverified", "app": app, "error": decl.error})
        app_sites = [s for s in sites if app in (installed.get(s, (None, None))[0] or [])]
        branch = ref.split("/", 1)[1]
        if any(app_deps.entry_module(e)[0] not in ("frappe", app) for e in decl.required):
            facts.extend(
                {
                    "code": "app.dependencies_unverified",
                    "app": app,
                    "error": f"the installed apps on {site} could not be read",
                }
                for site in unread
            )
        for entry in decl.required:
            dep, pin = app_deps.entry_module(entry)
            if dep in ("frappe", app):
                continue
            if dep not in bench and not app_deps.find_sources(dep, pin):
                # Catalog-only: --with-deps would hand a bare name to bench's own
                # github.com/frappe|erpnext guess, so this one is never offered it.
                facts.append(
                    {
                        "code": "app.required_app_source_unknown",
                        "app": app,
                        "dep": dep,
                        "ref": ref,
                        "branch": branch,
                    }
                )
                continue
            if dep not in bench:
                facts.append(
                    {
                        "code": "app.required_app_missing",
                        "app": app,
                        "dep": dep,
                        "ref": ref,
                        "branch": branch,
                        "sites": app_sites,
                    }
                )
                continue
            for site in app_sites:
                if dep not in (installed.get(site, (None, None))[0] or []):
                    facts.append(
                        {
                            "code": "app.required_app_not_installed",
                            "app": app,
                            "dep": dep,
                            "site": site,
                            "ref": ref,
                            "branch": branch,
                        }
                    )
        if not ranges:
            continue
        for dep, spec in sorted(decl.ranges.items()):
            if not app_deps.range_parses(spec):
                facts.append(
                    {"code": "app.range_unparseable", "app": app, "dep": dep, "spec": spec}
                )
                continue
            have = versions.get(dep)
            if have and app_deps.simple_spec(spec, have) is False:
                facts.append(
                    update_preflight.dependency_unsatisfied(
                        app, dep, spec, have, versions.get("frappe")
                    )
                )
    return facts


def _install_required(
    project_name: str,
    *,
    bench_path: str,
    facts: list[dict],
    dep_sources: dict[str, str] | None,
    emit: OnEvent,
    warnings: list[Message],
) -> bool:
    """``--with-deps``: fetch and install every required app the facts name, on the
    sites that have its dependent, BEFORE the pull. True when every one landed.

    Each goes through ``core.apps.install_apps`` with the same plan an install gets
    (catalog source, the dependent's branch name first, its own range checked, its own
    required apps first), so the update never grows a second way to fetch an app.
    """
    wanted: dict[str, tuple[set[str], str | None, str]] = {}
    for fact in facts:
        if fact.get("code") not in _REQUIRED_CODES:
            continue
        sites, branch, parent = wanted.get(
            fact["dep"], (set(), fact.get("branch"), str(fact.get("app")))
        )
        sites.update(fact.get("sites") or ([fact["site"]] if fact.get("site") else []))
        wanted[fact["dep"]] = (sites, branch, parent)
    ok = True
    for dep, (sites, branch, parent) in wanted.items():
        emit(
            UpdateStepStart(
                phase="install_required",
                item=dep,
                message=f"Fetching and installing {dep}, which {parent}'s new code requires",
            )
        )

        def relay(event, dep: str = dep) -> None:
            if isinstance(event, core_apps.AppsOutput):
                emit(
                    UpdateOutput(
                        phase="install_required", item=dep, stream=event.stream, text=event.text
                    )
                )

        status, why = "ok", None
        try:
            result = core_apps.install_apps(
                project_name,
                [dep],
                bench_path=bench_path,
                sites=sorted(sites) or None,
                fetch_only=not sites,
                if_not_present=True,
                with_deps=True,
                dep_sources=dep_sources,
                prefer_ref=branch,
                on_event=relay,
            )
            warnings.extend(result.warnings)
            if result.status is Status.NEEDS_CHOICE:
                status, why = "failed", result.choice.prompt if result.choice else "a choice"
            elif result.data is None or not result.data.ok:
                status, why = "failed", f"installing {dep} failed"
        except CwcliError as error:
            status, why = "failed", error.message
        if status != "ok":
            ok = False
            warnings.append(Message("app.required_app_install_failed", f"{dep}: {why}"))
        emit(UpdateStepEnd(phase="install_required", item=dep, status=status, message=why))
    return ok


def _app_checks(
    frappe_container,
    *,
    project_name: str,
    bench: str | None,
    path: str | None,
    bench_path: str,
    apps: list[str],
    sites_filter: list[str] | None,
    fix_branches: bool,
    with_deps: bool,
    dep_sources: dict[str, str] | None,
    emit: OnEvent,
    warnings: list[Message],
) -> tuple[list[PreflightFinding], Result[UpdateReport] | None]:
    """The preflight of an update that resets no core app: the dependency tier alone.

    A plain ``git pull`` cannot lose work the way the core reset can, so the reset's
    predictions do not apply; what does is the new code's own declarations. A blocker
    refuses with nothing pulled, the same report shape the core path returns.
    """
    emit(UpdateStepStart(phase="preflight"))
    started = time.monotonic()

    def check() -> list[dict]:
        return _dependency_facts(
            frappe_container,
            bench_path=bench_path,
            apps=apps,
            sites_filter=sites_filter,
            ranges=True,
        )

    facts = check()
    changed = False
    if fix_branches and update_preflight.fixable_branches(facts):
        changed = _switch_mismatched_apps(project_name, bench_path, facts, emit, warnings)
    if with_deps and any(f.get("code") in _REQUIRED_CODES for f in facts):
        changed = (
            _install_required(
                project_name,
                bench_path=bench_path,
                facts=facts,
                dep_sources=dep_sources,
                emit=emit,
                warnings=warnings,
            )
            or changed
        )
    if changed:
        facts = check()
    findings = update_preflight.classify(facts, project=project_name, bench=bench, path=path)
    blocked = any(f.severity == update_preflight.BLOCKER for f in findings)
    emit(
        UpdatePreflight(
            findings=findings,
            summary=f"{len(apps)} app(s) on {bench_path}: required apps and Frappe ranges at "
            f"the new tip ({time.monotonic() - started:.1f}s)",
            blocked=blocked,
        )
    )
    if not blocked:
        return findings, None
    report = _build_report(
        project_name=project_name,
        bench_path=bench_path,
        apps=apps,
        frappe_reset=False,
        affected=set(),
        migrated=[],
        failed_apps=[],
        unknown_apps=[],
        failed_maintenance_enable=[],
        failed_migrations=[],
        unknown_migrations=[],
        failed_builds=[],
        unknown_builds=[],
        failed_cache_clears=[],
        failed_website_cache_clears=[],
        failed_maintenance_disable=[],
        preflight=findings,
        preflight_blocked=True,
        aborted=False,
    )
    return findings, Result(status=Status.WARNING, data=report, warnings=warnings)


def _frappe_checks(
    frappe_container,
    *,
    project_name: str,
    bench: str | None,
    path: str | None,
    bench_path: str,
    apps: list[str],
    preflight: bool,
    force: bool,
    fix_branches: bool = False,
    with_deps: bool = False,
    dep_sources: dict[str, str] | None = None,
    sites_filter: list[str] | None = None,
    emit: OnEvent,
    warnings: list[Message],
) -> tuple[list[PreflightFinding], list[str], Result[UpdateReport] | None]:
    """The checks before ``bench update --reset``: the findings to report, the apps
    whose local work ``--force`` lets the reset discard, and the refusal to return
    instead of running it.

    ``preflight`` runs every prediction (:mod:`.update_preflight`); without it only
    the local tier runs, for the data-loss guard. The guard is not a prediction, so
    ``--no-preflight`` never skips it and only ``--force`` passes it - the ``apps
    checkout`` dirty-tree rule, where only ``--reset`` passes a dirty tree. ``--force``
    never passes a blocker. It first re-owns any app repo git refuses for dubious
    ownership, then checks again, so a re-owned repo's local work is seen too.

    A refusal is a report, not a raise: it carries EVERY finding (blockers, data
    loss, notices together), which is the point of checking first - bench itself
    stops at the first problem it meets, after backing up every site.
    """
    emit(UpdateStepStart(phase="preflight" if preflight else "local_work"))

    def check() -> update_preflight.PreflightOutcome:
        outcome = update_preflight.run_preflight(
            frappe_container,
            bench_path,
            project=project_name,
            bench=bench,
            path=path,
            local_only=not preflight,
        )
        if not preflight:
            return outcome
        # The dependency tier's required apps; the content tier already checks every
        # app's frappe-dependencies range at its fetched tip.
        extra = _dependency_facts(
            frappe_container,
            bench_path=bench_path,
            apps=apps,
            sites_filter=sites_filter,
            ranges=False,
        )
        if not extra:
            return outcome
        added = update_preflight.classify(extra, project=project_name, bench=bench, path=path)
        return dataclasses.replace(
            outcome,
            facts=outcome.facts + extra,
            findings=update_preflight.sort_findings(outcome.findings + added),
        )

    outcome = check()
    if with_deps and any(f.get("code") in _REQUIRED_CODES for f in outcome.facts):
        if _install_required(
            project_name,
            bench_path=bench_path,
            facts=outcome.facts,
            dep_sources=dep_sources,
            emit=emit,
            warnings=warnings,
        ):
            outcome = check()
    if force and _reown_dubious_apps(frappe_container, bench_path, outcome.findings, warnings):
        outcome = check()
    # On a staging server (2026-10-28), two more failures the preflight now prevents:
    # (B) a branch the remote's fetch refspec does not cover (a narrow clone switched
    #     with `checkout -B ... FETCH_HEAD`): widened in place and logged, always - a
    #     refspec that also covers the checked-out branch is never wrong;
    # (A) an app whose declared frappe-dependencies the bench cannot satisfy: a
    #     refusal naming the switch, performed only when --fix-branches confirms it.
    fixed = _widen_untracked_refspecs(frappe_container, bench_path, outcome.facts, emit, warnings)
    if fix_branches:
        fixed = (
            _switch_mismatched_apps(project_name, bench_path, outcome.facts, emit, warnings)
            or fixed
        )
    if fixed:
        outcome = check()
        # A switch can land on a branch the refspec did not cover until now: widen
        # once more, then the final check.
        if _widen_untracked_refspecs(frappe_container, bench_path, outcome.facts, emit, warnings):
            outcome = check()
    wanted = {a.lower() for a in apps}
    # Only the core apps are reset now, so only THEIR local work is at risk (an
    # unchecked bench - no app named - still refuses: it could be any of them).
    # Another app's local work is a fact worth showing, never a loss: it is
    # re-labelled a notice so the data-loss group lists only what the reset touches.
    relabelled = []
    for f in outcome.findings:
        if f in update_preflight.local_work([f]) and f.app and f.app not in wanted:
            kept = re.split(
                r" would be DISCARDED| that exist on no remote|;", f.message, maxsplit=1
            )[0]
            f = dataclasses.replace(
                f,
                severity=update_preflight.NOTICE,
                message=f"{kept} - not touched by a core update "
                f"(only {', '.join(sorted(wanted))} {'is' if len(wanted) == 1 else 'are'} reset)",
                remedy=None,
            )
        relabelled.append(f)
    at_risk = [f for f in update_preflight.local_work(relabelled) if not f.app or f.app in wanted]
    findings = relabelled if preflight else at_risk
    blocked = preflight and outcome.blocked
    refused = bool(at_risk) and not force
    at_risk_apps = list(dict.fromkeys(f.app for f in at_risk if f.app))
    emit(
        UpdatePreflight(
            findings=findings,
            summary=outcome.summary,
            blocked=blocked or refused,
            local_only=not preflight,
        )
    )
    if not (blocked or refused):
        return findings, at_risk_apps, None
    report = _build_report(
        project_name=project_name,
        bench_path=bench_path,
        apps=apps,
        frappe_reset=True,
        affected=set(),
        migrated=[],
        failed_apps=[],
        unknown_apps=[],
        failed_maintenance_enable=[],
        failed_migrations=[],
        unknown_migrations=[],
        failed_builds=[],
        unknown_builds=[],
        failed_cache_clears=[],
        failed_website_cache_clears=[],
        failed_maintenance_disable=[],
        preflight=findings,
        preflight_blocked=blocked,
        data_loss_refused=refused,
        data_loss_apps=at_risk_apps,
        aborted=False,
    )
    return findings, at_risk_apps, Result(status=Status.WARNING, data=report, warnings=warnings)


# ------------------------------------------------------------------------------ verb


def update(
    project_name: str,
    apps: list[str],
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    path_selector: str | None = None,
    sites: list[str] | None = None,
    clear_cache: bool = False,
    clear_website_cache: bool = False,
    build: bool = False,
    skip_maintenance: bool = False,
    skip_requirements: bool = False,
    no_recache: bool = False,
    force: bool = False,
    preflight: bool = True,
    fix_branches: bool = False,
    with_deps: bool = False,
    dep_sources: dict[str, str] | None = None,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[UpdateReport]:
    """Update ``apps`` and migrate every affected site. See the module docstring.

    After the pull and before any site migrates, the bench's Python requirements are
    reinstalled for EVERY app (``bench setup requirements --python``; the core path
    runs the full ``bench setup requirements``), because a dependency the new code
    needs can come from another app than the one updated - a frappe that gained
    ``pyarrow`` fails the migrate of a site whose update named only a custom app.
    ``skip_requirements`` opts out; the report then says ``requirements="skipped"``.

    Every named app's new tip is read before anything is pulled (``preflight``; the
    dependency tier): a newly required app the bench lacks, or a site lacks, refuses
    the update with nothing pulled, naming ``--with-deps``. ``with_deps`` fetches and
    installs those apps on the affected sites first (through ``core.apps``' install
    plan, ``dep_sources`` answering an ambiguous source), then the pull and migrate
    run as before. A non-core app's new Frappe range that no longer fits the bench is
    ``app.frappe_dependency_unsatisfied``, as it is for the core path, and
    ``fix_branches`` switches it the same way.

    ``fix_branches`` (the core path) lets the preflight SWITCH an app whose declared
    frappe-dependencies the bench cannot satisfy to the matching ``version-N`` branch
    (``cwcli apps checkout``, dirty-tree guarded) before the update; without it such
    a mismatch refuses with the switch named. A branch the remote's fetch refspec
    does not cover is always widened in place and logged, since bench's and cwcli's
    reset both need ``<remote>/<branch>`` to exist.

    ``preflight`` (the frappe path only) checks, before ``bench update --reset``
    starts, everything that would make it fail late - see
    :mod:`.update_preflight`. A blocker returns a report with
    ``preflight_blocked=True`` and nothing run.

    ``force`` on the frappe path accepts the data loss ``bench update --reset``
    causes: without it, any app with uncommitted or unpushed work (or work that
    could not be checked) returns a report with ``data_loss_refused=True`` and
    nothing run, whether or not ``preflight`` is set. It also re-owns app repos git
    refuses for dubious ownership, as it does before the per-app pull.

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
        # 5. The core special-case (frappe, and erpnext when named): the SAME per-site
        # state machine as any app, with these differences - each core app is reset to
        # its remote branch (fetch + reset --hard, the core apps ONLY; never bench's
        # bench-wide `bench update --reset`, which hard-reset every app on the bench,
        # the captain's staging-server defect) after the preflight and the data-loss guard,
        # every site's stale locks are cleared first, and the bench's requirements
        # are reinstalled before the sites migrate. Every site then migrates under
        # cwcli's own per-site maintenance with its `finally`, so a failed migrate
        # names its site and leaves nothing bench-wide behind.
        core_reset = any(app.lower() in CORE_APPS for app in apps)
        findings: list[PreflightFinding] | None = None
        discarding: list[str] | None = None
        if core_reset:
            findings, discarding, refusal = _frappe_checks(
                frappe_container,
                project_name=project_name,
                bench=bench,
                path=path_selector,
                bench_path=bench_path,
                apps=apps,
                preflight=preflight,
                force=force,
                fix_branches=fix_branches,
                with_deps=with_deps,
                dep_sources=dep_sources,
                sites_filter=sites,
                emit=emit,
                warnings=warnings,
            )
            if refusal is not None:
                return refusal
            build = True  # a core update rebuilds assets, as bench's own update does
        elif preflight:
            findings, refusal = _app_checks(
                frappe_container,
                project_name=project_name,
                bench=bench,
                path=path_selector,
                bench_path=bench_path,
                apps=apps,
                sites_filter=sites,
                fix_branches=fix_branches,
                with_deps=with_deps,
                dep_sources=dep_sources,
                emit=emit,
                warnings=warnings,
            )
            if refusal is not None:
                return refusal

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
            skip_requirements=skip_requirements,
            no_recache=no_recache,
            force=force,
            core_reset=core_reset,
            preflight_findings=findings,
            discarding=discarding,
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
    force: bool,
    skip_requirements: bool = False,
    core_reset: bool = False,
    preflight_findings: list[PreflightFinding] | None = None,
    discarding: list[str] | None = None,
    emit: OnEvent,
    warnings: list[Message],
) -> Result[UpdateReport]:
    """The per-app pull + per-site migrate state machine, with its maintenance-mode
    lifecycle. Kept in a PLAIN function so the ``finally`` is unconditional."""
    affected: set[str] = set()
    failed_apps: list[str] = []
    unknown_apps: list[str] = []
    force_reset_apps: list[str] = []  # --force hard-reset these past a conflict
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
    app_imports: list[resolvers.AppImport] = []  # BUG-10 wrong-copy detection
    unreadable_sites: list[str] = []  # could not be opened, so never discovered or migrated
    requirements: str | None = None  # the requirements step's outcome, when it was reached
    requirements_cause: list[str] = []
    resync: supervision.ResyncOutcome | None = None
    aborted = False
    # Marker stamped on every migrate exec so an interrupt can end EXACTLY the process
    # cwcli started before clearing maintenance (see core.bench_ops interrupt cleanup).
    migrate_token = bench_ops.new_migrate_token()
    # Each site's LIVE maintenance state BEFORE the migrate, so an interrupt under
    # --skip-maintenance can RESTORE the operator's pre-state instead of blindly
    # clearing (see _restore_pre_maintenance_on_abort). Empty until recorded below, so
    # an abort before the migrate loop finds nothing to restore.
    pre_maintenance: dict[str, bool | None] = {}

    if core_reset:
        # Captain 2026-10-28: a stale document lock on one site killed a core
        # update mid-migrate; clear every site's locks folder first, listing them.
        _unlock_every_site(
            frappe_container,
            bench_path,
            project_name=project_name,
            emit=emit,
            warnings=warnings,
        )

    try:
        if core_reset:
            unchecked_bench = any(
                not f.app for f in update_preflight.local_work(preflight_findings or [])
            )
            if discarding or unchecked_bench:
                where = (
                    "the core apps, none of whose local work could be checked"
                    if unchecked_bench
                    else ", ".join(discarding or [])
                )
                note = (
                    "--force: the core reset will discard any uncommitted or unpushed work "
                    f"in {where}."
                )
                warnings.append(
                    Message(
                        "frappe_reset.discarding_local_work",
                        note,
                        detail={
                            "apps": list(discarding or []),
                            "all_apps_unchecked": unchecked_bench,
                        },
                    )
                )
                # On the START event so a frontend shows it BEFORE the reset runs.
                emit(UpdateStepStart(phase="core_reset", message=note))
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
            # --force clears a git "dubious ownership" refusal at its root: git run as
            # the frappe user refuses a repo owned by another uid, so BOTH the pull and
            # the `git status` the conflict path relies on fail and plain --force can do
            # nothing. Re-own the app repo (resolving the symlink to the real source,
            # e.g. .hdsrc) to the frappe user BEFORE the pull. Owner-mismatch gated, so
            # an already-correct repo is a cheap stat; failure degrades to the pull's
            # own error (the pull runs regardless).
            if force:
                reowned, reown_err = core_docker.reown_app_repo_to_frappe(
                    frappe_container, app_path
                )
                if reowned:
                    warnings.append(
                        Message(
                            "app.force_reown",
                            f"--force: re-owned '{app}' repository to the container user "
                            "to clear a git ownership refusal before pulling.",
                        )
                    )
                elif reown_err:
                    warnings.append(
                        Message("app.force_reown_failed", f"--force: '{app}': {reown_err}")
                    )
            if core_reset and app.lower() in CORE_APPS:
                # The core app is RESET to its remote branch (what bench's reset does
                # per app), but only this app: fetch, then reset --hard to the SAME
                # remote-tracking branch git pull would merge. Other apps on the
                # bench are not touched.
                status = "ok"
                remote, branch = _tracked_branch(frappe_container, app_path, app)
                q_remote, q_branch = shlex.quote(remote), shlex.quote(branch)
                for cmd in (
                    f"git fetch {q_remote} {q_branch}",
                    f"git reset --hard {q_remote}/{q_branch}",
                ):
                    status = _run_step(
                        frappe_container,
                        cmd,
                        workdir=app_path,
                        phase="reset",
                        item=app,
                        index=i,
                        total=len(apps),
                        emit=emit,
                        warnings=warnings,
                        failed=failed_apps,
                        unknown=unknown_apps,
                    )
                    if status != "ok":
                        break
            else:
                status = _run_step(
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
            # --force ONLY on a real conflict: a failed pull whose tree is dirty is a
            # local-changes conflict a hard-reset to upstream can force through; a
            # clean tree means the pull failed for another reason (network/auth) that
            # discarding work would not fix, so it is left as the failure it is. A
            # successful reset resolves the pull's failure - the app is now AT the
            # upstream tip, so it is un-failed and discovery/migrate proceed for it.
            if status == "failed" and force and _tree_is_dirty(frappe_container, app_path):
                if (
                    _force_reset_app(frappe_container, app, app_path, emit=emit, warnings=warnings)
                    == "ok"
                ):
                    failed_apps.remove(app)
                    force_reset_apps.append(app)

        if any(a not in failed_apps and a not in unknown_apps for a in apps):
            # New code wants its new pins before any site migrates on it (bench's own
            # update reinstalls requirements at this point too). For EVERY app on the
            # bench, not only the updated ones: the new package can come from another
            # app (a frappe that gained pyarrow failed a custom app's migrate on a
            # reported v15 bench). A failure here migrates no site: migrating
            # against half-installed dependencies is worse than not migrating.
            if skip_requirements:
                requirements = "skipped"
                skipped_note = (
                    "Skipped bench setup requirements (--skip-requirements): a Python "
                    "package the updated code needs may be missing when the sites migrate."
                )
                warnings.append(Message("requirements.skipped", skipped_note))
                emit(UpdateStepEnd(phase="requirements_skipped", message=skipped_note))
            else:
                output: list[str] = []

                def _keep_output(event: UpdateEvent) -> None:
                    if isinstance(event, UpdateOutput):
                        output.append(event.text)
                    emit(event)

                requirements = _run_step(
                    frappe_container,
                    # The core path keeps the full step (Node too) it always ran.
                    "bench setup requirements" + ("" if core_reset else " --python"),
                    workdir=bench_path,
                    phase="requirements",
                    item="bench",
                    index=1,
                    total=1,
                    emit=_keep_output,
                    warnings=warnings,
                    failed=[],
                    unknown=[],
                )
                if requirements == "failed":
                    requirements_cause = _failure_cause("".join(output))
            if requirements in ("failed", "unknown"):
                cause = " ".join(line.strip() for line in requirements_cause)
                warnings.append(
                    Message(
                        "requirements.failed",
                        "bench setup requirements did not finish, so no site was migrated "
                        "onto the new code"
                        + (f" ({cause})" if cause else "")
                        + ". Fix that dependency and re-run, or re-run with "
                        "--skip-requirements to migrate without the reinstall.",
                    )
                )

        # An app whose outcome is unknown is not known to have updated, so it is not
        # discovered against - the same treatment a failed pull gets.
        skipped = set(failed_apps) | set(unknown_apps)
        if requirements in ("failed", "unknown"):
            skipped = set(apps)

        # BUG-10: the pull landed on whatever `apps/<app>` physically resolves to;
        # verify the bench actually imports THAT copy. A diverged app was updated in a
        # copy the running site does not load, so it fails the run (via _build_report's
        # `ok`) with a warning naming both paths, instead of a bare "successfully
        # updated". One exec over the updated apps; a probe failure is soft.
        updated_apps = [a for a in apps if a not in skipped]
        if updated_apps:
            imports = resolvers.resolve_app_imports(frappe_container, bench_path, updated_apps)
            app_imports = [imports[a] for a in updated_apps if a in imports]
            warnings.extend(resolvers.app_import_divergence_warnings(app_imports))

        if no_recache:
            emit(UpdateStepStart(phase="recache_skipped"))
        elif len(skipped) < len(apps):
            _recache(project_name, bench_path, warnings, emit)

        for app in apps:
            if app in skipped:
                continue
            emit(UpdateStepStart(phase="discover", item=app))
            sites = _sites_with_app(
                project_name, bench_path, app, frappe_container, warnings, unreadable_sites
            )
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
            unreadable_sites = [s for s in unreadable_sites if s in allowed]
        # Refuse when --site narrows an actually-affected set to empty: that is a
        # typo/mismatch, distinct from "no site has the app at all" (which exits 0).
        # It cannot live in a resolve phase - it needs the discovered sites.
        if sites_filter and unfiltered and not affected and not unreadable_sites:
            raise CwcliError(
                ErrorKind.USAGE,
                "site_filter.matched_nothing",
                "--site matched no affected site(s). "
                f"Requested: {', '.join(sorted(sites_filter))}; "
                f"affected: {', '.join(sorted(unfiltered))}",
            )

        # Record each affected site's LIVE maintenance state BEFORE anything changes
        # it. Under --skip-maintenance an interrupt restores THIS state per site rather
        # than blindly clearing, because that flag means the operator manages
        # maintenance themselves; on the managed path below this is unused (that path
        # always clears). maintenance_mode_on returns True / False / None (unreadable).
        pre_maintenance = {
            site: supervision.maintenance_mode_on(frappe_container, bench_path, site)
            for site in sorted(affected)
        }

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
                environment=bench_ops.migrate_env(migrate_token),
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
        # Records that the fan-out stopped early, then re-raises untouched.
        # BaseException (not Exception) so a Ctrl-C is reported the same way.
        aborted = True
        # An interrupt (Ctrl+C / SIGTERM / SIGHUP unwind) leaves the in-container
        # migrate running orphaned. End it FIRST - and wait for it to exit - so the
        # maintenance clear in the finally is the LAST write; on v15+ the orphan would
        # otherwise re-assert maintenance behind us and strand the site at 503. Only a
        # process carrying THIS invocation's marker is signalled, never a name pattern.
        # Keyed on sites_to_migrate (a migrate was actually launched), so it also ends
        # the orphan under --skip-maintenance, where bench migrate still self-manages
        # maintenance on v15+ even though cwcli enabled none.
        if sites_to_migrate and not bench_ops.end_migrate_on_interrupt(
            frappe_container, migrate_token
        ):
            emit(
                UpdateStepEnd(
                    phase="interrupt_cleanup",
                    status="unknown",
                    message=bench_ops.interrupt_recheck_hint(
                        project_name, ", ".join(sites_to_migrate)
                    ),
                )
            )
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
                    # LAST write, and on the abort path the settle/re-check (recheck=
                    # aborted) catches any late re-assert by the just-ended orphan
                    # migrate, so the dying migrate cannot win. On the normal path the
                    # migrate has already exited, so it is a plain disable.
                    ok = bench_ops.clear_maintenance(
                        frappe_container, bench_path, site, recheck=aborted
                    )
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
        elif skip_maintenance and aborted and sites_to_migrate:
            # Under --skip-maintenance cwcli enabled no maintenance, so the disable
            # loop above does not run - but the except block ENDED the orphaned migrate
            # that on v15+ would have cleared maintenance as it finished. Restore each
            # migrated site's pre-migrate state (was-off -> cleared, was-on -> left on
            # with a note, unknown -> cleared with a note), so an interrupted
            # --skip-maintenance update never silently strands a site at 503 yet never
            # overrides an operator who deliberately put a site into maintenance.
            for site in sites_to_migrate:
                try:
                    _restore_pre_maintenance_on_abort(
                        frappe_container,
                        bench_path,
                        site,
                        pre_maintenance.get(site),
                        project_name=project_name,
                        emit=emit,
                        warnings=warnings,
                    )
                except Exception as e:  # noqa: BLE001 - a stuck site must still be reported
                    warnings.append(
                        Message(
                            "maintenance.disable_error",
                            f"Failed to restore maintenance mode for '{site}': {e}",
                        )
                    )

        # Steering 041: the core-update postflight - a site whose database user is
        # scoped to an address the container no longer has reads as a migrate
        # failure or a 500; name it as a login refusal with the repair.
        if core_reset and sites_to_migrate and not aborted:
            from . import db_access

            refused = db_access.postflight(
                frappe_container,
                bench_path,
                project=project_name,
                sites=sites_to_migrate,
                bench_flag=resolvers.bench_cli_flag(project_name, bench_path),
            )
            if refused is not None:
                warnings.append(refused)

        # THE RESYNC, and its position is the whole subtlety. `git pull` moved the
        # code under processes that were already running, so they hold the pre-pull
        # interpreter exactly as an install does. It has to come AFTER the
        # maintenance-disable above, because a site in maintenance mode answers 503
        # and the proof this step exists to give could never be obtained.
        #
        # Skipped on an abort: the maintenance-disable is the only cleanup a Ctrl-C
        # owes, and adding a restart plus a bounded site wait to that path would make
        # an interrupt take another minute to return. `aborted` already tells the
        # caller the run is half-applied, which covers "the processes may be stale".
        if not aborted and sites_to_migrate:
            resync = supervision.resync_after_code_change(
                frappe_container,
                bench_path,
                sites=sites_to_migrate,
                on_restart=lambda program: emit(UpdateStepStart(phase="resync", item=program)),
            )
            if resync.error:
                warnings.append(
                    Message("resync.failed", f"The update completed, but {resync.error}")
                )
            if resync.attempted:
                emit(
                    UpdateStepEnd(
                        phase="resync",
                        status="ok" if resync.ok else "failed",
                        message=resync.error,
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
            frappe_reset=core_reset,
            preflight=preflight_findings,
            data_loss_apps=discarding,
            affected=affected,
            migrated=sites_to_migrate,
            failed_apps=failed_apps,
            unknown_apps=unknown_apps,
            force_reset_apps=force_reset_apps,
            failed_maintenance_enable=failed_maintenance_enable,
            failed_migrations=failed_migrations,
            unknown_migrations=unknown_migrations,
            failed_builds=failed_builds,
            unknown_builds=unknown_builds,
            failed_cache_clears=failed_cache_clears,
            failed_website_cache_clears=failed_website_cache_clears,
            failed_maintenance_disable=failed_maintenance_disable,
            unreadable_sites=unreadable_sites,
            requirements=requirements,
            requirements_cause=requirements_cause,
            app_imports=app_imports,
            resync=resync,
            aborted=aborted and bool(sites_to_migrate),
        )
        if aborted:
            # The exception is still unwinding, so this report can never be RETURNED.
            # Hand it over before it is lost: a stuck site must keep its remediation
            # even when the run ends in a Ctrl-C.
            emit(UpdateAborted(report=report))

    return Result(status=Status.OK if report.ok else Status.WARNING, data=report, warnings=warnings)
