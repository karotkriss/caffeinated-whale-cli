"""Real-Docker E2E for BUG-11: a SIGTERM mid-``migrate`` must NOT leave the site
stuck in maintenance mode.

``cwcli migrate`` puts a site into maintenance mode, runs ``bench migrate``, and
takes it back out in a ``finally``. That ``finally`` runs on Ctrl+C (SIGINT ->
KeyboardInterrupt unwind) but used to be SKIPPED on SIGTERM (a plain ``kill`` / a
service stop) and SIGHUP (a closed terminal), whose default disposition is
immediate death with no stack unwind - so the site was left ``maintenance_mode: 1``
(HTTP 503 to every user, indefinitely) with nothing telling the operator. The fix
installs a process-level handler (``utils/signals.py``, wired in ``main.cli``) that
converts SIGTERM/SIGHUP into the same unwind, so the existing cleanup runs.

This reproduces the report's deterministic repro: launch the REAL cwcli binary
directly (so the signal reaches the real process, not a ``uv run`` wrapper), catch
the exact instant maintenance flips ON, ``kill -TERM`` it, then assert the site is
taken back OUT of maintenance - which only happens if the ``finally`` ran.

The unit-level signal->unwind mechanism is pinned by ``tests/test_signals.py``; this
is the end-to-end proof against a genuine bench.
"""

from __future__ import annotations

import json
import signal
import subprocess

import pytest

from . import harness

pytestmark = pytest.mark.e2e


def _maintenance_mode(inst) -> int | None:
    """Read ``maintenance_mode`` LIVE from the site's site_config.json in-container.

    Returns 1/0, or None when the file is unreadable/unparseable (fail-honest, so a
    transient read miss never masquerades as "not in maintenance").
    """
    code, out = harness.exec_in_frappe(
        inst.name, f"cat {inst.bench}/sites/{inst.site}/site_config.json"
    )
    if code != 0:
        return None
    try:
        return 1 if json.loads(out).get("maintenance_mode") else 0
    except (json.JSONDecodeError, TypeError):
        return None


def _migrate_lock_present(inst) -> bool:
    code, _ = harness.exec_in_frappe(
        inst.name,
        f"test -f {inst.bench}/sites/{inst.site}/locks/bench_migrate.lock && echo yes",
    )
    return code == 0


def test_sigterm_mid_migrate_takes_the_site_back_out_of_maintenance(running_instance):
    inst = running_instance
    # Precondition: not already stuck in maintenance from an earlier step.
    assert _maintenance_mode(inst) in (0, None)

    proc = subprocess.Popen(
        [harness.CWCLI, "axi", "migrate", inst.name, "--site", inst.site],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Catch the exact maintenance-ON window (poll fast; bench migrate on a v16
        # site holds the window open long enough to catch reliably).
        harness.wait_until(
            lambda: _maintenance_mode(inst) == 1,
            timeout=240,
            interval=0.25,
            desc="site enters maintenance mode",
        )
        # The kill under test: a plain SIGTERM, exactly what `kill`/a service stop
        # sends. Before the fix this killed cwcli with maintenance left ON.
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=90)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # THE ASSERTION: the SIGTERM unwound the `finally`, so the site is taken back OUT
    # of maintenance. (Before the fix this stayed 1 forever.) The orphaned in-container
    # `bench migrate` keeps running and does not manage maintenance, so once cwcli's
    # cleanup clears it, it stays clear.
    harness.wait_until(
        lambda: _maintenance_mode(inst) == 0,
        timeout=180,
        interval=1,
        desc="site taken back out of maintenance after SIGTERM",
    )

    # Good-citizen cleanup for the shared session instance: let the orphaned migrate
    # finish and confirm the site serves again, so a sibling test finds it quiescent.
    harness.wait_until(
        lambda: not _migrate_lock_present(inst),
        timeout=600,
        interval=3,
        desc="orphaned migrate releases its lock",
    )
    harness.wait_for_site_ready(inst.name, inst.site)
    assert _maintenance_mode(inst) == 0
