"""``core.apps`` - ``apps list`` / ``install`` / ``uninstall`` as UI-pure logic.

Batch 5 of the logic-core rework (openspec `migrate-apps-core`), finishing the
module batch 4 left half-migrated: ``apps update`` already returned a typed report
from :mod:`core.update` while its three siblings still hand-rolled
``results = [{"app", "site", "action", "ok"}]`` and printed it themselves. That
hand-rolled shape is what :class:`~.envelope.Result` was modeled on in the first
place (`core-logic-foundation/design.md:8`), so this retires the original now that
the typed form exists.

The three functions own their resolution, container I/O and fan-out, and RETURN a
typed report. They print, prompt and exit nothing: a stopped container or an
ambiguous multi-bench project comes back as ``NEEDS_CHOICE``, the destructive
uninstall gate comes back as ``NEEDS_CHOICE``/``confirm_uninstall``, hard failures
raise :class:`~.errors.CwcliError`.

Three things here are deliberate and load-bearing:

- **Plain functions with an optional ``on_event`` callback, NOT generators.**
  Same shape as :func:`core.update.update` (batch 4). Note the reason is only
  shape-consistency: ``update``'s generator hazard was that a ``try/finally``
  disabling maintenance mode does not run when an abandoned generator lands in a
  reference cycle. There is no such cleanup block here, so an abandoned generator
  would leak a socket, not corrupt state. Do not cite that hazard as if it applied.
- **The cache refresh is NOT here.** ``core/update.py`` reaches into the ``inspect``
  COMMAND only because its recache runs mid-fan-out and cannot be hoisted; these two
  recache as a post-mutation epilogue gated on a condition already in the returned
  report, so it hoists to the frontend for free and that reach stays the ONE place.
- **Resolution stops at container + bench.** No default-site, no site-name
  validation, no bench-dir probe: ``apps`` uses none of them today and adding them
  would ADD failures ``cwcli apps`` does not have (batch 3's Decision 8 trap).
  ``--site`` here is a FILTER, not a site to resolve.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..utils import bench_sites
from . import credbridge, resolvers, supervision
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_stream

# ------------------------------------------------------------------------------ DTOs


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsListing:
    """What ``apps list`` found: available apps, and (when asked) installed per site."""

    project: str
    bench_path: str
    available: list[str]
    # site -> its installed apps, or None when that site's read FAILED. The None is
    # load-bearing: "no apps" and "could not tell" are different facts, and only the
    # second one makes the command exit non-zero.
    installed: dict[str, list[str] | None] = field(default_factory=dict)
    ok: bool = True  # False iff any site's read failed


@dataclass(frozen=True, slots=True, kw_only=True)
class AppResult:
    """One (app, site) step's outcome.

    Deliberately the same four fields the hand-rolled dict already carried
    (`apps.py:342` pre-migration), so the human ``--json`` shape is preserved
    without a translation layer.
    """

    app: str
    site: str | None  # None for the bench-wide get-app step
    action: str  # "get-app" | "install-app" | "uninstall-app" | "restart-web"
    ok: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsReport:
    """The aggregate of a fan-out. ``ok`` is False iff ANY step failed."""

    project: str
    bench_path: str
    results: list[AppResult]
    ok: bool


# ---------------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsAnnounce:
    """A step the user should be told about is starting ("Fetching x...").

    Separate from :class:`AppsCommand` because the two interleave: the pre-migration
    code announces the fetch, THEN reads ``apps/`` (echoing that read under
    ``--verbose``), THEN echoes the get-app itself. Fusing them into one event would
    reorder ``--verbose`` stderr. Not every command is announced - install's
    internal ``apps/`` reads are echoed but never narrated.
    """

    phase: str  # "get-app" | "install-app" | "uninstall-app"
    app: str | None = None
    site: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsCommand:
    """A command is about to run: the ``--verbose`` echo, verbatim."""

    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsOutput:
    """A chunk of a bench command's output, tagged with the stream it came from."""

    phase: str
    app: str | None
    site: str | None
    stream: str  # "stdout" | "stderr", carried through from ExecChunk
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AppsStepEnd:
    """A step finished. ``ok`` is the honest per-step outcome."""

    phase: str
    app: str | None = None
    site: str | None = None
    ok: bool


