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
- **A stranded migrate lock is diagnosed, not swallowed.** Two migrations racing
  for one site's ``bench_migrate.lock`` is a reproducible, tolerated failure - they
  share a job queue. What is not tolerable is the aftermath: the loser's site then
  fails every LATER solo migrate the exact same opaque way, with nothing else
  running, until someone happens to know ``cwcli unlock`` exists. Verified against
  a real bench (see ``_migrate_lock_held``): the fix probes the SAME ``flock``
  kernel primitive frappe's own ``filelock()`` acquires, never the mere presence of
  a lock FILE, which a real bench proved is harmless on its own.

``set_maintenance`` lives here, and ``core.update`` imports it. It was private to
``core/update.py`` until this module became its second caller; it was PROMOTED
rather than copied, because two copies of a maintenance-mode lifecycle drift until
one of them stops disabling, and a site stuck in maintenance is a site that is down.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from . import resolvers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_stream

_MIGRATE_LOCK_CONFLICT_EXIT_CODE = 200

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


BenchOpEvent = BenchOpCommand | BenchOpOutput | BenchOpStepEnd
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


def _run_step(container, cmd: str, bench_path: str, *, action: str, emit: OnEvent) -> int:
    """Run one bench command, narrating it as events. Returns the honest exit code.

    Every chunk is emitted rather than written anywhere: the frontend picks its
    consumption mode. The command's OWN bytes matter here more than anywhere else
    on this surface - neither a migrate nor a test run is a supervised process, so
    neither writes to a log ``cwcli logs`` can serve afterwards, and the name of the
    patch that blew up (or the assertion that failed) exists ONLY in those bytes.
    """
    emit(BenchOpCommand(command=cmd))
    exit_code = 1
    for event in exec_stream(container, cmd, workdir=bench_path):
        if isinstance(event, ExecChunk):
            emit(BenchOpOutput(action=action, stream=event.stream, text=event.text))
        else:
            exit_code = event.exit_code
    emit(BenchOpStepEnd(action=action, ok=exit_code == 0))
    return exit_code


def _migrate_lock_held(container, bench_path: str, site: str) -> bool:
    """True when frappe's OWN migrate lock is genuinely held by a live process.

    ``bench migrate`` wraps its whole run in ``frappe.utils.synchronization.
    filelock("bench_migrate", timeout=1)``, an ``fcntl``-based advisory lock on
    ``{bench_path}/sites/{site}/locks/bench_migrate.lock``. Two migrations racing
    for one site is a reproducible, tolerated failure (they share a job queue); what
    is NOT tolerable is that the loser's site then reads as permanently broken - a
    stranded lock produces the exact same opaque failure on every later attempt,
    with nothing else running, until someone happens to know `cwcli unlock` exists.

    This probes with ``flock -n -E 200`` - the SAME kernel primitive frappe's own
    ``filelock()`` acquires - rather than checking whether the lock FILE exists.
    That distinction is load-bearing and verified against a real bench: an empty
    leftover ``bench_migrate.lock`` with no live holder is harmless (a plain
    ``bench migrate`` succeeds straight through it, and Python's ``filelock``
    package removes the file again on release), so gating on file presence would
    refuse perfectly runnable migrates. Exit 200 alone means the identical lock is
    held. Exit 0 means it is free. Any other exit is an unreadable lock state and
    raises ``CwcliError(PRECONDITION)`` rather than claiming a lock is held.
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
        site = resolvers.resolve_default_site(project_name, bench_path)
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

    A THIRD gate runs first, before maintenance mode is even touched: a site whose
    migrate lock is genuinely held (:func:`_migrate_lock_held`) is refused rather
    than run, naming ``cwcli unlock`` as the remedy. Two migrations racing for one
    site is a tolerated, reproducible failure; a stranded lock silently making
    every LATER solo attempt fail the same opaque way is not, and this is what
    turns that into a self-explaining refusal instead of a mystery.
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
        # Caught before maintenance mode is touched at all: a stranded lock reads
        # as a broken site precisely because the generic failure gives no next
        # step. Name the exact cause and the exact remedy instead.
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

    try:
        code = _run_step(
            container,
            f"bench --site {shlex.quote(target)} migrate",
            path,
            action="migrate",
            emit=emit,
        )
        results.append(BenchOpResult(action="migrate", ok=code == 0))
    finally:
        # Plain function, NOT a generator, precisely so this runs: an abandoned
        # generator's finally does not, and the cost here is a site left down.
        if set_maintenance(container, path, target, enable=False):
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
