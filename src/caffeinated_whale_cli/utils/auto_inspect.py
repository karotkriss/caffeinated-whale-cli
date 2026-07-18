"""
Auto-inspection service for periodically inspecting running Frappe projects.

This module provides a background service that automatically inspects all running
Frappe projects at configurable intervals to keep cached data fresh for tab completion
and other features.
"""

import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import docker

from . import config_utils

# PID file location (under cwcli_home() so it honors the CWCLI_HOME override
# like the rest of cwcli's footprint, rather than always the real ~/.cwcli)
PID_DIR = config_utils.cwcli_home() / "run"
PID_FILE = PID_DIR / "auto-inspect.pid"
LOG_FILE = PID_DIR / "auto-inspect.log"


def _ensure_pid_dir():
    """Ensure the PID directory exists."""
    PID_DIR.mkdir(parents=True, exist_ok=True)


def _pid_alive(pid: int) -> bool:
    """Report whether ``pid`` is a live process, WITHOUT signalling it.

    ``os.kill(pid, 0)`` is the standard POSIX liveness probe, and it is a valid
    one there: signal 0 is delivered to no handler and only the errno matters.
    On Windows it is not a probe at all, and NOT for the reason the docs first
    suggest. ``signal.CTRL_C_EVENT`` is literally ``0``, and CPython's
    ``os_kill_impl`` tests ``sig == CTRL_C_EVENT`` FIRST, so ``os.kill(pid, 0)``
    is routed to ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` and never
    reaches the ``TerminateProcess`` path that os.kill's "any other value for
    sig" sentence describes. Two things follow, both observed on a real Windows
    kernel (Python 3.14, win32) rather than argued from the docs:

    1. Per MSDN, ``GenerateConsoleCtrlEvent`` with CTRL_C_EVENT and a NONZERO
       process-group id SUCCEEDS but does not deliver to that group. So the call
       returns cleanly for a process that is already DEAD, and ``is_running()``
       reported a dead daemon as running and never cleared its stale PID file -
       after which ``auto-inspect start`` answered "already running" forever and
       the daemon never came back. Only a pid that never existed raises (OSError
       87, "The parameter is incorrect").
    2. The Ctrl+C it generates lands on the CALLER's console, so probing could
       interrupt the cwcli process doing the probing.

    ``WaitForSingleObject(handle, 0)`` is a genuine read-only probe: it needs
    only SYNCHRONIZE access and returns WAIT_TIMEOUT while the process is still
    running. It is preferred over GetExitCodeProcess, whose STILL_ACTIVE
    sentinel is 259 and so cannot tell a running process from one that exited
    with code 259.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000  # noqa: N806
        WAIT_TIMEOUT = 0x00000102  # noqa: N806

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declaring these is not optional: without an explicit restype, ctypes
        # assumes c_int and silently truncates a 64-bit HANDLE.
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # No such process, or it belongs to another user. Both mean "not our
            # live daemon", which matches what os.kill(pid, 0) reports on POSIX
            # (ESRCH / EPERM both raise OSError there).
            return False
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT)
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_start_time(pid: int) -> str | None:
    """Return a stable per-process creation-time token, or None if unreadable.

    Paired with the pid this forms an IDENTITY. A liveness probe alone cannot
    tell our daemon from an unrelated process that later inherited its recycled
    pid, so ``stop_daemon`` would signal (and on Windows outright kill) the
    wrong process. A process's creation time changes when the pid is reused, so
    matching BOTH pid and creation time distinguishes the two. Cross-platform by
    necessity: the pid file is read on POSIX and Windows alike.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation, exit_t, kernel_t, user_t = (FILETIME(), FILETIME(), FILETIME(), FILETIME())
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            if not ok:
                return None
            return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}"
        finally:
            kernel32.CloseHandle(handle)

    # Linux: /proc/<pid>/stat field 22 is starttime (clock ticks since boot), a
    # stable per-process token that changes when the pid is reused. comm (field
    # 2) is parenthesized and may itself contain spaces/parens, so the fields
    # after the final ')' are what parse at fixed positions.
    try:
        with open(f"/proc/{pid}/stat") as f:
            after = f.read().rpartition(")")[2].split()
        return after[19]  # field 22 == index 19, counting from field 3 (state)
    except (OSError, IndexError):
        pass

    # POSIX without /proc (macOS/BSD): fall back to ps lstart. Second-grained and
    # weaker, but still distinguishes a pid recycled over the daemon's lifetime.
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None


def _read_daemon_record() -> tuple[int, str | None] | None:
    """Parse the pid file into (pid, recorded start-time), or None if unusable.

    Line 1 is the pid; line 2, when present, is the recorded creation-time token
    written by ``_write_pid_file``. A pre-identity pid file (pid only) yields a
    None start-time and is handled by the liveness-only fallback in is_running().
    """
    try:
        with open(PID_FILE) as f:
            lines = f.read().splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    start = lines[1].strip() if len(lines) > 1 and lines[1].strip() else None
    return pid, start


