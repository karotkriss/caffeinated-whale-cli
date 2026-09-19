"""``utils.signals`` - SIGTERM/SIGHUP unwind exactly like Ctrl+C (BUG-11).

The headline: a killed terminal (SIGHUP) or ``kill`` (SIGTERM) mid-``migrate`` /
``apps update`` used to leave a site stuck in maintenance mode, because the cleanup
lives in a ``finally`` that only Python's SIGINT->KeyboardInterrupt unwind runs. The
fix converts SIGTERM and SIGHUP into the same unwind. This proves it two ways: the
handler itself (reset-then-raise), and a REAL signal sent to a REAL subprocess whose
``finally`` must run.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest

from caffeinated_whale_cli.utils import signals

# The subprocess mirrors the real code: a `finally` cleanup, and a KeyboardInterrupt
# is what must trigger it. It writes CLEANED to the marker from the finally, so the
# marker's presence PROVES the finally ran (the maintenance-off / bridge-teardown
# would run at exactly this point in the real verbs).
_CHILD = """
import signal, sys, time
from caffeinated_whale_cli.utils.signals import install_unwind_handlers

marker, ready = sys.argv[1], sys.argv[2]
install_unwind_handlers()
try:
    try:
        open(ready, "w").close()  # tell the parent we are armed and blocking
        while True:
            time.sleep(0.02)
    finally:
        with open(marker, "w") as f:
            f.write("CLEANED")
except KeyboardInterrupt:
    pass
"""


def _run_child_and_signal(tmp_path, signum):
    marker = tmp_path / "marker"
    ready = tmp_path / "ready"
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, str(marker), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("child never became ready")
            time.sleep(0.02)
        proc.send_signal(signum)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    return marker


def test_sigterm_runs_the_cleanup_like_ctrl_c(tmp_path):
    marker = _run_child_and_signal(tmp_path, signal.SIGTERM)
    assert marker.exists() and marker.read_text() == "CLEANED"


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is POSIX-only")
def test_sighup_runs_the_cleanup_like_ctrl_c(tmp_path):
    marker = _run_child_and_signal(tmp_path, signal.SIGHUP)
    assert marker.exists() and marker.read_text() == "CLEANED"


def test_handler_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        signals._unwind(signal.SIGTERM, None)


def test_a_second_signal_during_cleanup_is_not_swallowed():
    """The handler restores DEFAULT disposition for both signals before raising, so a
    second termination signal arriving during cleanup kills the process instead of
    re-entering the handler."""
    saved = {s: signal.getsignal(s) for s in signals._UNWIND_SIGNALS}
    try:
        signals.install_unwind_handlers()
        with pytest.raises(KeyboardInterrupt):
            signals._unwind(signal.SIGTERM, None)
        for s in signals._UNWIND_SIGNALS:
            assert signal.getsignal(s) is signal.SIG_DFL
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def test_install_sets_our_handler_for_each_termination_signal():
    saved = {s: signal.getsignal(s) for s in signals._UNWIND_SIGNALS}
    try:
        signals.install_unwind_handlers()
        for s in signals._UNWIND_SIGNALS:
            assert signal.getsignal(s) is signals._unwind
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def test_install_is_a_noop_off_the_main_thread():
    """``signal.signal`` is main-thread only; installing from a worker thread must not
    raise (an embedded/threaded caller of the CLI would otherwise crash at startup)."""
    import threading

    errors: list[BaseException] = []

    def _work():
        try:
            signals.install_unwind_handlers()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=_work)
    t.start()
    t.join()
    assert errors == []


def test_unwind_signals_covers_sigterm():
    assert signal.SIGTERM in signals._UNWIND_SIGNALS
    # SIGHUP is included wherever the platform has it.
    if hasattr(signal, "SIGHUP"):
        assert signal.SIGHUP in signals._UNWIND_SIGNALS
