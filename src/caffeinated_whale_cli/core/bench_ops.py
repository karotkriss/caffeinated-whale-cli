"""``core.bench_ops`` - standalone ``bench migrate``/``run-tests``/``build`` as UI-pure logic.

The commands every Frappe proof runs, none of which existed as a callable
operation before this module (openspec ``add-axi-bench-exec-verbs``). ``bench
migrate`` lived only inside :func:`core.update.update`'s pull-and-fan-out state
machine and inside ``restore_apply``'s post-restore step; ``bench run-tests`` and
``bench build`` existed nowhere at all.

Both functions own their resolution and container I/O and RETURN a typed report.
They print, prompt and exit nothing: a stopped container or an ambiguous
multi-bench project comes back as ``NEEDS_CHOICE``, hard failures raise
:class:`~.errors.CwcliError`.

Four things here are deliberate and load-bearing:

- **A migrate resolves EXACTLY ONE site and never fans out.** This is the whole
  point of the module. ``apps update`` migrates every site it discovers to have
  the named app installed, which is right when the APP is the subject; here the
  SITE is the subject, and an agent that ran ``migrate <project>`` must never
  discover it migrated four sites. The resolved site is carried in the report so
  the caller can read back what it actually acted on.
- **Maintenance mode is a GATE, not a courtesy.** ``core/update.py`` calls its
  equivalent "THE LOAD-BEARING GATE" and refuses to migrate a site it could not
  put into maintenance. A standalone migrate that skipped it would reach the same
  schema mutation with strictly fewer protections than the path that already
  exists, which is indefensible for a verb whose purpose is a narrower, safer
  route. There is deliberately NO ``--skip-maintenance`` escape hatch (captain
  ruling M1): ``apps update``'s exists for long multi-site runs where an operator
  accepted the trade knowingly, and a flag that removes the gate has no named
  beneficiary here.
- **Plain functions with an optional ``on_event`` callback, NOT generators.**
  :func:`migrate_site` has a ``try/finally`` that disables maintenance mode, and
  that is exactly the cleanup ``core.update`` proved does not run when an
  abandoned generator lands in a reference cycle. Here the hazard is real, not
  merely shape-consistency: an abandoned generator would leave a site DOWN.
- **A held migrate lock is diagnosed before maintenance mode.** The probe uses
  the same non-blocking ``flock`` primitive as frappe's ``filelock()``; mere file
  presence never refuses a migrate.

``set_maintenance`` lives here, and ``core.update`` imports it. It was private to
``core/update.py`` until this module became its second caller; it was PROMOTED
rather than copied, because two copies of a maintenance-mode lifecycle drift until
one of them stops disabling, and a site stuck in maintenance is a site that is down.
"""

from __future__ import annotations

import json
import secrets
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import resolvers, supervision
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_capture, exec_stream

_MIGRATE_LOCK_CONFLICT_EXIT_CODE = 200

# The env var cwcli stamps on every migrate exec so interrupt cleanup can find EXACTLY
# the process tree it started - never a name pattern that could match another site's or
# another user's migrate. A marker, not a secret; it is inherited by every child.
_MIGRATE_MARKER_ENV = "CWCLI_MIGRATE_TOKEN"

# Interrupt-cleanup timings. cwcli runs these while it is unwinding to exit on Ctrl+C /
# SIGTERM / SIGHUP, so the user is waiting: keep them short. After SIGTERM the orphan
# gets a bounded window to exit on its own (bench migrate handles SIGTERM and unwinds);
# if it does not, SIGKILL and a shorter window.
_INTERRUPT_TERM_TIMEOUT = 10.0
_INTERRUPT_KILL_TIMEOUT = 5.0
_INTERRUPT_POLL_INTERVAL = 0.25
# After clearing maintenance on the interrupt path, settle briefly then re-read the
# LIVE flag: a migrate dying just after our clear can re-assert it, and this backstop
# catches that last write so it cannot win.
_MAINTENANCE_SETTLE = 1.0

