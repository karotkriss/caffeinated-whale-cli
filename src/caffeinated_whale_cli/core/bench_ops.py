"""``core.bench_ops`` - standalone ``bench migrate`` and ``bench run-tests`` as UI-pure logic.

The two commands every Frappe proof runs, neither of which existed as a callable
operation before this module (openspec ``add-axi-bench-exec-verbs``). ``bench
migrate`` lived only inside :func:`core.update.update`'s pull-and-fan-out state
machine and inside ``restore_apply``'s post-restore step; ``bench run-tests``
existed nowhere at all.

Both functions own their resolution and container I/O and RETURN a typed report.
They print, prompt and exit nothing: a stopped container or an ambiguous
multi-bench project comes back as ``NEEDS_CHOICE``, hard failures raise
:class:`~.errors.CwcliError`.

Three things here are deliberate and load-bearing:

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

# ------------------------------------------------------------------------------ DTOs


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchOpResult:
    """One step's outcome. ``message`` carries a reason only when there IS one."""

    action: str  # "maintenance_on" | "migrate" | "maintenance_off" | "run-tests"
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

    ``site`` is the RESOLVED site, always populated - reading back what was acted
    on is the gap ``apps checkout`` left open and this deliberately does not repeat.
    """

    project: str
    bench_path: str
    site: str
    app: str | None = None  # run-tests only; a migrate is not app-scoped
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
    code = _run_step(
        container,
        f"bench --site {shlex.quote(target)} run-tests --app {shlex.quote(app)}",
        path,
        action="run-tests",
        emit=emit,
    )
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