def _clear_pid_file() -> None:
    """Remove the pid file if present (stale/recycled), tolerating a race."""
    PID_FILE.unlink(missing_ok=True)


def is_running() -> bool:
    """Check if the auto-inspect service is currently running.

    Verifies process IDENTITY, not just liveness: a live pid whose recorded
    creation time no longer matches has been recycled to a DIFFERENT process, so
    it is not our daemon and its stale pid file is cleared. When identity cannot
    be confirmed (creation time unreadable now), it is likewise treated as gone
    rather than risking a signal to the wrong process.
    """
    record = _read_daemon_record()
    if record is None:
        return False
    pid, recorded_start = record

    if not _pid_alive(pid):
        _clear_pid_file()
        return False

    if recorded_start is not None:
        if _process_start_time(pid) != recorded_start:
            # Recycled pid (different creation time) or an unconfirmable
            # identity: either way this is not our daemon.
            _clear_pid_file()
            return False
        return True

    # Pre-identity pid file (pid only): degrade to liveness rather than making an
    # already-running daemon unstoppable.
    # ponytail: this fallback retires once no pre-identity pid files can exist.
    return True


def get_pid() -> int | None:
    """Get the PID of the running auto-inspect service."""
    record = _read_daemon_record()
    return record[0] if record else None


def _log(message: str, *, exc_info: bool = False):
    """Write a message to the log file.

    ``exc_info=True`` appends the active traceback. Every handler here used to
    log a bare ``{e}``, which drops the exception TYPE and the traceback - so a
    ``typer.Exit`` (a RuntimeError subclass) from the inspect call logged as an
    all-but-empty string, and nothing recorded where a failure came from.
    """
    if exc_info:
        message = f"{message}\n{traceback.format_exc().rstrip()}"
    try:
        _ensure_pid_dir()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError as e:
        # Fallback to stderr if logging fails
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {message}", file=sys.stderr)
        print(f"Logging error: {e}", file=sys.stderr)


def _get_running_projects() -> list[str]:
    """Get list of currently running Frappe projects."""
    try:
        client = docker.from_env()
        containers = client.containers.list(
            filters={"label": "com.docker.compose.service=frappe", "status": "running"}
        )

        projects = set()
        for container in containers:
            project_name = container.labels.get("com.docker.compose.project")
            if project_name:
                projects.add(project_name)

        return sorted(projects)
    except Exception as e:
        _log(f"Error getting running projects: {e!r}", exc_info=True)
        return []


def _inspect_project(project_name: str) -> bool:
    """Inspect a single project and update its cache."""
    try:
        # Import here to avoid circular imports
        from ..core import inspect as core_inspect

        # A full core inspect owns the cache write; offer_choice=False keeps the
        # daemon non-interactive (a stopped project raises instead of prompting).
        core_inspect.inspect(project_name, refresh="full", offer_choice=False)
        return True
    except Exception as e:
        _log(f"Error inspecting project {project_name}: {e!r}", exc_info=True)
        return False


def _run_inspection_cycle():
    """Run one complete inspection cycle for all running projects."""
    _log("Starting inspection cycle")

    projects = _get_running_projects()
    if not projects:
        _log("No running projects found")
        return

    _log(f"Found {len(projects)} running project(s): {', '.join(projects)}")

    for project in projects:
        _log(f"Inspecting {project}...")
        success = _inspect_project(project)
        if success:
            _log(f"Successfully inspected {project}")
        else:
            _log(f"Failed to inspect {project}")

    _log("Inspection cycle completed")


def _bootstrap_source(module_path: str, interval: int) -> str:
    """Build the Python source the detached child runs to host the service loop.

    ``{module_path!r}`` is a Python string literal, so Windows backslashes and
    any quoting survive verbatim without a json round-trip.
    """
    return (
        "import sys\n"
        f"sys.path.insert(0, {module_path!r})\n"
        "from caffeinated_whale_cli.utils.auto_inspect import "
        "_write_pid_file, _log, _run_service_loop\n"
        "_write_pid_file()\n"
        f'_log("Auto-inspect service started (interval: {interval}s)")\n'
        f"_run_service_loop({interval})\n"
    )


def _spawn_detached(interval: int):
    """Run the service loop in a detached child, where os.fork() is unavailable.

    The child's stderr goes to the LOG FILE rather than DEVNULL. That redirect is
    the point of this function: Popen does not wait, so a child that died in its
    bootstrap (bad sys.path, failed import) wrote its traceback to DEVNULL and
    vanished, while the parent went on to print "Auto-inspect background process
    started." No PID file, no log line, no error text anywhere - which is exactly
    why the Windows failures here were never diagnosable.

    stdout stays on DEVNULL: the inspect call underneath prints a rich tree, and
    that is UI output, not diagnostics.
    """
    _ensure_pid_dir()
    source = _bootstrap_source(str(Path(__file__).parent.parent.parent), interval)

    # Each platform ignores the other's detach knob at its default (Popen only
    # rejects creationflags on POSIX / start_new_session on Windows when they are
    # actually set), so both can be passed unconditionally and the call stays one
    # copy instead of two near-identical branches.
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


