"""``utils.daemon_identity`` unit tests, in its promoted home.

These pin the pid-file IDENTITY primitives that both detached daemons (auto-inspect
and the credential bridge) now share from ONE copy. Deliberately cross-platform
and mock-free: the bugs they guard (a Windows liveness probe that says a dead pid
is alive; a recycled pid signalled as if it were the daemon) are real-kernel bugs,
so they run on the Windows CI leg as well as Linux, parameterised by an explicit
``pid_file`` path so the module carries no daemon's globals.
"""

import os
import subprocess
import sys

import pytest

from caffeinated_whale_cli.utils import daemon_identity


@pytest.fixture
def pid_file(tmp_path):
    return tmp_path / "some-daemon.pid"


@pytest.fixture
def live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


# ----------------------------------------------------------------- liveness probe


def test_pid_alive_true_for_a_live_process(live_process):
    assert daemon_identity.pid_alive(live_process.pid) is True


def test_pid_alive_false_for_a_dead_process():
    assert daemon_identity.pid_alive(_dead_pid()) is False


def test_pid_alive_false_for_a_pid_that_never_existed():
    assert daemon_identity.pid_alive(999999) is False


def test_pid_alive_leaves_the_process_running(live_process):
    assert daemon_identity.pid_alive(live_process.pid) is True
    with pytest.raises(subprocess.TimeoutExpired):
        live_process.wait(timeout=1)


# --------------------------------------------------------------------- start time


def test_process_start_time_is_real_and_stable():
    token = daemon_identity.process_start_time(os.getpid())
    assert token is not None
    assert daemon_identity.process_start_time(os.getpid()) == token


# ------------------------------------------------------------------ pid-file I/O


def test_write_pid_file_records_the_identity(pid_file):
    daemon_identity.write_pid_file(pid_file)
    lines = pid_file.read_text().splitlines()
    assert lines[0] == str(os.getpid())
    assert lines[1] == daemon_identity.process_start_time(os.getpid())


def test_write_pid_file_creates_the_parent_dir(tmp_path):
    nested = tmp_path / "run" / "sub" / "d.pid"
    daemon_identity.write_pid_file(nested)
    assert nested.exists()


def test_read_daemon_record_parses_pid_and_start(pid_file):
    pid_file.write_text("4242\nstart-tok")
    assert daemon_identity.read_daemon_record(pid_file) == (4242, "start-tok")


def test_read_daemon_record_pid_only_gives_none_start(pid_file):
    pid_file.write_text("4242")
    assert daemon_identity.read_daemon_record(pid_file) == (4242, None)


def test_read_daemon_record_garbage_is_none(pid_file):
    pid_file.write_text("not-a-pid")
    assert daemon_identity.read_daemon_record(pid_file) is None


def test_read_daemon_record_missing_is_none(pid_file):
    assert daemon_identity.read_daemon_record(pid_file) is None


def test_get_pid(pid_file):
    pid_file.write_text("77\ntok")
    assert daemon_identity.get_pid(pid_file) == 77
    assert daemon_identity.get_pid(pid_file.with_name("nope.pid")) is None


# ------------------------------------------------------------------- is_running


def test_is_running_true_for_a_live_daemon_and_leaves_it_running(pid_file, live_process):
    pid_file.write_text(str(live_process.pid))
    assert daemon_identity.is_running(pid_file) is True
    with pytest.raises(subprocess.TimeoutExpired):
        live_process.wait(timeout=1)


def test_is_running_false_and_clears_a_stale_pid_file(pid_file):
    pid_file.write_text(str(_dead_pid()))
    assert daemon_identity.is_running(pid_file) is False
    assert not pid_file.exists()


def test_is_running_false_for_a_recycled_pid_and_clears_it(pid_file, live_process, monkeypatch):
    pid_file.write_text(f"{live_process.pid}\nrecorded-identity")
    monkeypatch.setattr(daemon_identity, "process_start_time", lambda pid: "different-now")
    assert daemon_identity.is_running(pid_file) is False
    assert not pid_file.exists()


def test_is_running_false_when_no_pid_file(pid_file):
    assert daemon_identity.is_running(pid_file) is False


def test_two_daemons_use_independent_pid_files(tmp_path):
    a = tmp_path / "a.pid"
    b = tmp_path / "b.pid"
    a.write_text(str(_dead_pid()))
    assert daemon_identity.is_running(a) is False  # clears a
    assert daemon_identity.is_running(b) is False  # b never existed; a's clearing didn't touch it
    assert not a.exists()