AppsEvent = AppsAnnounce | AppsCommand | AppsOutput | AppsStepEnd

OnEvent = Callable[[AppsEvent], None]


def validate_checkout_app_name(app: str) -> None:
    if (
        not app
        or not app.strip()
        or app in {".", ".."}
        or PurePosixPath(app).name != app
        or "\0" in app
    ):
        raise CwcliError(
            ErrorKind.USAGE,
            "app.invalid_component",
            f"Invalid app name '{app}'. It must be one plain directory name.",
        )


def _noop(_event: AppsEvent) -> None:
    """The drain-and-discard consumption mode: what a non-narrating caller passes."""


# --------------------------------------------------------------------------- helpers


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


def _resolve(
    project_name: str,
    bench: str | None,
    bench_path: str | None,
    *,
    auto_start: bool = False,
) -> tuple[object, str, list[Message]] | Result:
    """The container + bench prologue all three verbs share.

    Promoted to :func:`core.resolvers.resolve_container_and_bench` when
    ``core.bench_ops`` became its second caller; this stays as the module's own
    spelling so every call site here is unchanged. Behaviour is identical.
    """
    return resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )


def _available_apps(frappe_container, bench_path: str) -> tuple[str, list[str]]:
    """Available apps = the directories under ``apps/`` (a live read).

    Returns ``(command_for_echo, apps)``. Run via ``workdir`` rather than
    interpolating ``bench_path`` into the command - that is a deliberate quoting
    guard against a hostile ``--path`` value, not an accident.

    A failed read is an empty list, NOT an error: `apps list` makes no claim it
    cannot back up, and `install`'s before/after diff simply falls through to
    deriving the name from the target. Pre-migration behaviour, preserved.
    """
    command = f"ls -1 apps (in {bench_path})"
    exit_code, output = frappe_container.exec_run("ls -1 apps", workdir=bench_path)
    if exit_code != 0:
        return command, []
    return command, [a for a in _decode(output).split("\n") if a.strip()]


def _installed_apps(frappe_container, bench_path: str, site: str) -> tuple[str, bool, list[str]]:
    """Apps installed on ``site``. Returns ``(command_for_echo, ok, apps)``.

    ``ok`` distinguishes "read failed" from "no apps" - the whole basis of the
    caller's exit code. Only the first token of each line is kept: a real bench
    prints ``<name> <version> <branch>``.
    """
    cmd = f"bench --site {shlex.quote(site)} list-apps"
    exit_code, text = _capture(frappe_container, cmd, bench_path)
    command = f"{cmd} -> exit {exit_code}"
    if exit_code != 0:
        return command, False, []
    return command, True, [line.split()[0] for line in text.split("\n") if line.strip()]


def _capture(frappe_container, cmd: str, workdir: str) -> tuple[int, str]:
    """Drain-and-join one exec: nothing is narrated, the output comes back whole."""
    text: list[str] = []
    exit_code = 1
    for event in exec_stream(frappe_container, cmd, workdir=workdir):
        if isinstance(event, ExecChunk):
            text.append(event.text)
        else:
            exit_code = event.exit_code
    return exit_code, "".join(text)


def _run_step(
    frappe_container,
    cmd: str,
    workdir: str,
    *,
    emit: OnEvent,
    phase: str,
    app: str | None = None,
    site: str | None = None,
) -> int:
    """Run one bench command, narrating it as events. Returns the exit code.

    Every chunk is emitted rather than written anywhere: the frontend picks its
    consumption mode (render to stdout in human mode, buffer in ``--json`` mode),
    which is the choice that belongs to a renderer and not to this module.
    """
    emit(AppsCommand(command=cmd))
    exit_code = 1
    for event in exec_stream(frappe_container, cmd, workdir=workdir):
        if isinstance(event, ExecChunk):
            emit(AppsOutput(phase=phase, app=app, site=site, stream=event.stream, text=event.text))
        else:
            exit_code = event.exit_code
    emit(AppsStepEnd(phase=phase, app=app, site=site, ok=exit_code == 0))
    return exit_code