# ------------------------------------------------------------------------------ DTOs


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpResult:
    """One step's outcome. ``message`` carries a reason only when there IS one."""

    action: str  # "maintenance_on" | "migrate" | "maintenance_off" | "run-tests" | "build"
    ok: bool
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpReport:
    """The outcome of one bench operation against one resolved site.

    Deliberately NOT :class:`core.apps.AppsReport`: that DTO is ``(app, site,
    action, ok)`` frozen to the shape the human ``apps --json`` output already
    carried, and a migrate has no app, so ``app`` would carry a placeholder that
    lies. ``add-axi-apps-checkout-verb`` Decision 3 is binding on not bolting
    fields onto the shared mutation report.

    ``site`` is the RESOLVED site - reading back what was acted on is the gap
    ``apps checkout`` left open and this deliberately does not repeat. It is
    populated on every site-scoped op and is None ONLY on :func:`build_assets`,
    which acts on the bench and not on any site; naming a site there would state
    something untrue, which is the same rule that keeps ``app`` off a migrate.
    """

    project: str
    bench_path: str
    site: str | None
    app: str | None = None  # run-tests/build only; a migrate is not app-scoped
    results: list[BenchOpResult] = field(default_factory=list)
    ok: bool = True
    # A site we could not take back OUT of maintenance mode is a site left DOWN.
    # It is reported explicitly rather than inferred from a failed step, because it
    # is the one failure the agent must act on even when the migrate itself worked.
    maintenance_left_on: bool = False


# ---------------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpCommand:
    """A command is about to run: the stderr echo, verbatim."""

    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpOutput:
    """A chunk of the command's own output, tagged with the stream it came from."""

    action: str
    stream: str  # "stdout" | "stderr", carried through from ExecChunk
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpStepEnd:
    """A step finished. ``ok`` is the honest per-step outcome."""

    action: str
    ok: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpNotice:
    """A plain-language message to surface in run order (interrupt cleanup only today).

    Carries the one thing a Ctrl+C / SIGTERM cannot say through the returned report
    (there is none - the exception is unwinding): that cwcli could not confirm the
    in-container migrate stopped, and how to re-check and clear the site.
    """

    message: str


BenchOpEvent = BenchOpCommand | BenchOpOutput | BenchOpStepEnd | BenchOpNotice
OnEvent = Callable[[BenchOpEvent], None]


def _noop(_event: BenchOpEvent) -> None:
    """The drain-and-discard consumption mode: what a non-narrating caller passes."""


# --------------------------------------------------------------------------- helpers


def set_maintenance(container, bench_path: str, site: str, *, enable: bool) -> bool:
    """Turn maintenance mode on/off for one site. True when bench accepted it.

    Promoted out of ``core/update.py``'s private ``_set_maintenance`` when this
    module became its second caller (openspec ``add-axi-bench-exec-verbs``). A MOVE,
    not a rewrite: behaviour is byte-identical and ``core.update`` imports this one.
    Two copies of this lifecycle drift until one stops disabling, and a site stuck
    in maintenance mode is a site that is down.
    """
    mode = "on" if enable else "off"
    cmd = f"bench --site {shlex.quote(site)} set-maintenance-mode {mode}"
    exit_code, _ = container.exec_run(cmd, workdir=bench_path)
    return bool(exit_code == 0)


def _run_step(
    container,
    cmd: str,
    bench_path: str,
    *,
    action: str,
    emit: OnEvent,
    environment: dict[str, str] | None = None,
) -> int:
    """Run one bench command, narrating it as events. Returns the honest exit code.

    Every chunk is emitted rather than written anywhere: the frontend picks its
    consumption mode. The command's OWN bytes matter here more than anywhere else
    on this surface - neither a migrate nor a test run is a supervised process, so
    neither writes to a log ``cwcli logs`` can serve afterwards, and the name of the
    patch that blew up (or the assertion that failed) exists ONLY in those bytes.

    ``environment`` rides through to the exec (``migrate`` stamps its interrupt marker
    there so cleanup can find the process cwcli started; see :func:`migrate_env`).
    """
    emit(BenchOpCommand(command=cmd))
    exit_code = 1
    for event in exec_stream(container, cmd, workdir=bench_path, environment=environment):
        if isinstance(event, ExecChunk):
            emit(BenchOpOutput(action=action, stream=event.stream, text=event.text))
        else:
            exit_code = event.exit_code
    emit(BenchOpStepEnd(action=action, ok=exit_code == 0))
    return exit_code


