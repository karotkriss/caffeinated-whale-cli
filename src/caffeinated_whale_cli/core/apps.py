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
from collections.abc import Callable
from dataclasses import dataclass, field

from ..utils import bench_sites
from . import credbridge, resolvers
from . import docker as core_docker
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
    action: str  # "get-app" | "install-app" | "uninstall-app"
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

    Returns ``(container, bench_path, warnings)``, or a ``NEEDS_CHOICE`` ``Result``
    the caller must return as-is.
    """
    warnings: list[Message] = []

    frappe_container = core_docker.get_frappe_container(project_name)

    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        # No cache to resolve against: fall back to the historical default, matching
        # `run` and the pre-migration `_resolve_bench`'s `or _DEFAULT_BENCH`.
        resolved = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        assert bench_result.data is not None  # OK always carries the path
        resolved = bench_result.data
        warnings.extend(bench_result.warnings)

    return frappe_container, resolved, warnings


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
    on_event: OnEvent | None = None,
) -> Result[AppsReport]:
    """Fetch (``bench get-app``) and install app(s) on the target site(s)."""
    emit: OnEvent = on_event or _noop

    resolved = _resolve(project_name, bench, bench_path, auto_start=auto_start)
    if isinstance(resolved, Result):
        return resolved
    frappe_container, path, warnings = resolved

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
    ``reset=True`` additionally hard-resets the working tree to the fetched tip, the
    clean-tree guarantee the delivery workflow's build/migrate steps rely on.

    Returns the same :class:`AppsReport` as ``install``/``uninstall`` - one
    :class:`AppResult` per git step - so the CLI renderer and exit-code logic are
    shared. It stops at the first failed step (a failed fetch makes the checkout
    meaningless).
    """
    emit: OnEvent = on_event or _noop

    resolved = _resolve(project_name, bench, bench_path, auto_start=auto_start)
    if isinstance(resolved, Result):
        return resolved
    frappe_container, path, warnings = resolved

    app_dir = f"{path}/apps/{app}"
    remote = _resolve_remote(frappe_container, app_dir, app)
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

    any_fail = any(not r.ok for r in results)
    return Result(
        status=Status.WARNING if any_fail else Status.OK,
        data=AppsReport(project=project_name, bench_path=path, results=results, ok=not any_fail),
        warnings=warnings,
    )