def start_daemon():
    """Start the auto-inspect daemon process."""
    if is_running():
        raise RuntimeError("Auto-inspect service is already running")

    config = config_utils.get_auto_inspect_config()
    if not config.get("enabled"):
        raise RuntimeError("Auto-inspect is not enabled in configuration")

    # config.toml is hand-editable, so coerce before this reaches the child's
    # generated source, where a non-int would emit a syntax error.
    interval = int(config.get("interval", 3600))

    # ONLY the fork() call is guarded: os.fork is absent on Windows
    # (AttributeError) and can fail under resource limits (OSError). This try
    # used to wrap the entire child body down to _run_service_loop, so an OSError
    # escaping the *running child* dropped it into the fallback and spawned a
    # SECOND daemon on top of itself.
    try:
        pid = os.fork()
    except (AttributeError, OSError):
        _spawn_detached(interval)
        return

    if pid > 0:
        # Parent process - just return
        return

    # Child process - detach and run service
    try:
        os.setsid()

        # Redirect the REAL file descriptors, not just the sys.* objects. The
        # child inherits the parent's stdout/stderr fds, and a shell capturing
        # the parent's output (`$(cwcli config auto-inspect enable)`, any pipe)
        # waits for EOF on that pipe - which never comes while the daemon holds
        # fd 1 open. Rebinding sys.stdout alone left the fd open, so every
        # script that captured the enabling command's output hung forever.
        # stdin/stdout go to devnull; stderr goes to the LOG FILE (mirroring
        # _spawn_detached) so a crash below _log's reach still leaves a trace.
        _ensure_pid_dir()
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(devnull_fd, 0)
        os.dup2(devnull_fd, 1)
        os.dup2(log_fd, 2)
        os.close(devnull_fd)
        os.close(log_fd)

        # Write PID file
        _write_pid_file()

        # Set up signal handlers
        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

        # Log startup
        _log(f"Auto-inspect service started (interval: {interval}s)")

        # Run service loop
        _run_service_loop(interval)
        code = 0
    except SystemExit as e:
        # The normal stop path: _handle_sigterm raises this via sys.exit(0).
        code = e.code if isinstance(e.code, int) else 0
    except BaseException:
        # sys.stderr is devnull by this point, so an unhandled crash would leave
        # no trace at all. Log it before the child goes.
        _log("Auto-inspect daemon exited abnormally", exc_info=True)
        code = 1

    # The child must never return into the CLI's control flow and start printing
    # the parent's success messages, so leave via os._exit rather than falling
    # off the end of start_daemon().
    os._exit(code)


def _write_pid_file():
    """Write the current process ID (and its creation time) to the PID file.

    The creation-time second line is the daemon's IDENTITY: is_running() and
    stop_daemon() match it against the live pid's current creation time so a
    recycled pid can never be mistaken for the daemon (see _process_start_time).
    """
    _ensure_pid_dir()
    pid = os.getpid()
    start = _process_start_time(pid)
    with open(PID_FILE, "w") as f:
        f.write(str(pid))
        if start is not None:
            f.write("\n" + start)


def _run_service_loop(interval: int):
    """Run the main service loop."""
    while True:
        try:
            _run_inspection_cycle()
        except Exception as e:
            _log(f"Error in inspection cycle: {e!r}", exc_info=True)

        # Sleep until next inspection
        time.sleep(interval)


def _handle_sigterm(signum, frame):
    """Handle termination signal from WITHIN the daemon.

    This runs inside the daemon process being asked to stop, so it must NOT call
    stop_daemon(): that function os.kill()s the stored pid - which is our own -
    re-entering this handler until it hit RecursionError (seen live on Linux).
    Instead reset the handlers to default first (so a second SIGTERM can't re-run
    teardown), clean up our own pid file, and exit. The default disposition then
    handles any signal that races in during the exit.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    _log("Auto-inspect service received termination signal")
    _clear_pid_file()
    sys.exit(0)


def stop_daemon():
    """Stop the auto-inspect daemon process."""
    if not is_running():
        raise RuntimeError("Auto-inspect service is not running")

    pid = get_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            _log("Auto-inspect service stopped")

            # Wait for process to terminate
            for _ in range(10):
                if not is_running():
                    break
                time.sleep(0.1)

            # Force kill if still running
            if is_running():
                if sys.platform == "win32":
                    # On Windows, use taskkill for force termination
                    import subprocess

                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, signal.SIGKILL)
                _log("Auto-inspect service force killed")
        except OSError as e:
            _log(f"Error stopping service: {e!r}", exc_info=True)
            raise

    # Remove PID file
    _clear_pid_file()


def get_log_tail(lines: int = 20) -> str:
    """Get the last N lines from the log file."""
    if not LOG_FILE.exists():
        return "No log file found"

    with open(LOG_FILE) as f:
        all_lines = f.readlines()
        return "".join(all_lines[-lines:])