def _target_sites(frappe_container, bench_path: str, sites: list[str] | None) -> list[str]:
    """The target site set: explicit ``--site`` values, else every site on the bench.

    Multi-site is the default. ``--site`` is a FILTER, de-duplicated with its order
    preserved; the fan-out set is sorted so the order is deterministic.
    """
    if sites:
        return list(dict.fromkeys(sites))
    found = bench_sites.list_sites(frappe_container, bench_path)
    return sorted(found) if found else []


def _wait_for_sites_after_restart(
    frappe_container,
    *,
    port: int,
    sites: list[str],
    timeout: float = 60.0,
    interval: float = 1.0,
) -> tuple[list[str], dict[str, str | None]]:
    """Poll until every mutated site answers Frappe's ping with HTTP 200.

    ``/api/method/ping`` is the target rather than ``/`` because its 200 is
    unambiguous: it is a whitelisted guest endpoint, so a healthy site cannot
    answer it with a redirect the way a website route can, and reaching it at all
    means Frappe booted that site's installed-app set. Bounded, because the
    mutation has already landed and a wait that never ends is worse than an
    honest "it did not come back".
    """
    pending = set(sites)
    codes: dict[str, str | None] = {site: None for site in sites}
    deadline = time.monotonic() + timeout
    while pending:
        for site in list(pending):
            codes[site] = supervision.web_http_code(
                frappe_container, port=port, site=site, path="/api/method/ping"
            )
            if codes[site] == "200":
                pending.remove(site)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    return sorted(pending), codes


def _restart_web_after_mutation(
    frappe_container,
    bench_path: str,
    sites: list[str],
    results: list[AppResult],
    warnings: list[Message],
    *,
    emit: OnEvent,
) -> None:
    """Restart a running bench's web program and prove every mutated site serves.

    The defect this closes: ``bench install-app`` changes what is on disk, but the
    web process was started BEFORE it and keeps serving the interpreter it booted
    with, so every request to the site 500s on a module the process cannot import
    while the command reports plain success. Uninstall is the same fault mirrored -
    the old process keeps serving an app whose files and tables are gone. So the
    restart is ONE shared post-mutation step for both verbs, not two.

    It is not enough to restart and say so: this whole defect is a command
    vouching for a state it never checked, and "supervisord reports the program
    RUNNING" is another bound-port-shaped claim. Every site the fan-out actually
    changed must answer a site-routed request before ``ok`` may stay true.

    Three properties, each guarding something specific:

    - **A stopped bench stays stopped.** The bench is only restarted when a
      ``required`` process read POSITIVELY establishes a live supervisord; an
      unreadable read is a reported failure, never "assume nothing is running"
      (``core.where``'s fail-honest rule). Starting a bench the user deliberately
      stopped is its own defect, and an install must not smuggle one in.
    - **Nothing here raises.** A mutation has already landed by this point, so
      failures append to the typed report instead. A raise would erase the record
      of a change that genuinely happened, and an automation caller that retried
      the install or uninstall would then run it a second time.
    - **The restart is DISCLOSED**, as its own announced step and its own result
      row, on the human, ``--json`` and ``axi`` surfaces alike. Replacing a silent
      breakage with a silent restart would fix the outcome and keep the habit.
    """

    def failed(text: str) -> None:
        results.append(AppResult(app="web", site=None, action="restart-web", ok=False))
        warnings.append(Message("app.web_restart_failed", text))

    unique_sites = list(dict.fromkeys(sites))
    if not unique_sites:
        return

    try:
        snapshot = supervision.discover_stack(frappe_container, bench_path, required=True)
        supervised = snapshot.supervisor_up
        if supervised:
            states = supervision.supervisorctl_states(frappe_container, bench_path)
            program = supervision.program_for_label(list(states), "web")
            if program is None:
                failed(
                    "App mutation completed, but the running bench has no supervised web "
                    "process that cwcli can restart."
                )
                return
            if states[program][0] not in {"RUNNING", "STARTING"}:
                return

            emit(AppsAnnounce(phase="restart-web"))
            restart_code, restart_output = supervision.restart_program(
                frappe_container, bench_path, program
            )
            if restart_code not in (0, None):
                detail = restart_output.strip()
                suffix = f" ({detail})" if detail else ""
                failed(
                    "App mutation completed, but restarting the bench web process "
                    f"failed{suffix}."
                )
                return
        else:
            fallback = supervision.discover_unsupervised_stack(
                frappe_container, bench_path, required=True
            )
            if not fallback.manager_up:
                return

        ports = resolvers.resolve_assigned_ports(
            frappe_container, [bench_path], fill_defaults=False
        ).get(bench_path)
        if ports is None:
            failed(
                "App mutation completed, but the bench's assigned port could not be read, "
                "so cwcli could not confirm the mutated site serves."
            )
            return

        pending, codes = _wait_for_sites_after_restart(
            frappe_container, port=ports[0], sites=unique_sites
        )
        if pending:
            observed = ", ".join(f"{site}={codes[site] or 'unreachable'}" for site in pending)
            remedy = (
                " Restart the bench manually, then verify the sites again."
                if not supervised
                else ""
            )
            failed(
                "These mutated sites did not answer Frappe's ping with HTTP 200: "
                f"{observed}.{remedy}"
            )
            return

        if supervised:
            results.append(AppResult(app="web", site=None, action="restart-web", ok=True))
    except Exception as error:  # noqa: BLE001
        detail = error.message if isinstance(error, CwcliError) else str(error)
        failed(
            "App mutation completed, but cwcli could not complete the web restart and "
            f"verification: {detail or type(error).__name__}"
        )


