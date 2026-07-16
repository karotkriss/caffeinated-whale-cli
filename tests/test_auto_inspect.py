"""Tests for the auto-inspect daemon's process handling and diagnostics.

These are deliberately cross-platform and mock-free: the bugs they pin are
Windows-only, and a mocked platform proves nothing about the real one. The
suite therefore runs on the `windows-latest` job in `.github/workflows/test.yml`
as well as on Linux, so a real Windows kernel - not a stand-in for one - is what
holds `_pid_alive` honest.
"""

import os
import subprocess
import sys
import time

import pytest

from caffeinated_whale_cli.utils import auto_inspect


@pytest.fixture
def isolated_run_dir(tmp_path, monkeypatch):
    """Point the daemon's PID/log files at a temp dir instead of ~/.cwcli/run."""
    monkeypatch.setattr(auto_inspect, "PID_DIR", tmp_path)
    monkeypatch.setattr(auto_inspect, "PID_FILE", tmp_path / "auto-inspect.pid")
    monkeypatch.setattr(auto_inspect, "LOG_FILE", tmp_path / "auto-inspect.log")
    return tmp_path


@pytest.fixture
def live_process():
    """A real child process that stays alive for the duration of the test."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _dead_pid() -> int:
    """Return a PID that is definitely not a live process."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# The liveness probe.
#
# os.kill(pid, 0) is a valid probe on POSIX but not on Windows, where
# signal.CTRL_C_EVENT == 0 routes it to GenerateConsoleCtrlEvent instead of the
# TerminateProcess path. Verified on a real Windows kernel: it SUCCEEDS for an
# already-dead pid, so the daemon reported itself running forever off a stale
# PID file, and the Ctrl+C it raises lands on the caller's own console.
#
# `test_pid_alive_reports_false_for_a_dead_process` and
# `test_is_running_is_false_and_clears_the_pid_file_when_stale` are the two that
# actually FAIL against the unfixed code on the windows-latest job; on Linux
# they pass either way, which is the whole reason that job exists.
# ---------------------------------------------------------------------------


def test_pid_alive_reports_true_for_a_live_process(live_process):
    """A live process reports alive."""
    assert auto_inspect._pid_alive(live_process.pid) is True


def test_pid_alive_reports_false_for_a_dead_process():
    """An exited process reports not-alive (on Windows the old probe said alive)."""
    assert auto_inspect._pid_alive(_dead_pid()) is False


def test_pid_alive_reports_false_for_a_pid_that_never_existed():
    """An implausible pid reports not-alive rather than raising."""
    assert auto_inspect._pid_alive(999999) is False


def test_pid_alive_leaves_the_process_it_probes_running(live_process):
    """Probing liveness is a read - the process must still be there afterwards."""
    assert auto_inspect._pid_alive(live_process.pid) is True
    assert auto_inspect._pid_alive(live_process.pid) is True

    with pytest.raises(subprocess.TimeoutExpired):
        live_process.wait(timeout=1)


def test_is_running_reports_a_live_daemon_and_leaves_it_running(isolated_run_dir, live_process):
    """is_running() is a read - the daemon survives being asked about, repeatedly."""
    auto_inspect.PID_FILE.write_text(str(live_process.pid))

    assert auto_inspect.is_running() is True
    assert auto_inspect.is_running() is True

    with pytest.raises(subprocess.TimeoutExpired):
        live_process.wait(timeout=1)


def test_is_running_is_false_and_clears_the_pid_file_when_stale(isolated_run_dir):
    """A PID file left by a dead daemon reports not-running and is removed.

    The Windows failure the captain reported: the old probe SUCCEEDED for a dead
    pid, so this returned True, the stale PID file was never cleared, and
    `auto-inspect start` refused with "already running" from then on.
    """
    auto_inspect.PID_FILE.write_text(str(_dead_pid()))

    assert auto_inspect.is_running() is False
    assert not auto_inspect.PID_FILE.exists()


def test_is_running_is_false_when_the_pid_file_is_garbage(isolated_run_dir):
    """A corrupt PID file reports not-running rather than raising."""
    auto_inspect.PID_FILE.write_text("not-a-pid")

    assert auto_inspect.is_running() is False


def test_is_running_is_false_when_there_is_no_pid_file(isolated_run_dir):
    """No PID file means no daemon."""
    assert auto_inspect.is_running() is False


# ---------------------------------------------------------------------------
# Diagnostics: the log is the only place a detached daemon can report anything.
# ---------------------------------------------------------------------------


def test_log_records_the_traceback_when_asked(isolated_run_dir):
    """exc_info=True puts the exception type and traceback in the log."""
    try:
        raise ValueError("the actual cause")
    except ValueError as e:
        auto_inspect._log(f"Something failed: {e!r}", exc_info=True)

    logged = auto_inspect.LOG_FILE.read_text()
    assert "ValueError('the actual cause')" in logged
    assert "Traceback (most recent call last)" in logged
    assert "test_log_records_the_traceback_when_asked" in logged


def test_log_omits_the_traceback_by_default(isolated_run_dir):
    """A routine log line stays a single timestamped line."""
    auto_inspect._log("Starting inspection cycle")

    logged = auto_inspect.LOG_FILE.read_text()
    assert "Starting inspection cycle" in logged
    assert "Traceback" not in logged


