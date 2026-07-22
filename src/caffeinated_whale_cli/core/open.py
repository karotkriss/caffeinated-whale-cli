"""``core.open`` - resolve what ``cwcli open`` will launch, without launching it.

The settled shape from the ``cwcli-open-handover-design-o9`` recon: ``open`` was
never a handover command - only one of its four editor branches hands over
(``--docker`` -> ``exec_into_container`` -> ``os.execvp``, the codebase's one
``execvp``), the other three return normally. So this is :func:`~.run.run_plan`
minus ``run_stream``: :func:`open_plan` resolves everything (container,
run-state, bench, the no-cache fallback populate, ``--app``, editor) and returns
a ``Result[LaunchTarget]``; the FRONTEND performs the handover. A plain function,
not a generator - nothing streams, and ``NEEDS_CHOICE`` must be returnable at
call time for all three forks (``confirm_start``, ``select_bench``,
``select_editor``).

:class:`LaunchTarget` is declarative - four strings, never an argv - so a GUI
can perform its own handover (a GUI must spawn detached, never ``execvp`` itself
away). It carries a container NAME, not an ID: both handover mechanisms consume
the name (the vscode-remote URI hex-encodes it), and unlike ``run`` there is no
phase-2 core call needing to bridge an ID back into a handle.

The core never prints, prompts, exits, or execs; the one ``execvp`` in the
codebase stays in ``utils/docker_utils.py``, called only by the frontend.

**There is deliberately NO ``axi open`` verb** (asserted by a test): not because
the plan will not serialize - it is four strings and would - but because
``execvp`` destroys the process that owes ``axi`` its one-TOON-document
contract, and the editor branches are meaningless to an agent with no desktop.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass

from . import docker as core_docker
from . import inspect as core_inspect
from . import resolvers
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchTarget:
    """What the frontend should launch, and where. Serializable: plain strings."""

    project: str
    container_name: str  # a NAME string; both handover mechanisms consume the name
    working_dir: str  # the bench path, or {bench}/apps/{app} under --app
    editor: str  # "docker" | "code" | "code-insiders" | "cursor"
    # The host URL this bench actually serves on (``resolvers.resolve_host_web_url``),
    # or None when it cannot be read. ``open`` hands over to an editor or a shell and
    # never opens a browser, but it is the command every "open it at ..." hint points
    # at, so it is the one place that owes the reader the real address: bench 1 of a
    # ``--port 21000`` instance is http://<site>:21001, not the :8000 that used to be
    # printed. None is rendered as nothing at all - never a guessed port.
    web_url: str | None = None


# ------------------------------------------------------------------------ typed events


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenNotice:
    """A note the frontend should surface UNCONDITIONALLY (not ``-v``-gated),
    e.g. the fallback-populate announcement."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenTrace:
    """A prose diagnostic (the ``-v`` ``VERBOSE:`` lines)."""

    text: str


OpenEvent = OpenNotice | OpenTrace
OnEvent = Callable[[OpenEvent], None]


def _noop(_event: OpenEvent) -> None:
    """Default event sink; keeps every emit unconditional."""


# --------------------------------------------------------------------------- editors

_EDITOR_LABELS = {
    "code": "VS Code - Open in development container",
    "code-insiders": "VS Code Insiders - Open in development container",
    "cursor": "Cursor - Open in development container",
    "docker": "Docker - Execute interactive shell in container",
}

_EDITOR_INSTALL = {
    "code": ("VS Code", "https://code.visualstudio.com/"),
    "code-insiders": ("VS Code Insiders", "https://code.visualstudio.com/insiders/"),
    "cursor": ("Cursor", "https://cursor.sh/"),
}