def derive_app_name(target: str) -> str:
    """Fallback app name when the ``apps/`` before/after diff cannot tell us.

    Used only when ``bench get-app`` added zero or more than one new ``apps/`` entry
    (an already-present app, or an ambiguous multi-dir fetch). A plain name is
    itself; a git URL clones into ``apps/<repo-basename>`` (minus a trailing
    ``.git``) by convention, which is the name ``install-app`` expects.
    """
    if "://" in target or target.endswith(".git") or "@" in target or "/" in target:
        base = target.rstrip("/").split("/")[-1]
        return base[:-4] if base.endswith(".git") else base
    return target


# ------------------------------------------------------------------------------ list


def list_apps(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    sites: list[str] | None = None,
    installed: bool = False,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[AppsListing]:
    """List apps available in a bench, and (with ``sites``/``installed``) per site.

    A pure read: it emits no progress and no output, only the :class:`AppsCommand`
    trace of the reads it performs, so a ``--verbose`` frontend can echo them. The
    proposal specified NO callback here ("nothing to report progress about"), which
    was right about progress and wrong about the trace: routing it through
    ``warnings`` instead put a debug echo in the agent surface's structured
    document, where it is noise. Traces ride the event channel across this whole
    module (as they do in ``core.update``); ``warnings`` stays for genuine notes an
    agent should act on, and a caller that wants neither passes nothing.
    """
    emit: OnEvent = on_event or _noop

    resolved = _resolve(project_name, bench, bench_path, auto_start=auto_start)
    if isinstance(resolved, Result):
        return resolved
    frappe_container, path, warnings = resolved

    command, available = _available_apps(frappe_container, path)
    emit(AppsCommand(command=command))

    installed_by_site: dict[str, list[str] | None] = {}
    if installed or sites:
        for site in _target_sites(frappe_container, path, sites):
            command, ok, site_apps = _installed_apps(frappe_container, path, site)
            emit(AppsCommand(command=command))
            installed_by_site[site] = site_apps if ok else None

    any_fail = any(v is None for v in installed_by_site.values())

    return Result(
        status=Status.WARNING if any_fail else Status.OK,
        data=AppsListing(
            project=project_name,
            bench_path=path,
            available=available,
            installed=installed_by_site,
            ok=not any_fail,
        ),
        warnings=warnings,
    )


# --------------------------------------------------------------------------- install


def _refuse_if_installed(
    frappe_container,
    path: str,
    apps: list[str],
    sites: list[str] | None,
    *,
    emit: OnEvent,
) -> None:
    """Raise CONFLICT if any of ``apps`` is already installed on any target site.

    Runs BEFORE ``bench get-app``, so a refused install fetches nothing and leaves
    the bench exactly as it found it.

    The name compared is :func:`derive_app_name` of the target, which is what
    ``install-app`` would be handed when the ``apps/`` before/after diff is
    inconclusive. When that derivation is wrong for an exotic URL the check simply
    does not match and the install proceeds to bench's own behaviour - it can miss a
    match, it cannot invent one, so it never refuses an install that was safe.
    """
    for site in _target_sites(frappe_container, path, sites):
        command, ok, site_apps = _installed_apps(frappe_container, path, site)
        emit(AppsCommand(command=command))
        if not ok:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "app.install_state_unknown",
                f"Could not read the installed apps on site '{site}' (it may not exist "
                "on this bench), so it cannot be confirmed that this install would not "
                "touch existing app data.",
                hint=(
                    f"Check the site exists and is readable: 'cwcli axi apps list "
                    f"<project> --site {site}'."
                ),
            )
        for target in apps:
            name = derive_app_name(target)
            if name in site_apps:
                raise CwcliError(
                    ErrorKind.CONFLICT,
                    "app.already_installed",
                    f"App '{name}' is already installed on site '{site}'. Installing it "
                    "again would re-run its install hooks against that site's existing "
                    "data.",
                    hint=(
                        f"To move it to another ref: 'cwcli axi apps checkout <project> "
                        f"{name} <ref>'. To pull and migrate it: 'cwcli axi apps update "
                        f"<project> {name} --site {site}'. A genuine reinstall is the "
                        f"human 'cwcli apps install', which confirms first."
                    ),
                )