# --------------------------------------------- interrupt cleanup (migrate/apps update)
#
# The ONE place ``migrate`` and ``apps update`` both route their "end what I started,
# then clear maintenance LAST" cleanup, so the two maintenance-owning verbs cannot
# drift apart. cwcli launches ``bench migrate`` as a docker exec; Docker has no
# kill-exec API and closing the exec socket does NOT kill the process, so on an
# interrupt (Ctrl+C / SIGTERM / SIGHUP) that migrate keeps running orphaned. On Frappe
# v15+ ``bench migrate`` manages maintenance mode ITSELF, so a surviving orphan
# re-asserts maintenance AFTER cwcli clears it and then dies without clearing it,
# leaving the site stuck at HTTP 503 with nothing to clear it. The remedy is to end
# the process cwcli started FIRST, then clear maintenance last with a settle/re-check.


def new_migrate_token() -> str:
    """A unique per-invocation marker for the migrate exec(s) cwcli launches.

    Rides the exec's environment as ``CWCLI_MIGRATE_TOKEN`` (see :func:`migrate_env`)
    and is inherited by every child process, so :func:`end_migrate_on_interrupt` finds
    EXACTLY the process tree cwcli started - never a name pattern that could match
    another site's or another user's migrate. A marker, not a secret.
    """
    return secrets.token_hex(16)


def migrate_env(token: str) -> dict[str, str]:
    """The environment to hand a migrate exec so it carries the interrupt marker."""
    return {_MIGRATE_MARKER_ENV: token}


def _signal_marked(container, token: str, *, sig: str) -> None:
    """Send ``sig`` to every in-container process whose environ carries ``token``.

    Scans ``/proc/<pid>/environ`` for the exact ``CWCLI_MIGRATE_TOKEN=<token>`` record
    (``grep -a`` reads the NUL-separated environ as text, ``-z`` splits on NUL, ``-F``
    fixed string). Runs as the container's default user - the SAME user the migrate
    runs as - so it can read the environ and signal the process. Best-effort: a process
    that already exited, or a container that has gone away, is fine.
    """
    quoted = shlex.quote(f"{_MIGRATE_MARKER_ENV}={token}")
    script = (
        "for d in /proc/[0-9]*; do "
        f'grep -aqzF -- {quoted} "$d/environ" 2>/dev/null && '
        f'kill -{sig} "${{d##*/}}" 2>/dev/null; '
        "done"
    )
    try:
        container.exec_run(["sh", "-c", script])
    except Exception:  # noqa: BLE001 - best-effort; a failed kill must not abort cleanup
        pass


def _marked_process_alive(container, token: str) -> bool:
    """True while any in-container process still carries ``token`` (the migrate tree
    cwcli started, and its children, which inherited the env).

    Fail-honest toward 'gone': an unreadable ``/proc`` or a lost container returns
    False so cleanup never blocks - the settle/re-check backstop in
    :func:`clear_maintenance` still guards a stray late write.
    """
    quoted = shlex.quote(f"{_MIGRATE_MARKER_ENV}={token}")
    script = (
        "for d in /proc/[0-9]*; do "
        f'grep -aqzF -- {quoted} "$d/environ" 2>/dev/null && exit 0; '
        "done; exit 1"
    )
    try:
        exit_code, _ = container.exec_run(["sh", "-c", script])
    except Exception:  # noqa: BLE001 - can't tell means don't block cleanup
        return False
    return bool(exit_code == 0)


def _wait_marked_gone(container, token: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _marked_process_alive(container, token):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_INTERRUPT_POLL_INTERVAL)


def end_migrate_on_interrupt(container, token: str) -> bool:
    """Interrupt-cleanup step ONE: make sure the migrate cwcli started is GONE before
    maintenance mode is cleared. Returns True iff it is confirmed gone.

    Terminates it precisely - only a process carrying THIS token - and waits for it to
    exit, escalating SIGTERM -> SIGKILL. Returning False means cwcli could not confirm
    it stopped, and the caller says so plainly and names the re-check command (see
    :func:`interrupt_recheck_hint`). This must run BEFORE :func:`clear_maintenance`, so
    the clear is the LAST write and the orphan cannot re-assert maintenance behind it.
    """
    _signal_marked(container, token, sig="TERM")
    if _wait_marked_gone(container, token, timeout=_INTERRUPT_TERM_TIMEOUT):
        return True
    _signal_marked(container, token, sig="KILL")
    return _wait_marked_gone(container, token, timeout=_INTERRUPT_KILL_TIMEOUT)