def _resolve_editor(editor: str | None) -> Result[str]:
    """The editor decision, detection included (stdlib ``shutil.which``, the
    ``core/version.py`` host-side-detection precedent), run ONCE - this retires
    the old frontend's pointless double detection.

    - requested but not installed -> ``NOT_FOUND`` with the install-URL hint
      (``docker`` is always valid)
    - unrecognized value -> ``USAGE``
    - ``None`` + nothing installed -> ``"docker"``, silently, as today
    - ``None`` + at least one editor installed -> ``NEEDS_CHOICE``
      ``select_editor`` listing the installed editors plus Docker
    """
    if editor == "docker":
        return Result(status=Status.OK, data="docker")

    if editor is not None:
        if editor not in _EDITOR_INSTALL:
            raise CwcliError(
                ErrorKind.USAGE,
                "editor.unknown",
                f"Unknown editor '{editor}'. Use one of: code, code-insiders, cursor, docker.",
            )
        name, url = _EDITOR_INSTALL[editor]
        if shutil.which(editor) is None:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "editor.not_installed",
                f"{name} is not installed.",
                hint=f"Install it from {url}",
            )
        return Result(status=Status.OK, data=editor)

    installed = [e for e in ("code", "code-insiders", "cursor") if shutil.which(e)]
    if not installed:
        return Result(status=Status.OK, data="docker")

    options = [{"value": e, "label": _EDITOR_LABELS[e]} for e in [*installed, "docker"]]
    return Result(
        status=Status.NEEDS_CHOICE,
        choice=Choice(
            kind="select_editor",
            param="editor",
            prompt="How would you like to open this instance?",
            options=options,
        ),
    )


# --------------------------------------------------------------------------- the plan


def open_plan(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    app: str | None = None,
    editor: str | None = None,
    auto_start: bool = False,
    on_event: OnEvent | None = None,
) -> Result[LaunchTarget]:
    """Resolve the container, bench, ``--app`` dir, and editor for ``cwcli open``.

    Resolution order is today's: container -> run-state -> bench (with the
    no-cache fallback populate) -> ``--app`` -> editor, so the editor choice
    arrives only AFTER bench/app resolution succeeded, preserving the error
    ordering. See the module docstring for the boundary.
    """
    emit = on_event or _noop
    warnings: list[Message] = []

    # 1. Resolve the frappe container (raises NOT_FOUND / DOCKER).
    frappe_container = core_docker.get_frappe_container(project_name)
    emit(OpenTrace(text=f"Found frappe container: {frappe_container.name}"))

    # 2. Container must be running; a stopped container is a confirm_start fork
    # (the race backstop behind the frontend's interactive prologue).
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # 3. Resolve which bench to open (--bench/--path, else single, else the
    # fallback populate below when nothing is cached).
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        bench_result = _fallback_populate(
            project_name, bench, auto_start=auto_start, warnings=warnings, emit=emit
        )
    if bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    resolved_path = bench_result.data
    warnings.extend(bench_result.warnings)
    assert resolved_path is not None  # OK always carries the resolved path

    # 4. --app: validate against the bench being opened and descend into it.
    working_dir = resolved_path
    if app:
        working_dir = _resolve_app_dir(project_name, frappe_container, resolved_path, app, emit)
        emit(OpenTrace(text=f"Opening app path: {working_dir}"))

    # 5. The editor decision (detection included), after bench/app resolution.
    editor_result = _resolve_editor(editor)
    if editor_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=editor_result.choice, warnings=warnings)
    assert editor_result.data is not None

    return Result(
        status=Status.OK,
        data=LaunchTarget(
            project=project_name,
            container_name=frappe_container.name,
            working_dir=working_dir,
            editor=editor_result.data,
            web_url=_web_url(project_name, frappe_container, resolved_path),
        ),
        warnings=warnings,
    )


def _web_url(project_name: str, frappe_container, bench_path: str) -> str | None:
    """This bench's real host URL, or None.

    Fails SOFT by design: the URL is a convenience the plan carries, never the thing
    ``open`` was asked to do, so an unreadable config or port table costs the caller
    one hint line - it must not turn a working ``cwcli open`` into an error.
    """
    try:
        site = resolvers.resolve_representative_site(project_name, bench_path)
        return resolvers.resolve_host_web_url(frappe_container, bench_path, site=site)
    except Exception:  # noqa: BLE001 - a hint must never fail the launch
        return None