def install_apps(
    project_name: str,
    apps: list[str],
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    sites: list[str] | None = None,
    branch: str | None = None,
    fetch_only: bool = False,
    auto_start: bool = False,
    require_absent: bool = False,
    on_event: OnEvent | None = None,
) -> Result[AppsReport]:
    """Fetch (``bench get-app``) and install app(s) on the target site(s).

    ``require_absent`` refuses, BEFORE fetching anything, if an app is already
    installed on a target site. It defaults off so the human verb is unchanged, and
    the rule lives here rather than in a frontend so ``axi`` and any future GUI share
    one implementation of it (the ``apps uninstall`` consent lesson: a safety rule a
    frontend owns alone is a rule the next frontend forgets). ``cwcli axi apps
    install`` is its only caller today - see that verb for why it is applied there.

    The check FAILS CLOSED: a site whose ``list-apps`` read fails cannot be confirmed
    clean, so it refuses rather than proceeding on an unknown (``core.where``'s
    fail-honest rule - an unreadable state must never degrade to "nothing is there").
    """
    emit: OnEvent = on_event or _noop

    resolved = _resolve(project_name, bench, bench_path, auto_start=auto_start)
    if isinstance(resolved, Result):
        return resolved
    frappe_container, path, warnings = resolved

    if require_absent and not fetch_only:
        _refuse_if_installed(frappe_container, path, apps, sites, emit=emit)

    results: list[AppResult] = []
    fetched: list[tuple[str, str]] = []
    branch_arg = f"--branch {shlex.quote(branch)} " if branch else ""

    # Bridge the container's git back to the host's gh/glab for private-repo
    # fetches. Inert for public repos (git only calls a credential helper on 401),
    # so every git-URL fetch is wrapped and torn down; see core.credbridge.
    with credbridge.credential_bridge(frappe_container, path):
        for target in apps:
            get_cmd = f"bench get-app {branch_arg}{shlex.quote(target)}"

            # Announce BEFORE the apps/ read, so --verbose stderr keeps its historical
            # order: "Fetching x..." then the read's echo then get-app's own echo.
            emit(AppsAnnounce(phase="get-app", app=target))
            command, before_apps = _available_apps(frappe_container, path)
            emit(AppsCommand(command=command))
            before = set(before_apps)

            code = _run_step(
                frappe_container, get_cmd, path, emit=emit, phase="get-app", app=target
            )
            if code != 0:
                results.append(AppResult(app=target, site=None, action="get-app", ok=False))
                continue
            results.append(AppResult(app=target, site=None, action="get-app", ok=True))

            command, after_apps = _available_apps(frappe_container, path)
            emit(AppsCommand(command=command))

            new_dirs = set(after_apps) - before
            app_name = new_dirs.pop() if len(new_dirs) == 1 else derive_app_name(target)
            fetched.append((target, app_name))

    if not fetch_only:
        target_sites = _target_sites(frappe_container, path, sites)
        if not target_sites:
            warnings.append(
                Message(
                    "sites.none",
                    "no sites on the bench to install on; app(s) fetched only.",
                )
            )
        for _target, app_name in fetched:
            for site in target_sites:
                install_cmd = (
                    f"bench --site {shlex.quote(site)} install-app {shlex.quote(app_name)}"
                )
                emit(AppsAnnounce(phase="install-app", app=app_name, site=site))
                code = _run_step(
                    frappe_container,
                    install_cmd,
                    path,
                    emit=emit,
                    phase="install-app",
                    app=app_name,
                    site=site,
                )
                results.append(
                    AppResult(app=app_name, site=site, action="install-app", ok=code == 0)
                )

    # Only the sites an install actually landed on: a site whose install-app failed
    # was not changed, so it has nothing to re-verify and must not fail the restart.
    changed_sites = [
        r.site for r in results if r.action == "install-app" and r.ok and r.site is not None
    ]
    _restart_web_after_mutation(frappe_container, path, changed_sites, results, warnings, emit=emit)

    any_fail = any(not r.ok for r in results)
    return Result(
        status=Status.WARNING if any_fail else Status.OK,
        data=AppsReport(project=project_name, bench_path=path, results=results, ok=not any_fail),
        warnings=warnings,
    )