def clear_maintenance(container, bench_path: str, site: str, *, recheck: bool) -> bool:
    """Disable maintenance mode for ``site`` - the LAST cleanup step - and report
    whether the site ends OUT of maintenance.

    On the normal path (``recheck`` False) the migrate has already exited, so one
    disable is enough and the settle is skipped. On the interrupt path (``recheck``
    True), after :func:`end_migrate_on_interrupt` has stopped the orphan, this settles
    briefly and re-reads the LIVE flag, disabling once more if a late write by the
    dying migrate re-asserted it - so the dying migrate's last write cannot win.
    """
    ok = set_maintenance(container, bench_path, site, enable=False)
    if not recheck:
        return ok
    time.sleep(_MAINTENANCE_SETTLE)
    state = supervision.maintenance_mode_on(container, bench_path, site)
    if state:
        ok = set_maintenance(container, bench_path, site, enable=False)
        state = supervision.maintenance_mode_on(container, bench_path, site)
    if state is None:
        return ok  # could not read it back; trust bench's exit code
    return not state


def interrupt_recheck_hint(project: str, site: str, bench: str | None = None) -> str:
    """The plain-language warning when cwcli could not confirm the orphaned migrate
    stopped: it may still be running, so name how to re-check and clear the site."""
    bench_flag = f" --bench {bench}" if bench else ""
    return (
        f"cwcli was interrupted and could not confirm the in-container 'bench migrate' "
        f"for '{site}' stopped; it may still be running. Re-check with "
        f"'cwcli status {project}'. If '{site}' is left in maintenance mode (HTTP 503), "
        f"clear it with: cwcli run {project} --site {site}{bench_flag} "
        "set-maintenance-mode off"
    )


def _migrate_lock_held(container, bench_path: str, site: str) -> bool:
    """True when frappe's OWN migrate lock is genuinely held by a live process.

    ``bench migrate`` wraps its whole run in ``frappe.utils.synchronization.
    filelock("bench_migrate", timeout=1)``, an ``fcntl``-based advisory lock on
    ``{bench_path}/sites/{site}/locks/bench_migrate.lock``.

    This probes with ``flock -n -E 200`` - the SAME kernel primitive frappe's own
    ``filelock()`` acquires - rather than checking whether the lock FILE exists.
    Exit 200 alone means the identical lock is held. Exit 0 means it is free,
    including when an unheld lock file remains. Any other exit is an unreadable
    lock state and raises ``CwcliError(PRECONDITION)``.
    """
    locks_dir = f"{bench_path}/sites/{site}/locks"
    exit_code, _ = container.exec_run(["test", "-d", locks_dir])
    if exit_code != 0:
        return False  # never migrated yet: nothing to hold a lock

    lock_file = f"{locks_dir}/bench_migrate.lock"
    exit_code, _ = container.exec_run(
        [
            "flock",
            "-n",
            "-E",
            str(_MIGRATE_LOCK_CONFLICT_EXIT_CODE),
            lock_file,
            "-c",
            "true",
        ]
    )
    if exit_code == 0:
        return False
    if exit_code == _MIGRATE_LOCK_CONFLICT_EXIT_CODE:
        return True
    raise CwcliError(
        ErrorKind.PRECONDITION,
        "migrate.lock_probe_failed",
        (
            f"Could not determine whether migrate lock "
            f"'sites/{site}/locks/bench_migrate.lock' is held "
            f"(flock exited {exit_code})."
        ),
        hint="Check that flock is installed and the lock path is accessible, then retry.",
    )