def _fallback_populate(
    project_name: str,
    bench: str | None,
    *,
    auto_start: bool,
    warnings: list[Message],
    emit: OnEvent,
) -> Result[str]:
    """The no-cache fallback: populate via ``core.inspect``, then re-resolve.

    The abort/degrade contract (the merged PR #93 fallback-abort decision,
    pinned by ``tests/test_open_inspect_fallback.py``):

    - a hard ``CwcliError`` from the fallback inspect PROPAGATES - the guessed
      default path is never opened;
    - a non-``CwcliError`` exception degrades to ``DEFAULT_BENCH_PATH`` with a
      warning (the belt-and-braces residue: post batch 7 ``core.inspect`` wraps
      raw docker escapes into ``CwcliError(DOCKER)``, so this branch is nearly
      dead and survives strictly as residue);
    - a populate that succeeds but still resolves nothing degrades likewise.

    Disclosed hardening: the inspect runs ``offer_choice=False`` where the old
    frontend left the default ``True`` and then DISCARDED a returned
    ``confirm_start`` - a container stopped in the race window now aborts with a
    typed ``NOT_RUNNING`` instead of silently opening the guessed default path
    against a stopped container.
    """
    emit(OpenNotice(text="No cached bench path found. Running inspect..."))
    try:
        # Populate the cache via the core inspect slice (it owns the cache
        # write); the returned report is deliberately discarded - open only
        # needs the side effect.
        core_inspect.inspect(
            project_name, refresh="auto", auto_start=auto_start, offer_choice=False
        )
        # Re-resolve with the same --bench rules, so a freshly-inspected
        # multi-bench project still surfaces select_bench rather than silently
        # picking a bench.
        re_resolved = resolvers.resolve_bench(project_name, bench, None)
    except CwcliError:
        raise
    except Exception as e:  # noqa: BLE001 - the disclosed degrade residue
        warnings.append(
            Message(
                "bench.default_used",
                f"Inspect failed. Using default bench path: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
        emit(OpenTrace(text=f"Inspect error: {e}"))
        return Result(status=Status.OK, data=resolvers.DEFAULT_BENCH_PATH)

    if re_resolved is None:
        warnings.append(
            Message(
                "bench.default_used",
                f"Could not detect bench path. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
        return Result(status=Status.OK, data=resolvers.DEFAULT_BENCH_PATH)

    if re_resolved.status is Status.OK:
        emit(OpenTrace(text=f"Using cached bench path from inspect: {re_resolved.data}"))
    return re_resolved


def _resolve_app_dir(
    project_name: str,
    frappe_container,
    bench_path: str,
    app: str,
    emit: OnEvent,
) -> str:
    """The ``--app`` pass, moved verbatim from the old frontend.

    The cached bench is matched BY PATH against the bench being opened, never
    ``bench_instances[0]`` - ``--path`` may name a non-default bench, and
    anchoring to ``[0]`` would validate ``--app`` against one bench and then
    open another (the pinned wrong-bench bug).
    """
    benches = resolvers.cached_benches(project_name)
    if not benches:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "bench.no_cache",
            f"No cached bench data found. Run 'cwcli inspect {project_name}' first.",
        )

    bench_instance = next((b for b in benches if b.get("path") == bench_path), None)
    if bench_instance is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "bench.not_cached",
            f"Bench '{bench_path}' not found in cached data for '{project_name}'. "
            f"Run 'cwcli inspect {project_name}' first.",
        )
    available_apps = bench_instance.get("available_apps", [])

    # Run inspect's read-only T2 partial pass IN-MEMORY over the known benches so
    # a just-installed app is visible here without a manual `cwcli inspect -u`.
    # We deliberately do NOT persist it: leaving the cache untouched lets the next
    # plain `cwcli inspect` self-heal via escalate-on-drift (refreshing the deep
    # per-site installed lists too). Degrade to the cached list on any error so a
    # transient failure never blocks opening the app.
    try:
        refreshed, _drift = core_inspect.partial_refresh(frappe_container, benches)
        # partial_refresh drops any vanished bench, so `refreshed` may be
        # index-shifted relative to the cached list; match by path to the SAME
        # bench the membership check is about rather than indexing [0].
        match = next((b for b in refreshed if b.get("path") == bench_instance["path"]), None)
        if match and match.get("available_apps"):
            available_apps = match["available_apps"]
    except Exception as e:  # noqa: BLE001 - degrade to the cached list, never block
        emit(OpenTrace(text=f"App-list refresh skipped: {e}"))

    if not available_apps:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "apps.none_cached",
            f"No apps found in cached data. Run 'cwcli inspect {project_name}' first.",
        )

    if app not in available_apps:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "app.not_found",
            f"App '{app}' not found in bench instance.",
            hint=f"Available apps: {', '.join(available_apps)}",
        )

    return f"{bench_path}/apps/{app}"
