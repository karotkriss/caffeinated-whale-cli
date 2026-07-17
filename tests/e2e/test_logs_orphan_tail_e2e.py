"""Real-Docker E2E for the ``cwcli logs --follow`` orphan-``tail -F`` leak.

Guards the regression where following logs non-interactively (an agent/pipe, no
TTY) and Ctrl+C-ing out left the exec'd ``tail -F`` running INSIDE the container.
The ``docker exec`` client dies on Ctrl+C but its exec'd process keeps running,
and Docker exposes no kill-exec API, so every Ctrl+C accumulated another orphan.

The frontend now wraps the non-TTY follow tail so it records its own PID, and
reaps that PID on exit (``commands/logs.py``). The interactive ``-it`` path was
already clean - docker forwards ^C into the container, tail exits 130 - and this
test proves BOTH: no orphan ``tail -F`` survives the follower's exit in either
mode. Runs against the shared session instance; a pure read, nothing is mutated.
"""

from __future__ import annotations

import signal
import subprocess
import time

import pytest

from . import harness

pytestmark = pytest.mark.e2e


def _orphan_tail_count(project: str) -> int:
    """How many ``tail -F`` processes are alive inside the frappe container."""
    code, out = harness.exec_in_frappe(
        project, "ps -e -o pid,args 2>/dev/null | grep '[t]ail -F' | wc -l"
    )
    assert code == 0, out
    return int(out.strip() or "0")


def test_non_tty_follow_leaves_no_orphan_tail(running_instance):
    inst = running_instance
    baseline = _orphan_tail_count(inst.name)

    # Non-TTY follow, exactly as an agent/pipe drives it: stdin closed, a real
    # non-TTY, so `docker exec` carries no `-it` and Ctrl+C reaches cwcli itself.
    proc = subprocess.Popen(
        [harness.CWCLI, "logs", inst.name, "--follow"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait until the exec'd tail is genuinely running in the container.
        harness.wait_until(
            lambda: _orphan_tail_count(inst.name) > baseline,
            timeout=60,
            desc="tail -F running inside the container",
        )
        # The user's Ctrl+C.
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # The reap runs in cwcli's `finally`; give docker a beat to settle, then assert
    # no orphan survived the follower's exit.
    harness.wait_until(
        lambda: _orphan_tail_count(inst.name) <= baseline,
        timeout=30,
        desc="no orphan tail -F after the non-TTY follower exits",
    )


def test_tty_follow_leaves_no_orphan_tail(running_instance):
    inst = running_instance
    baseline = _orphan_tail_count(inst.name)

    # Interactive TTY follow on a real pty: docker allocates a TTY and forwards ^C
    # into the container, so tail exits there (130). This path is unchanged by the
    # fix; the test pins that it stays orphan-free.
    child = harness.spawn_cwcli(["logs", inst.name, "--follow"])
    try:
        harness.wait_until(
            lambda: _orphan_tail_count(inst.name) > baseline,
            timeout=60,
            desc="tail -F running inside the container (TTY path)",
        )
        child.sendcontrol("c")  # Ctrl+C into the pty
        time.sleep(2)
    finally:
        if child.isalive():
            child.close(force=True)

    harness.wait_until(
        lambda: _orphan_tail_count(inst.name) <= baseline,
        timeout=30,
        desc="no orphan tail -F after the TTY follower exits",
    )