def _resolve_site(
    container,
    project_name: str,
    bench_path: str,
    site: str | None,
    warnings: list[Message],
) -> str:
    """Resolve EXACTLY ONE site and prove it exists. Never a set, never a fan-out.

    An explicit ``site`` is used verbatim; omitting it falls back to the bench's
    default site (the ``backup``/``unlock`` convention). Either way the site
    directory is probed, so a typo is a typed ``NOT_FOUND`` rather than a cryptic
    bench failure halfway through a mutation.
    """
    resolvers.validate_bench_path(bench_path)
    if site is None:
        # The configured default, else the sole site of a single-site bench (a
        # fresh `cwcli init` sets no default), else a typed refusal listing the
        # sites for a multi-site bench.
        site = resolvers.resolve_sole_or_require_site(project_name, bench_path)
        warnings.append(Message("site.default_used", f"No --site given; using '{site}'."))
    resolvers.validate_site_name(site)
    resolvers.require_bench_dir(container, bench_path)
    resolvers.require_site_dir(container, bench_path, site)
    return site


# --------------------------------------------------------------------------- migrate


def migrate_site(
    project_name: str,
    *,
    site: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[BenchOpReport]:
    """Run ``bench migrate`` against EXACTLY ONE site, under maintenance mode.

    The gap ``apps update`` leaves: it migrates only as the tail of a ``git pull``
    across every named app, so an agent that has just pinned a feature branch with
    ``apps checkout`` cannot migrate without a pull that moves the ref it pinned.

    **Blast radius**: this applies every pending schema patch from every installed
    app to the site's live database. It ALTERs tables and executes patch code the
    apps ship. It is not transactional across patches, so a patch that fails partway
    leaves the database partially migrated and cwcli has no rollback. Recovery is
    from a backup - which is why the caller's failure hint points at ``cwcli backup``.

    The maintenance-mode gate is not optional: a site that cannot be put into
    maintenance is NOT migrated, and a site that cannot be taken back out is
    reported via ``maintenance_left_on`` and fails the operation.

    A THIRD gate runs first, before maintenance mode is touched: a site whose
    migrate lock is genuinely held (:func:`_migrate_lock_held`) is refused,
    naming ``cwcli unlock`` as the remedy.
    """
    emit: OnEvent = on_event or _noop

    resolved = resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )
    if isinstance(resolved, Result):
        return resolved
    container, path, warnings = resolved

    target = _resolve_site(container, project_name, path, site, warnings)

    results: list[BenchOpResult] = []
    maintenance_left_on = False

    if _migrate_lock_held(container, path, target):
        # Name the exact cause and remedy before maintenance mode is touched.
        bench_flag = f" --bench {bench}" if bench else ""
        message = (
            f"Site '{target}' already has an active migrate lock "
            f"(sites/{target}/locks/bench_migrate.lock); the migrate was NOT run. "
            "This is expected if another migration is genuinely in progress right "
            "now - wait for it to finish. If nothing is actually running, the lock "
            "is stranded from an earlier interrupted migration; clear it with: "
            f"cwcli unlock {project_name} --site {target}{bench_flag}"
        )
        emit(BenchOpStepEnd(action="lock_check", ok=False))
        results.append(BenchOpResult(action="lock_check", ok=False, message=message))
        return Result(
            status=Status.WARNING,
            data=BenchOpReport(
                project=project_name,
                bench_path=path,
                site=target,
                results=results,
                ok=False,
            ),
            warnings=warnings,
        )

    if not set_maintenance(container, path, target, enable=True):
        # The gate. No migrate is issued at all - this is a REFUSAL, not a failure
        # partway through, and the report says so rather than leaving the agent to
        # infer it from a missing row.
        emit(BenchOpStepEnd(action="maintenance_on", ok=False))
        results.append(
            BenchOpResult(
                action="maintenance_on",
                ok=False,
                message=(
                    f"Could not put site '{target}' into maintenance mode; "
                    "the migrate was NOT run."
                ),
            )
        )
        return Result(
            status=Status.WARNING,
            data=BenchOpReport(
                project=project_name,
                bench_path=path,
                site=target,
                results=results,
                ok=False,
            ),
            warnings=warnings,
        )

    emit(BenchOpStepEnd(action="maintenance_on", ok=True))
    results.append(BenchOpResult(action="maintenance_on", ok=True))

    # Stamp the migrate exec with a unique marker so an interrupt can end EXACTLY the
    # process cwcli started before clearing maintenance (see end_migrate_on_interrupt).
    token = new_migrate_token()
    interrupted = False

    try:
        code = _run_step(
            container,
            f"bench --site {shlex.quote(target)} migrate",
            path,
            action="migrate",
            emit=emit,
            environment=migrate_env(token),
        )
        results.append(BenchOpResult(action="migrate", ok=code == 0))
    except BaseException:
        # An interrupt (Ctrl+C / SIGTERM / SIGHUP unwind) leaves the in-container
        # migrate running orphaned. End it FIRST - and wait for it to exit - so the
        # maintenance clear in the finally is the LAST write; on v15+ the orphan would
        # otherwise re-assert maintenance behind us and strand the site at 503.
        interrupted = True
        if not end_migrate_on_interrupt(container, token):
            emit(BenchOpNotice(message=interrupt_recheck_hint(project_name, target, bench)))
        raise
    finally:
        # Plain function, NOT a generator, precisely so this runs: an abandoned
        # generator's finally does not, and the cost here is a site left down. On the
        # interrupt path the orphan has already been ended above, so the settle/re-check
        # (recheck=True) catches any last write by the dying migrate.
        if clear_maintenance(container, path, target, recheck=interrupted):
            emit(BenchOpStepEnd(action="maintenance_off", ok=True))
            results.append(BenchOpResult(action="maintenance_off", ok=True))
        else:
            maintenance_left_on = True
            emit(BenchOpStepEnd(action="maintenance_off", ok=False))
            results.append(
                BenchOpResult(
                    action="maintenance_off",
                    ok=False,
                    message=(
                        f"Site '{target}' is STILL in maintenance mode and is not serving. "
                        "Take it out with: cwcli run "
                        f"{project_name} --site {target} set-maintenance-mode off"
                    ),
                )
            )

    ok = all(r.ok for r in results)
    return Result(
        status=Status.OK if ok else Status.WARNING,
        data=BenchOpReport(
            project=project_name,
            bench_path=path,
            site=target,
            results=results,
            ok=ok,
            maintenance_left_on=maintenance_left_on,
        ),
        warnings=warnings,
    )