# -------------------------------------------------------------------------- checkout


def _resolve_remote(frappe_container, app_dir: str, app: str) -> str:
    """The git remote to fetch the ref from - auto-detected, no hardcoded name.

    bench's own ``get-app`` clones with ``--origin upstream``, so a
    bench-installed app's remote is ``upstream``, NOT ``origin`` (verified on a
    real bench); a hand-cloned checkout usually has ``origin``. Detecting it lets
    the verb work on both with no flag. A dir that is not a git checkout (``git
    remote`` fails) is a clear precondition error, not a cryptic later git failure.
    """
    exit_code, output = frappe_container.exec_run("git remote", workdir=app_dir)
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "app.no_checkout",
            f"No git checkout found for app '{app}' at {app_dir}.",
            hint="Target an app already installed in the bench (see 'cwcli apps list').",
        )
    remotes = _decode(output).split()
    for preferred in ("upstream", "origin"):
        if preferred in remotes:
            return preferred
    if remotes:
        return remotes[0]
    raise CwcliError(
        ErrorKind.PRECONDITION,
        "app.no_remote",
        f"The checkout for app '{app}' has no git remote to fetch from.",
    )


def _refuse_dirty_tree(frappe_container, app_dir: str, app: str) -> None:
    """Refuse the checkout when the app's working tree has uncommitted work.

    DIRTY means, precisely: ANY change ``git status --porcelain`` reports - staged
    changes, unstaged modifications to tracked files, AND untracked files. Files
    matched by ``.gitignore`` are not reported by git and so never count, which is
    what keeps a real bench app dir usable: its ``__pycache__``/``node_modules``/
    ``*.egg-info`` build residue is ignored, so only genuinely unaccounted-for
    files trip this.

    Untracked files count DELIBERATELY (captain's ruling, widening the first cut of
    this guard). A brand-new module a developer has written but not yet added is
    uncommitted work in the plainest sense, and it is precisely the case where the
    tool must not decide on the user's behalf that the file is worthless. Note the
    honest consequence: ``git reset --hard`` does NOT delete untracked files (that
    is ``git clean``), so ``--reset`` lets the checkout proceed and leaves them in
    place rather than destroying them.

    This is cwcli's OWN guard and it is deliberately STRONGER than git's. ``git
    checkout -B`` refuses only a checkout that would OVERWRITE a modified file, so
    a dirty file the target ref does not touch used to ride silently across a
    branch switch at exit 0 while the docs promised a dirty tree would fail. These
    checkouts live in a SHARED dev instance, so the uncommitted work carried across
    may not even belong to whoever ran the command.

    FAILS CLOSED: an unreadable ``git status`` refuses rather than proceeding on an
    unknown (``core.where``'s fail-honest rule - an unreadable state must never
    degrade to "nothing is there").
    """
    exit_code, output = frappe_container.exec_run("git status --porcelain", workdir=app_dir)
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "app.dirty_state_unknown",
            f"Could not read the git working-tree state for app '{app}' at {app_dir}, "
            "so it cannot be confirmed that this checkout would not discard "
            "uncommitted work.",
            hint="Check the checkout is readable, or pass --reset to discard local changes.",
        )
    dirty = [line for line in _decode(output).splitlines() if line.strip()]
    if not dirty:
        return
    # ONE line: `axi` renders this through `toon.kv`, where an embedded newline
    # would split the document into unparseable lines.
    shown = ", ".join(line.strip() for line in dirty[:10])
    more = f" (and {len(dirty) - 10} more)" if len(dirty) > 10 else ""
    raise CwcliError(
        ErrorKind.CONFLICT,
        "app.dirty_tree",
        f"App '{app}' has uncommitted changes, so checking out another ref would "
        f"carry them across: {shown}{more}.",
        hint=(
            "Commit or stash them in the instance, or pass --reset to hard-reset to "
            "the fetched ref, DISCARDING tracked changes (untracked files are left "
            "in place, not deleted). '??' marks an untracked file."
        ),
        detail={"dirty": dirty},
    )


