"""``utils.cred_daemon`` - the detached, persistent git credential-bridge daemon.

The per-invocation bridge (``core/credbridge.py``) only lives around cwcli's own
``apps``/``init``/``update`` git fan-outs and tears everything down on exit, so
interactive git inside ``cwcli open`` (which HANDS THE PROCESS OVER via
``os.execvp`` - a thread cannot survive that) or a plain ``docker exec`` shell
never runs inside its window. This module is the fix: a detached host daemon that
serves the EXISTING, proven bridge logic (``credbridge.host_credential`` +
``credbridge._serve``, unchanged) on a STABLE per-instance socket, with a stable
container-side shim whose failure mode is SILENT EXIT 0. So a dead, stopped, or
never-started daemon degrades byte-identically to today's prompt behaviour - the
non-negotiable degradation contract (scout report §5.8).

Shape mirrors ``utils/auto_inspect.py`` (the sibling detached daemon): the
fork/setsid + ``_spawn_detached`` split, and the pid-file IDENTITY primitives
promoted to ``utils/daemon_identity.py`` and shared with it. What is NEW here:

* a small JSON REGISTRY (``credbridge-registry.json``) mapping project -> its
  workspace bind-mount host dir; the ensure step registers, the daemon prunes
  vanished workspaces;
* PER-BOOT rotation - AF_UNIX sockets are unlinked+rebound each boot, TCP shims
  are rewritten with a fresh loopback port + secret token each boot, so no stale
  file/config can authenticate against a new daemon;
* an AUDIT log - one line per request (timestamp, project, host, answered) and
  never a credential byte;
* the daemon serves MANY workspaces at once (one AF_UNIX listener thread each on
  native Linux; one loopback-TCP listener for the whole daemon under Docker
  Desktop), reconciled against the registry by a single polling main loop.

UI-pure by construction (imports no rich/typer/questionary); the desired-state
decisions live in ``core/cred_bridge.py`` and the renderers in
``commands/config.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import config_utils, daemon_identity

# All of the daemon's footprint lives under cwcli_home()/run so it honours the
# CWCLI_HOME override, exactly like the auto-inspect daemon.
PID_DIR = config_utils.cwcli_home() / "run"
PID_FILE = PID_DIR / "credbridge.pid"
LOG_FILE = PID_DIR / "credbridge.log"
AUDIT_FILE = PID_DIR / "credbridge-audit.log"
REGISTRY_FILE = PID_DIR / "credbridge-registry.json"


@dataclass(frozen=True, slots=True)
class EnsureOutcome:
    """What one ``ensure_bridge`` call did (all serializable, no live object)."""

    project: str
    workspace: str
    config_value: str
    added_config: bool
    daemon_started: bool


def _ensure_run_dir() -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)


def _log(message: str, *, exc_info: bool = False) -> None:
    """Append a diagnostic line to the daemon log (the only place a detached
    daemon can report anything). ``exc_info`` appends the active traceback."""
    if exc_info:
        message = f"{message}\n{traceback.format_exc().rstrip()}"
    try:
        _ensure_run_dir()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError as e:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {message}", file=sys.stderr)
        print(f"Logging error: {e}", file=sys.stderr)


# --------------------------------------------------------------------- config gate


def is_enabled() -> bool:
    """Whether the persistent bridge is enabled, via a NO-CREATE config read.

    The ensure step calls this on the hot path (every ``cwcli open`` /
    ``core.start``), so it must never write a default config file into the user's
    home as a side effect - a missing config just reads as disabled.
    """
    return bool(config_utils.read_cred_bridge_config().get("enabled", False))


# ------------------------------------------------------------------------ registry


def _read_registry() -> dict[str, str]:
    """The project -> workspace-host-dir map, or {} if unreadable/absent."""
    try:
        with open(REGISTRY_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_registry(registry: dict[str, str]) -> None:
    """Write the registry atomically (tmp file + replace) so a reader never sees
    a half-written file."""
    _ensure_run_dir()
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(registry, f)
    os.replace(tmp, REGISTRY_FILE)


def _register(project: str, workspace: str) -> None:
    registry = _read_registry()
    if registry.get(project) != workspace:
        registry[project] = workspace
        _write_registry(registry)


def deregister(project: str) -> None:
    """Drop a project from the registry (e.g. on ``cwcli rm``); a no-op if absent.

    The daemon also prunes a vanished workspace lazily, so this is belt-and-
    suspenders - a removed project whose workspace dir is gone drops out on the
    next poll regardless.
    """
    registry = _read_registry()
    if project in registry:
        del registry[project]
        _write_registry(registry)


def registered_projects() -> list[str]:
    return sorted(_read_registry())


def _prune_vanished(registry: dict[str, str]) -> dict[str, str]:
    """Drop registry rows whose workspace host dir no longer exists, persisting
    the pruned map so the file reflects reality for ``status``."""
    kept = {p: ws for p, ws in registry.items() if Path(ws).is_dir()}
    if kept != registry:
        with contextlib.suppress(OSError):
            _write_registry(kept)
    return kept


# --------------------------------------------------------------------------- audit


def _audit(project: str | None, host: str, answered: bool) -> None:
    """One audit line per request: timestamp, project, host, answered - and NEVER
    a credential byte (only the host string and a bool ever reach here)."""
    try:
        _ensure_run_dir()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{timestamp}] project={project or '-'} "
            f"host={host or '-'} answered={'yes' if answered else 'no'}\n"
        )
        with open(AUDIT_FILE, "a") as f:
            f.write(line)
    except OSError:
        pass


def recent_audit(lines: int = 10) -> list[str]:
    """The last ``lines`` audit lines (newest last), or [] if none yet."""
    try:
        with open(AUDIT_FILE) as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-lines:]]
    except OSError:
        return []


# ------------------------------------------------------------------- transport read


def transport() -> str:
    """``"tcp"`` under Docker Desktop, ``"unix"`` on native Linux."""
    from ..core import credbridge

    return "tcp" if credbridge._prefer_tcp() else "unix"


# ------------------------------------------------------------------ the ensure step


def _ensure_container_config(container, container_dir: str) -> tuple[str, bool]:
    """Ensure the container's SYSTEM git config points at the stable shim.

    Written at ``--system`` (``/etc/gitconfig``, via ``exec_run(user="root")``),
    not ``--global``: the persistent bridge must serve ANY shell/editor user
    (VS Code attached-container sessions may run as a different user), and the
    system config is git's lowest-priority helper source, so a user's own helpers
    still run first. Idempotent: grep the existing values before adding, so a
    double-ensure adds exactly one line. Returns ``(config_value, added)``.
    """
    from ..core import credbridge

    helper_container = f"{container_dir}/{credbridge.PERSISTENT_HELPER_NAME}"
    config_value = f"!/usr/bin/python3 {helper_container}"
    existing = ""
    with contextlib.suppress(Exception):
        code, out = container.exec_run(
            ["git", "config", "--system", "--get-all", "credential.helper"], user="root"
        )
        existing = (
            out.decode("utf-8", "replace")
            if isinstance(out, (bytes, bytearray))
            else str(out or "")
        )
    if config_value in existing.splitlines():
        return config_value, False
    with contextlib.suppress(Exception):
        container.exec_run(
            ["git", "config", "--system", "--add", "credential.helper", config_value],
            user="root",
        )
    return config_value, True


def ensure_bridge(container, bench_path: str, project_name: str) -> EnsureOutcome | None:
    """Idempotently wire a project's instance into the persistent bridge.

    A no-op returning ``None`` when the feature is disabled or no workspace bind
    mount can be resolved. Otherwise: register project -> workspace, write the
    stable shim into the workspace mount, ensure the container's system git
    config, and auto-start the daemon if it is not already up (the
    ``config auto-inspect`` self-heal pattern). Fully defensive - it NEVER raises,
    so a bridge hiccup can never break the ``open``/``start`` it rides on.
    """
    try:
        if not is_enabled():
            return None
        from ..core import credbridge

        resolved = credbridge._resolve_workspace_mount(container, bench_path)
        if resolved is None:
            return None
        host_dir, container_dir = resolved

        _register(project_name, str(host_dir))

        helper_host = host_dir / credbridge.PERSISTENT_HELPER_NAME
        if credbridge._prefer_tcp():
            # TCP: the live port+token are the running daemon's to know, so it
            # rewrites this shim on its next poll/boot. Until then a placeholder
            # inert stub keeps the file present and fail-silent (never a prompt-
            # breaking dangling helper).
            if not helper_host.exists():
                with contextlib.suppress(OSError):
                    helper_host.write_text(credbridge.persistent_helper_stub())
        else:
            with contextlib.suppress(OSError):
                helper_host.write_text(credbridge.persistent_helper_unix())

        config_value, added = _ensure_container_config(container, container_dir)

        daemon_started = False
        if not is_running():
            start_daemon()
            daemon_started = True

        return EnsureOutcome(
            project=project_name,
            workspace=str(host_dir),
            config_value=config_value,
            added_config=added,
            daemon_started=daemon_started,
        )
    except Exception:
        _log(f"ensure_bridge({project_name}) failed", exc_info=True)
        return None


# A representative bench path under the workspace bind mount. The bridge is scoped
# per-INSTANCE (per workspace mount), and mount resolution picks the bind mount
# that is an ancestor of any path under it, so this default resolves the same
# workspace mount every bench in the instance shares - enough for `ensure` to find
# the mount and write the instance-level shim + system git config.
_DEFAULT_BENCH_PATH = "/workspace/frappe-bench"


def ensure_running_instances() -> int:
    """Best-effort: ``ensure_bridge`` every currently-running frappe instance.

    Called by ``enable`` so the bridge takes effect immediately rather than only
    on the next ``open``/``start`` of each instance. Returns how many instances
    were ensured; swallows all Docker errors (0 on any failure).
    """
    if not is_enabled():
        return 0
    try:
        import docker

        client = docker.from_env()
        containers = client.containers.list(
            filters={"label": "com.docker.compose.service=frappe", "status": "running"}
        )
    except Exception:
        return 0
    count = 0
    for container in containers:
        project = container.labels.get("com.docker.compose.project")
        if not project:
            continue
        if ensure_bridge(container, _DEFAULT_BENCH_PATH, project) is not None:
            count += 1
    return count


def disable_bridge_artifacts() -> None:
    """Disable hygiene: make every registered shim inert, unlink sockets, and
    best-effort unset the config line in RUNNING containers, then clear registry.

    The inert-stub rewrite and socket unlink are host-side writes that work
    regardless of container state, so a stopped container that missed the config
    unset keeps only a helper line pointing at a shim that answers nothing -
    silent and harmless (never a dangling helper that hard-errors git).
    """
    from ..core import credbridge

    registry = _read_registry()
    for project, ws in registry.items():
        host_dir = Path(ws)
        with contextlib.suppress(OSError):
            (host_dir / credbridge.PERSISTENT_HELPER_NAME).write_text(
                credbridge.persistent_helper_stub()
            )
        with contextlib.suppress(OSError):
            (host_dir / credbridge.PERSISTENT_SOCK_NAME).unlink(missing_ok=True)
        _best_effort_container_unset(project)
    _write_registry({})


def _best_effort_container_unset(project: str) -> None:
    """Try to unset the persistent helper line in a project's RUNNING frappe
    container. Swallows everything - the inert stub already makes a missed unset
    harmless."""
    from ..core import credbridge

    try:
        import docker

        client = docker.from_env()
        containers = client.containers.list(
            filters={"label": f"com.docker.compose.project={project}", "status": "running"}
        )
    except Exception:
        return
    helper_marker = credbridge.PERSISTENT_HELPER_NAME.lstrip(".")
    for container in containers:
        if container.labels.get("com.docker.compose.service") != "frappe":
            continue
        with contextlib.suppress(Exception):
            container.exec_run(
                ["git", "config", "--system", "--unset-all", "credential.helper", helper_marker],
                user="root",
            )


# -------------------------------------------------------------------- serving loop


@dataclass
class _Listener:
    """One live AF_UNIX workspace listener (its socket + serving thread)."""

    workspace: str
    srv: socket.socket
    thread: threading.Thread


def _start_unix_listener(
    workspace: str, project: str | None, stop: threading.Event, credbridge
) -> _Listener | None:
    """Bind ``<workspace>/.cwcli-git-cred.sock`` and serve it in a thread.

    Per-boot rotation: any stale socket file is unlinked and rebound, and the
    shim is (re)written so a container recreation self-heals. Binds the short
    RELATIVE name from inside the mount dir because AF_UNIX sun_path caps at ~108
    bytes and a long CWCLI_HOME overflows it (the per-invocation bridge's trick).
    """
    host_dir = Path(workspace)
    sock_path = host_dir / credbridge.PERSISTENT_SOCK_NAME
    with contextlib.suppress(FileNotFoundError):
        sock_path.unlink()
    with contextlib.suppress(OSError):
        (host_dir / credbridge.PERSISTENT_HELPER_NAME).write_text(
            credbridge.persistent_helper_unix()
        )

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    prev_cwd = os.getcwd()
    try:
        os.chdir(host_dir)
        srv.bind(credbridge.PERSISTENT_SOCK_NAME)
    except OSError as e:
        srv.close()
        _log(f"could not bind socket in {workspace}: {e!r}")
        return None
    finally:
        os.chdir(prev_cwd)
    with contextlib.suppress(OSError):
        sock_path.chmod(0o666)  # container frappe user connects regardless of uid
    srv.settimeout(credbridge._ACCEPT_TIMEOUT)
    srv.listen(16)

    def _cb(host: str, answered: bool, _p: str | None = project) -> None:
        _audit(_p, host, answered)

    thread = threading.Thread(
        target=credbridge._serve, args=(srv, stop, None), kwargs={"audit": _cb}, daemon=True
    )
    thread.start()
    return _Listener(workspace=str(host_dir), srv=srv, thread=thread)


def _reconcile_unix_listeners(
    listeners: dict[str, _Listener], registry: dict[str, str], stop: threading.Event, credbridge
) -> None:
    """Bring the live AF_UNIX listener set in line with the registry: start one
    for each newly-registered workspace, stop and unlink the socket of any whose
    workspace left the registry."""
    ws_to_project: dict[str, str] = {}
    for project, ws in registry.items():
        ws_to_project.setdefault(ws, project)
    wanted = set(ws_to_project)

    for ws in wanted:
        if ws in listeners:
            continue
        listener = _start_unix_listener(ws, ws_to_project.get(ws), stop, credbridge)
        if listener is not None:
            listeners[ws] = listener

    for ws in list(listeners):
        if ws not in wanted:
            with contextlib.suppress(Exception):
                listeners[ws].srv.close()
            del listeners[ws]
            with contextlib.suppress(OSError):
                (Path(ws) / credbridge.PERSISTENT_SOCK_NAME).unlink(missing_ok=True)


def _rewrite_tcp_shims(registry: dict[str, str], port: int, token: bytes, credbridge) -> None:
    """Rewrite each registered workspace's shim with this boot's loopback port and
    token (the TCP transport's per-boot rotation)."""
    for ws in set(registry.values()):
        with contextlib.suppress(OSError):
            (Path(ws) / credbridge.PERSISTENT_HELPER_NAME).write_text(
                credbridge.persistent_helper_tcp(port, token)
            )


def _run_daemon_loop() -> None:
    """The daemon body: stand up listeners/transport, then poll the registry and
    reconcile until stopped. Runs in the detached child process."""
    from ..core import credbridge

    stop = threading.Event()

    def _on_term(signum, frame):
        # Reset handlers first so a second signal can't re-enter teardown, then
        # ask the loop to exit (never signal our own pid - the SIGTERM-recursion
        # lesson from auto_inspect).
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
        stop.set()

    with contextlib.suppress(Exception):
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)

    tcp = credbridge._prefer_tcp()
    listeners: dict[str, _Listener] = {}
    tcp_srv: socket.socket | None = None
    port: int | None = None
    token: bytes | None = None

    if tcp:
        tcp_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_srv.bind(("127.0.0.1", 0))  # loopback only, never routable
        port = tcp_srv.getsockname()[1]
        token = secrets.token_hex(16).encode()
        tcp_srv.settimeout(credbridge._ACCEPT_TIMEOUT)
        tcp_srv.listen(16)

        def _tcp_cb(host: str, answered: bool) -> None:
            _audit(None, host, answered)  # one shared TCP port can't attribute project

        threading.Thread(
            target=credbridge._serve,
            args=(tcp_srv, stop, token),
            kwargs={"audit": _tcp_cb},
            daemon=True,
        ).start()
        _log(f"credential-bridge daemon serving loopback TCP :{port}")
    else:
        _log("credential-bridge daemon serving AF_UNIX sockets")

    last_registry: dict[str, str] | None = None
    while not stop.is_set():
        try:
            registry = _prune_vanished(_read_registry())
            if registry != last_registry:
                if tcp:
                    assert port is not None and token is not None
                    _rewrite_tcp_shims(registry, port, token, credbridge)
                else:
                    _reconcile_unix_listeners(listeners, registry, stop, credbridge)
                last_registry = registry
        except Exception as e:
            _log(f"registry reconcile error: {e!r}", exc_info=True)
        stop.wait(credbridge._ACCEPT_TIMEOUT)

    # Teardown: closing each socket breaks its _serve accept loop.
    for listener in listeners.values():
        with contextlib.suppress(Exception):
            listener.srv.close()
    if tcp_srv is not None:
        with contextlib.suppress(Exception):
            tcp_srv.close()
    _clear_pid_file()
    _log("credential-bridge daemon stopped")


# ------------------------------------------------------------------ pid + lifecycle


def _write_pid_file() -> None:
    daemon_identity.write_pid_file(PID_FILE)


def _clear_pid_file() -> None:
    daemon_identity.clear_pid_file(PID_FILE)


def is_running() -> bool:
    """Whether OUR daemon is alive (identity-checked, prunes a stale pid file)."""
    return daemon_identity.is_running(PID_FILE)


def get_pid() -> int | None:
    return daemon_identity.get_pid(PID_FILE)


def _bootstrap_source(module_path: str) -> str:
    """The Python source the detached child runs where ``os.fork`` is unavailable
    (Windows). ``{module_path!r}`` is a literal so Windows backslashes survive."""
    return (
        "import sys\n"
        f"sys.path.insert(0, {module_path!r})\n"
        "from caffeinated_whale_cli.utils.cred_daemon import "
        "_write_pid_file, _log, _run_daemon_loop\n"
        "_write_pid_file()\n"
        '_log("credential-bridge daemon started")\n'
        "_run_daemon_loop()\n"
    )


def _spawn_detached() -> None:
    """Run the daemon loop in a detached child (the no-fork path).

    Child stderr goes to the LOG FILE (not DEVNULL) so a child that dies in its
    bootstrap leaves a diagnosable trace - the auto_inspect lesson.
    """
    _ensure_run_dir()
    source = _bootstrap_source(str(Path(__file__).parent.parent.parent))

    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008  # noqa: N806
        CREATE_NEW_PROCESS_GROUP = 0x00000200  # noqa: N806
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        new_session = False
    else:
        creationflags = 0
        new_session = True

    with open(LOG_FILE, "a") as log:
        subprocess.Popen(
            [sys.executable, "-c", source],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            creationflags=creationflags,
            start_new_session=new_session,
        )


def _acquire_startup_lock() -> int | None:
    """Take an exclusive, blocking lock over the whole check-then-start window.

    The ensure step calls ``start_daemon`` speculatively on every ``cwcli open`` /
    ``core.start`` (and ``enable`` -> ``ensure_running_instances``), so without
    serialization two concurrent invocations can each see ``is_running() == False``
    and fork RIVAL daemons - whichever writes the pid file last orphans the other,
    which can then never be stopped (``stop``/``disable`` only signal the recorded
    pid) and both race the same socket. This lock closes that window.

    Returns the held fd (release with :func:`_release_startup_lock`), or ``None``
    when locking is unavailable - in which case the caller proceeds UNSERIALIZED
    rather than refusing to start (a bridge that never comes up is worse than the
    narrow double-fork race). CRASH-SAFE by construction: ``flock`` / ``msvcrt``
    locks are released by the kernel when the holder dies, so a lock file left by
    a killed starter never permanently bricks startup.
    """
    try:
        _ensure_run_dir()
        fd = os.open(PID_DIR / "credbridge.start.lock", os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    return fd


def _release_startup_lock(fd: int | None) -> None:
    """Release the startup lock; POSIX ``flock`` is freed simply by closing it."""
    if fd is None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    with contextlib.suppress(OSError):
        os.close(fd)


def _await_started(timeout: float = 2.0) -> None:
    """Poll until the freshly-forked/spawned daemon has written its pid file.

    The startup lock is held across this wait, so a concurrent caller blocked on
    the lock observes ``is_running() == True`` when it finally acquires and no-ops
    instead of forking a rival. Bounded (ponytail: 2s ceiling) so a child that
    dies in its bootstrap can never keep startup blocked.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.02)


def _fork_or_spawn(lock_fd: int | None) -> None:
    """Fork+setsid (POSIX) or detached spawn (no-fork) the daemon, holding the
    startup lock until it is confirmed up."""
    try:
        pid = os.fork()
    except (AttributeError, OSError):
        _spawn_detached()  # Popen(close_fds=True) never inherits the lock fd
        _await_started()
        return

    if pid > 0:
        _await_started()  # parent: hold the lock until the child's pid file lands
        return

    # Child: drop the inherited startup-lock fd FIRST. It shares the parent's open
    # file description, and the daemon loop never returns, so leaving it open would
    # pin the flock forever and brick the next start. Then detach, redirect real
    # fds (stderr -> log, so a crash leaves a trace), write the pid file, run.
    if lock_fd is not None:
        with contextlib.suppress(OSError):
            os.close(lock_fd)
    try:
        os.setsid()
        _ensure_run_dir()
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(devnull_fd, 0)
        os.dup2(devnull_fd, 1)
        os.dup2(log_fd, 2)
        os.close(devnull_fd)
        os.close(log_fd)
        _write_pid_file()
        _log("credential-bridge daemon started")
        _run_daemon_loop()
        code = 0
    except BaseException:
        _log("credential-bridge daemon exited abnormally", exc_info=True)
        code = 1
    os._exit(code)


def start_daemon() -> None:
    """Start the credential-bridge daemon if it is not already running.

    Idempotent (unlike auto_inspect's raise-if-running): the ensure step calls
    this speculatively on every open/start, so an already-up daemon is a clean
    no-op. Startup is serialized by a crash-safe filesystem lock held across the
    ``is_running`` check AND the fork/spawn, so concurrent open/start/enable calls
    can never double-fork. Forks + setsids on POSIX; falls back to a detached
    spawn where ``os.fork`` is unavailable (Windows) or fails.
    """
    if is_running():
        return

    lock_fd = _acquire_startup_lock()
    try:
        # Re-check UNDER the lock: a racing caller that beat us to it has already
        # brought the daemon up, so we must not fork a second one.
        if is_running():
            return
        _fork_or_spawn(lock_fd)
    finally:
        _release_startup_lock(lock_fd)


def stop_daemon() -> None:
    """Stop the daemon if running; idempotent (a no-op when already stopped).

    Matches the identity gate: ``is_running`` has already confirmed the pid is
    OUR daemon before we signal it, so a recycled pid is never killed.
    """
    if not is_running():
        _clear_pid_file()
        return
    pid = get_pid()
    if pid:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not is_running():
                break
            time.sleep(0.1)
        if is_running():
            if sys.platform == "win32":
                with contextlib.suppress(Exception):
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
    _clear_pid_file()


def get_log_tail(lines: int = 20) -> str:
    """The last ``lines`` lines of the daemon diagnostic log."""
    if not LOG_FILE.exists():
        return "No log file found"
    with open(LOG_FILE) as f:
        return "".join(f.readlines()[-lines:])