# ------------------------------------------------------------------------- run-tests


def run_tests(
    project_name: str,
    *,
    site: str,
    app: str,
    module: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[BenchOpReport]:
    """Run ``bench run-tests --app <app>`` against ONE named site.

    **Blast radius**: this imports and executes the app's OWN test modules inside
    the container, against the named site's live database. cwcli cannot bound what
    that code does, because it IS the repository's code - a Frappe test suite
    creates, modifies and deletes records. The honest statement is *arbitrary Python
    from the repository under test, executed against a live site*.

    That is why ``site`` and ``app`` are REQUIRED parameters with no default (captain
    ruling S1), diverging deliberately from the ``--site``-defaults-to-the-default-
    site convention that ``backup``, ``unlock`` and :func:`migrate_site` all follow.
    For those three cwcli can state exactly what the operation does to the site; here
    it cannot, and when the effect is unbounded, defaulting the target is the wrong
    default. Requiring both makes "I am willing for THIS site to be written to" an
    explicit act, at a cost of one flag. A bare ``bench run-tests`` would also run
    every installed app's suite including ``frappe``'s own, so the scope is named too.

    This is NOT the ``axi run``/``axi exec`` passthrough that stays deferred: the
    caller SELECTS an app whose tests already exist in the bench, it does not AUTHOR
    a command string. There is no parameter through which a second command can be
    expressed.

    ``module`` narrows the run to ONE dotted test module (``bench run-tests --app X
    --module X.tests.test_y``). It only ever REDUCES what executes, so it needs no
    guard of its own; without it the single-module iteration every fix loop runs had
    no form here and dropped to a raw ``cwcli run``. ``app`` stays required even
    with it, so the report always names the scope that was under test.

    No maintenance mode: a test run is not a schema mutation, and putting a site
    into maintenance would change the conditions the suite runs under.
    """
    emit: OnEvent = on_event or _noop

    resolved = resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )
    if isinstance(resolved, Result):
        return resolved
    container, path, warnings = resolved

    target = _resolve_site(container, project_name, path, site, warnings)
    if not app or not app.strip():
        raise CwcliError(ErrorKind.USAGE, "app.empty", "App name cannot be empty.")

    # A typo'd app is left to bench's own error, forwarded verbatim to the caller's
    # narrator - the `apps checkout` lesson: a redundant cwcli-side pre-check drifts
    # from the tool's real answer and would over-reject targets bench accepts.
    command = f"bench --site {shlex.quote(target)} run-tests --app {shlex.quote(app)}"
    if module and module.strip():
        command += f" --module {shlex.quote(module.strip())}"
    code = _run_step(container, command, path, action="run-tests", emit=emit)
    results = [BenchOpResult(action="run-tests", ok=code == 0)]

    return Result(
        status=Status.OK if code == 0 else Status.WARNING,
        data=BenchOpReport(
            project=project_name,
            bench_path=path,
            site=target,
            app=app,
            results=results,
            ok=code == 0,
        ),
        warnings=warnings,
    )