def checkout_app(
    project_name: str,
    app: str,
    ref: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    reset: bool = False,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[AppsReport]:
    """Fetch and check out an arbitrary ``ref`` into an app that ALREADY EXISTS.

    The gap ``install``/``update`` leave: ``install`` is ``bench get-app`` (a FRESH
    clone of a new app) and ``update`` is ``bench update --pull`` (the TRACKED
    upstream on every app). Neither fetches one named branch/tag/commit into an
    existing ``apps/<app>`` checkout, which is exactly what putting a feature branch
    under test in the instance the app lives in needs.

    The private-repo fetch rides the SAME credential bridge as ``install``/
    ``update`` (host ``gh``/``glab`` -> in-container git over a unix socket; the raw
    token never enters the container, and the bridge is inert for public repos).
    A DIRTY working tree is REFUSED before anything is fetched (``CONFLICT``/
    ``app.dirty_tree``); ``reset=True`` is the explicit opt-in that instead discards
    tracked changes and hard-resets to the fetched tip, the clean-tree guarantee the
    delivery workflow's build/migrate steps rely on. Dirty means anything ``git
    status --porcelain`` reports, untracked files included - see
    :func:`_refuse_dirty_tree`.

    Returns the same :class:`AppsReport` as ``install``/``uninstall`` - one
    :class:`AppResult` per git step - so the CLI renderer and exit-code logic are
    shared. It stops at the first failed step (a failed fetch makes the checkout
    meaningless).
    """
    emit: OnEvent = on_event or _noop

    validate_checkout_app_name(app)
    resolved = _resolve(project_name, bench, bench_path, auto_start=auto_start)
    if isinstance(resolved, Result):
        return resolved
    frappe_container, path, warnings = resolved

    app_dir = f"{path}/apps/{app}"
    remote = _resolve_remote(frappe_container, app_dir, app)
    if not reset:
        # BEFORE any fetch: refuse a dirty tree outright. See _refuse_dirty_tree for
        # what counts as dirty and why this is stronger than git's own refusal.
        _refuse_dirty_tree(frappe_container, app_dir, app)
    q_ref = shlex.quote(ref)
    q_remote = shlex.quote(remote)
    # `-B <ref> FETCH_HEAD`: move the local branch <ref> to EXACTLY what we just
    # fetched. FETCH_HEAD (not <remote>/<ref>) is what `git fetch <remote> <ref>`
    # guarantees regardless of the app's fetch refspec, and `-B` makes a re-run
    # idempotent by re-pointing an existing local branch at the new remote tip.
    # ponytail: a tag/sha names its local branch after itself (odd, harmless) -
    # the workflow's target is a feature branch, where the name is exactly right.
    steps = [
        ("fetch", f"git fetch {q_remote} -- {q_ref}"),
        ("checkout", f"git checkout -B {q_ref} FETCH_HEAD"),
    ]
    if reset:
        # Discard any local edits so build/migrate run against exactly the fetched
        # tree (bench update's dirty-tree guard; the in-instance copy holds no work).
        steps.append(("reset", "git reset --hard FETCH_HEAD"))

    results: list[AppResult] = []
    with credbridge.credential_bridge(frappe_container, path):
        for action, cmd in steps:
            code = _run_step(frappe_container, cmd, app_dir, emit=emit, phase=action, app=app)
            results.append(AppResult(app=app, site=None, action=action, ok=code == 0))
            if code != 0:
                break

    any_fail = any(not r.ok for r in results)
    return Result(
        status=Status.WARNING if any_fail else Status.OK,
        data=AppsReport(project=project_name, bench_path=path, results=results, ok=not any_fail),
        warnings=warnings,
    )


# ------------------------------------------------------------------------- uninstall


def uninstall_apps(
    project_name: str,
    apps: list[str],
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    sites: list[str] | None = None,
    auto_start: bool = False,
    consent: bool = False,
    on_event: OnEvent | None = None,
) -> Result[AppsReport]:
    """Uninstall app(s) from the target site(s). Destructive: this deletes site data.

    ``auto_start`` and ``consent`` are SEPARATE parameters on purpose. The human
    ``cwcli apps uninstall --yes`` fuses them ("skip the destructive confirmation
    and auto-start containers") and keeps that exact meaning - a migration does not
    change the CLI contract - but the fusion is a UX question, and a core that must
    serve ``axi`` and a future GUI models the two consents as what they are. Without
    ``consent`` this returns ``NEEDS_CHOICE``/``confirm_uninstall``; it never
    prompts, and it never performs a start of its own.
    """
    emit: OnEvent = on_event or _noop

    resolved = _resolve(project_name, bench, bench_path, auto_start=auto_start)
    if isinstance(resolved, Result):
        return resolved
    frappe_container, path, warnings = resolved

    target_sites = _target_sites(frappe_container, path, sites)
    if not target_sites:
        # Nothing to uninstall from is a clean no-op, not a failure - and the
        # destructive gate is never reached, so this must be decided BEFORE it.
        warnings.append(Message("sites.none", "no sites on the bench to uninstall from."))
        return Result(
            status=Status.OK,
            data=AppsReport(project=project_name, bench_path=path, results=[], ok=True),
            warnings=warnings,
        )

    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_uninstall",
                param="consent",
                prompt=(
                    f"Uninstall {', '.join(apps)} from {len(target_sites)} site(s) "
                    f"({', '.join(target_sites)})? This deletes their data."
                ),
            ),
        )

    results: list[AppResult] = []
    for app_name in apps:
        for site in target_sites:
            cmd = f"bench --site {shlex.quote(site)} uninstall-app {shlex.quote(app_name)} --yes"
            emit(AppsAnnounce(phase="uninstall-app", app=app_name, site=site))
            code = _run_step(
                frappe_container,
                cmd,
                path,
                emit=emit,
                phase="uninstall-app",
                app=app_name,
                site=site,
            )
            results.append(AppResult(app=app_name, site=site, action="uninstall-app", ok=code == 0))

    changed_sites = [
        r.site for r in results if r.action == "uninstall-app" and r.ok and r.site is not None
    ]
    _restart_web_after_mutation(frappe_container, path, changed_sites, results, warnings, emit=emit)

    any_fail = any(not r.ok for r in results)
    return Result(
        status=Status.WARNING if any_fail else Status.OK,
        data=AppsReport(project=project_name, bench_path=path, results=results, ok=not any_fail),
        warnings=warnings,
    )
