"""``utils.daemon_identity`` - detached-daemon pid-file IDENTITY, shared.

Promoted verbatim out of ``utils/auto_inspect.py`` so cwcli's two detached host
daemons - the auto-inspect service and the persistent credential bridge - share
ONE copy of the liveness-plus-identity subtlety (the ``set_maintenance``
promotion precedent: two copies of a liveness check drift until one stops
checking). Every function is parameterized by the caller's own ``pid_file``
path; the daemons keep their own paths under ``cwcli_home()/run/``.

A liveness probe alone cannot tell a daemon from an unrelated process that later
inherited its recycled pid, so a signal (or, on Windows, an outright kill) would
land on the wrong process. The pid file therefore records the daemon's
process-creation-time token on a second line, and :func:`is_running` /
callers match BOTH pid and creation time before trusting or signalling it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def pid_alive(pid: int) -> bool:
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


def process_start_time(pid: int) -> str | None:
    """Return a stable per-process creation-time token, or None if unreadable.

    Paired with the pid this forms an IDENTITY. A liveness probe alone cannot
    tell our daemon from an unrelated process that later inherited its recycled
    pid, so a stop would signal (and on Windows outright kill) the wrong
    process. A process's creation time changes when the pid is reused, so
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


def read_daemon_record(pid_file: Path) -> tuple[int, str | None] | None:
    """Parse ``pid_file`` into (pid, recorded start-time), or None if unusable.

    Line 1 is the pid; line 2, when present, is the recorded creation-time token
    written by :func:`write_pid_file`. A pre-identity pid file (pid only) yields
    a None start-time and is handled by the liveness-only fallback in
    :func:`is_running`.
    """
    try:
        with open(pid_file) as f:
            lines = f.read().splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    start = lines[1].strip() if len(lines) > 1 and lines[1].strip() else None
    return pid, start


def clear_pid_file(pid_file: Path) -> None:
    """Remove ``pid_file`` if present (stale/recycled), tolerating a race."""
    pid_file.unlink(missing_ok=True)


def write_pid_file(pid_file: Path) -> None:
    """Write the current pid (and its creation time) to ``pid_file``.

    The creation-time second line is the daemon's IDENTITY: :func:`is_running`
    and callers match it against the live pid's current creation time so a
    recycled pid can never be mistaken for the daemon (see
    :func:`process_start_time`).
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    start = process_start_time(pid)
    with open(pid_file, "w") as f:
        f.write(str(pid))
        if start is not None:
            f.write("\n" + start)


def is_running(pid_file: Path) -> bool:
    """Whether the daemon recorded in ``pid_file`` is alive AND still itself.

    Verifies process IDENTITY, not just liveness: a live pid whose recorded
    creation time no longer matches has been recycled to a DIFFERENT process, so
    it is not our daemon and its stale pid file is cleared. When identity cannot
    be confirmed (creation time unreadable now), it is likewise treated as gone
    rather than risking a signal to the wrong process.
    """
    record = read_daemon_record(pid_file)
    if record is None:
        return False
    pid, recorded_start = record

    if not pid_alive(pid):
        clear_pid_file(pid_file)
        return False

    if recorded_start is not None:
        if process_start_time(pid) != recorded_start:
            # Recycled pid (different creation time) or an unconfirmable
            # identity: either way this is not our daemon.
            clear_pid_file(pid_file)
            return False
        return True

    # Pre-identity pid file (pid only): degrade to liveness rather than making an
    # already-running daemon unstoppable.
    return True


def get_pid(pid_file: Path) -> int | None:
    """The pid recorded in ``pid_file``, or None if there is no usable record."""
    record = read_daemon_record(pid_file)
    return record[0] if record else None