# ----------------------------------------------------------------------------- build


def build_assets(
    project_name: str,
    *,
    app: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[BenchOpReport]:
    """Run ``bench build`` for the bench, optionally scoped to ONE app.

    The third member of the proof loop `migrate` and `run_tests` already cover: an
    app whose JS/CSS changed is not visibly changed until its assets are rebuilt,
    so a checkout of a front-end-bearing branch left no callable way to finish the
    job and dropped to a raw ``cwcli run``.

    It is the LEAST dangerous op in this module and is guarded accordingly - which
    is to say barely, because there is little to guard. A build compiles the bench's
    own asset sources and writes the result under ``sites/assets``; it touches no
    database, runs no patch, and needs no site, so there is no maintenance gate here
    and nothing to fail closed over. ``--app`` only narrows it.

    That is also why :class:`BenchOpReport`'s ``site`` is ``None`` on this path
    alone. Populating it with the default site would state that a site was acted
    on, and none was; the module's own rule is that the report never carries a
    placeholder that lies.
    """
    emit: OnEvent = on_event or _noop

    resolved = resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )
    if isinstance(resolved, Result):
        return resolved
    container, path, warnings = resolved

    if app is not None and not app.strip():
        raise CwcliError(ErrorKind.USAGE, "app.empty", "App name cannot be empty.")

    command = "bench build"
    if app:
        command += f" --app {shlex.quote(app.strip())}"
    code = _run_step(container, command, path, action="build", emit=emit)

    return Result(
        status=Status.OK if code == 0 else Status.WARNING,
        data=BenchOpReport(
            project=project_name,
            bench_path=path,
            site=None,
            app=app,
            results=[BenchOpResult(action="build", ok=code == 0)],
            ok=code == 0,
        ),
        warnings=warnings,
    )


# ------------------------------------------------------------------------ setup-wizard

DEFAULT_SETUP_COUNTRY = "United States"
DEFAULT_SETUP_CURRENCY = "USD"
DEFAULT_SETUP_LANGUAGE = "English"
DEFAULT_SETUP_TIMEZONE = "UTC"


@dataclass(frozen=True, slots=True, kw_only=True)
class SetupWizardReport:
    """The outcome of completing (or confirming already-complete) one site's
    Frappe setup wizard.

    Deliberately NOT :class:`BenchOpReport`: this op is not maintenance-mode
    gated, has no app, and its own success axis (``already_complete``) would sit
    as an unused placeholder on every other bench op's report.
    """

    project: str
    bench_path: str
    site: str
    already_complete: bool
    setup_complete: bool
    country: str
    currency: str
    language: str
    timezone: str
    ok: bool = True