def test_inspect_failure_logs_the_exception_type_and_traceback(isolated_run_dir, monkeypatch):
    """A failing inspect records what actually went wrong, not a bare message."""
    import caffeinated_whale_cli.commands.inspect as inspect_module

    def boom(**kwargs):
        raise RuntimeError("docker exploded")

    monkeypatch.setattr(inspect_module, "inspect", boom)

    assert auto_inspect._inspect_project("some-project") is False

    logged = auto_inspect.LOG_FILE.read_text()
    assert "RuntimeError('docker exploded')" in logged
    assert "Traceback (most recent call last)" in logged


# ---------------------------------------------------------------------------
# The detached-child bootstrap. Generated source, so a bad literal is a syntax
# error in a process whose output nobody sees.
# ---------------------------------------------------------------------------


def test_bootstrap_source_is_valid_python():
    """The generated child source compiles."""
    source = auto_inspect._bootstrap_source("/home/user/site-packages", 3600)

    compile(source, "<bootstrap>", "exec")
    assert "_run_service_loop(3600)" in source


def test_bootstrap_source_survives_a_windows_path():
    """Backslashes in a Windows site-packages path round-trip intact."""
    windows_path = r"C:\Users\dev\AppData\Local\Programs\Python\Lib\site-packages"
    source = auto_inspect._bootstrap_source(windows_path, 60)

    # Embedded as a Python literal, so the backslashes are not eaten as escapes.
    compile(source, "<bootstrap>", "exec")
    assert repr(windows_path) in source


# ---------------------------------------------------------------------------
# Platform guard: the POSIX-only daemonisation must never be attempted on
# Windows, and the Windows fallback must be reachable.
# ---------------------------------------------------------------------------


def test_start_daemon_falls_back_to_spawn_when_fork_is_unavailable(isolated_run_dir, monkeypatch):
    """No os.fork (i.e. Windows) means the detached-spawn path, not a crash."""
    monkeypatch.setattr(
        auto_inspect.config_utils,
        "get_auto_inspect_config",
        lambda: {"enabled": True, "interval": 900},
    )
    monkeypatch.delattr(auto_inspect.os, "fork", raising=False)

    spawned = []
    monkeypatch.setattr(auto_inspect, "_spawn_detached", lambda interval: spawned.append(interval))

    auto_inspect.start_daemon()

    assert spawned == [900]


def test_start_daemon_coerces_a_string_interval(isolated_run_dir, monkeypatch):
    """A string interval from a hand-edited config.toml becomes an int."""
    monkeypatch.setattr(
        auto_inspect.config_utils,
        "get_auto_inspect_config",
        lambda: {"enabled": True, "interval": "900"},
    )
    monkeypatch.delattr(auto_inspect.os, "fork", raising=False)

    spawned = []
    monkeypatch.setattr(auto_inspect, "_spawn_detached", lambda interval: spawned.append(interval))

    auto_inspect.start_daemon()

    assert spawned == [900]
    assert isinstance(spawned[0], int)


def test_start_daemon_refuses_when_not_enabled(isolated_run_dir, monkeypatch):
    """A disabled daemon does not start."""
    monkeypatch.setattr(
        auto_inspect.config_utils,
        "get_auto_inspect_config",
        lambda: {"enabled": False, "interval": 3600},
    )

    with pytest.raises(RuntimeError, match="not enabled"):
        auto_inspect.start_daemon()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork path")
def test_start_daemon_refuses_when_already_running(isolated_run_dir, live_process):
    """Starting a second daemon over a live one is refused."""
    auto_inspect.PID_FILE.write_text(str(live_process.pid))

    with pytest.raises(RuntimeError, match="already running"):
        auto_inspect.start_daemon()


def test_spawn_detached_sends_child_stderr_to_the_log(isolated_run_dir, monkeypatch):
    """A child that dies in its bootstrap leaves its traceback in the log.

    This is the gap that made every prior Windows failure invisible: the child's
    stderr went to DEVNULL and Popen never waited, so the traceback vanished
    while the CLI printed "Auto-inspect background process started."
    """
    monkeypatch.setattr(
        auto_inspect,
        "_bootstrap_source",
        lambda module_path, interval: "raise RuntimeError('bootstrap blew up')",
    )

    auto_inspect._spawn_detached(60)

    # Popen does not wait, so give the child a moment to die and flush.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "bootstrap blew up" in auto_inspect.LOG_FILE.read_text():
            break
        time.sleep(0.05)

    logged = auto_inspect.LOG_FILE.read_text()
    assert "RuntimeError: bootstrap blew up" in logged
    assert "Traceback (most recent call last)" in logged


def test_get_log_tail_returns_the_last_lines(isolated_run_dir):
    """The log tail returns the most recent lines."""
    auto_inspect.LOG_FILE.write_text("".join(f"line {i}\n" for i in range(50)))

    tail = auto_inspect.get_log_tail(3)

    assert tail == "line 47\nline 48\nline 49\n"


def test_get_log_tail_without_a_log_file(isolated_run_dir):
    """No log file is reported, not raised."""
    assert auto_inspect.get_log_tail() == "No log file found"


def test_pid_dir_honors_cwcli_home():
    """The daemon's run dir lives under cwcli_home(), so it is valid on Windows.

    The 2026-07-05 scan reported a hardcoded /tmp log path; that string is in
    startup.py's macOS plist only. The daemon's own log has always resolved
    through cwcli_home(), and this pins it there.
    """
    assert auto_inspect.PID_DIR.name == "run"
    assert auto_inspect.LOG_FILE.parent == auto_inspect.PID_DIR
    assert not str(auto_inspect.LOG_FILE).startswith("/tmp/cwcli")
    assert os.path.isabs(str(auto_inspect.LOG_FILE))