def _read_setup_complete(container, bench_path: str, site: str) -> bool | None:
    """Read whether ``site``'s setup wizard is already complete.

    None on an unreadable probe (a failed exec, or output that doesn't parse as
    a JSON boolean or integer) - fail-honest, never guessed as either True or
    False. Reads the ``setup_complete`` System Settings Check field directly via
    ``frappe.db.get_single_value`` (present on every supported Frappe major),
    NOT ``frappe.is_setup_complete()`` which only exists on v15+; the value comes
    back as 1/0, not a JSON `true`/`false`, and ``bench execute`` prints the
    return only when truthy, so an incomplete site is an empty read.
    """
    kwargs = json.dumps({"doctype": "System Settings", "fieldname": "setup_complete"})
    cmd = (
        f"bench --site {shlex.quote(site)} execute frappe.db.get_single_value "
        f"--kwargs {shlex.quote(kwargs)}"
    )
    exit_code, text = exec_capture(container, cmd, workdir=bench_path)
    if exit_code != 0:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except (json.JSONDecodeError, TypeError):
        return None
    return bool(value) if isinstance(value, (bool, int)) else None


def complete_setup_wizard(
    project_name: str,
    *,
    site: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    country: str | None = None,
    currency: str | None = None,
    timezone: str | None = None,
    language: str | None = None,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[SetupWizardReport]:
    """Complete one site's Frappe setup wizard headlessly.

    A fresh site (from ``cwcli init`` or a bare ``bench new-site``) lands in the
    setup-wizard state: ``frappe.is_setup_complete()`` is False and the
    ``desktop:home_page`` default stays ``setup-wizard``, so a non-System-Manager
    desk renders with no navbar. The fix is the same code path a human finishing
    the wizard in a browser runs: Frappe's own whitelisted
    ``frappe.desk.page.setup_wizard.setup_wizard.setup_complete``, called here via
    ``bench execute ... --kwargs`` with the args dict the wizard UI would have
    submitted. It sets System Settings' country/currency/timezone, flips
    ``desktop:home_page`` to ``workspace``, and runs every installed app's
    ``setup_wizard_complete`` hook.

    Idempotent by ``setup_complete`` itself: it checks ``frappe.is_setup_complete``
    first and returns ``{"status": "ok"}`` without doing anything when the site is
    already set up (under an advisory lock, so a concurrent duplicate call is also
    a no-op). This wrapper reads that same flag before AND after so the report
    states plainly whether the call actually did anything.

    No user is created here - ``email``/``full_name``/``password`` are left out of
    the args dict, so ``setup_complete``'s own user-creation step is a no-op. The
    Administrator account and its password are already provisioned by
    ``cwcli init``'s ``bench new-site --admin-password``.
    """
    emit: OnEvent = on_event or _noop

    resolved = resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )
    if isinstance(resolved, Result):
        return resolved
    container, path, warnings = resolved

    target = _resolve_site(container, project_name, path, site, warnings)

    resolved_country = country.strip() if country and country.strip() else DEFAULT_SETUP_COUNTRY
    resolved_currency = (
        currency.strip() if currency and currency.strip() else DEFAULT_SETUP_CURRENCY
    )
    resolved_timezone = (
        timezone.strip() if timezone and timezone.strip() else DEFAULT_SETUP_TIMEZONE
    )
    resolved_language = (
        language.strip() if language and language.strip() else DEFAULT_SETUP_LANGUAGE
    )

    already_complete = bool(_read_setup_complete(container, path, target))

    args = {
        "country": resolved_country,
        "currency": resolved_currency,
        "timezone": resolved_timezone,
        "language": resolved_language,
    }
    command = (
        f"bench --site {shlex.quote(target)} execute "
        "frappe.desk.page.setup_wizard.setup_wizard.setup_complete "
        f"--kwargs {shlex.quote(json.dumps({'args': args}))}"
    )
    code = _run_step(container, command, path, action="setup-wizard", emit=emit)

    final_complete = _read_setup_complete(container, path, target)
    # Fail-honest: `ok` claims completion only when it was actually CONFIRMED
    # read back - an unreadable post-check (final_complete is None) is not
    # treated as success just because the RPC itself exited 0.
    ok = code == 0 and final_complete is True

    return Result(
        status=Status.OK if ok else Status.WARNING,
        data=SetupWizardReport(
            project=project_name,
            bench_path=path,
            site=target,
            already_complete=already_complete,
            setup_complete=bool(final_complete),
            country=resolved_country,
            currency=resolved_currency,
            language=resolved_language,
            timezone=resolved_timezone,
            ok=ok,
        ),
        warnings=warnings,
    )
