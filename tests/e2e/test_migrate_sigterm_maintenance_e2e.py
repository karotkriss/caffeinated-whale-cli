"""Real-Docker E2E for BUG-11: a SIGTERM mid-``migrate`` must NOT leave the site
stuck in maintenance mode - deterministic on Frappe v14, v15 and v16.

``cwcli migrate`` puts a site into maintenance mode, runs ``bench migrate``, and
takes it back out on interrupt. The signal->unwind mechanism (``utils/signals.py``,
wired in ``main.cli``) converts SIGTERM/SIGHUP into the same ``KeyboardInterrupt``
unwind Ctrl+C already gets, so the cleanup runs. But the unwind alone is not
enough: cwcli cannot kill the in-container ``bench migrate`` it launched (Docker
has no kill-exec API, and closing the exec socket does not stop the process), so
the migrate keeps running orphaned. On v15+ that orphan self-manages maintenance
mode and can re-assert it AFTER cwcli clears it, then die without clearing it,
leaving the site stuck at HTTP 503.

The PRODUCT fix (``core.bench_ops`` interrupt cleanup) makes the outcome
deterministic on every major WITHOUT a fragile kill-timing gate: on interrupt
cwcli ENDS the process it started (found by a unique per-invocation marker in its
environ, SIGTERM then SIGKILL, waited for), THEN clears maintenance LAST with a
settle/re-check. So regardless of whether the orphan had self-set maintenance, the
last write is cwcli's clear and the killed orphan cannot re-assert it.

This test proves that end to end. It gates the kill on the migrate PROCESS being
observably running (portable across majors via ``harness.migrate_process_running``,
unlike the migrate lock, which v14/v15 do not hold observably), so there is a
genuine orphan for cwcli to end. After SIGTERM it asserts the plain, version-
independent guarantee: the process cwcli started is GONE, and maintenance mode is 0
and STAYS 0 over a settle window.

**Why this runs against its OWN throwaway site, not the shared default site.**
Maintenance mode is PER SITE (``sites/<site>/site_config.json``), so migrating a
dedicated throwaway site keeps the orphan's flag entirely off the default site every
other test depends on: the blast radius cannot reach a sibling no matter what this
test does, and a fresh site migrates quickly. The site is always dropped in a
bounded, never-raising finalizer.

The unit-level signal->unwind mechanism is pinned by ``tests/test_signals.py`` and
the interrupt cleanup by ``tests/test_core_bench_ops.py`` /
``tests/test_core_update.py``; a deliberately-slow orphan that re-asserts
maintenance is exercised by ``test_migrate_sigterm_orphan_kill_e2e``. This is the
ordinary-case end-to-end proof against a genuine bench on every major.
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
import warnings

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = pytest.mark.e2e

# A dedicated throwaway site for THIS test's migrate, so the orphaned migrate's
# maintenance-mode flag can never reach the shared default site (see the module
# docstring). Named with the shared `cwe2e-` site convention; the session sweep
# removes the whole instance at teardown regardless.
MIGRATE_SITE = "cwe2e-sigterm-migrate.localhost"

# How long the test verifies maintenance STAYS cleared after cwcli exits. A surviving
# orphan (without the product fix) would flip it back; with the fix the orphan is dead.
SETTLE_SECONDS = 15


def _maintenance_mode(inst, site: str) -> int | None:
    """Read ``maintenance_mode`` LIVE from ``site``'s site_config.json in-container.

    Returns 1/0, or None when the file is unreadable/unparseable (fail-honest, so a
    transient read miss never masquerades as "not in maintenance").
    """
    code, out = harness.exec_in_frappe(inst.name, f"cat {inst.bench}/sites/{site}/site_config.json")
    if code != 0:
        return None
    try:
        return 1 if json.loads(out).get("maintenance_mode") else 0
    except (json.JSONDecodeError, TypeError):
        return None


def _create_site(inst, site: str) -> None:
    """A real ``bench new-site`` on the shared bench, both secrets supplied so it
    never prompts (mirrors the throwaway-site pattern in test_rm_site_e2e)."""
    result = harness.run_cwcli(
        "run",
        inst.name,
        "-i",
        "new-site",
        site,
        "--db-root-password",
        "123",
        "--admin-password",
        SESSION_ADMIN_PW,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{site}")
    assert code == 0, f"new-site did not create {site}"


def _drop_site(inst, site: str) -> None:
    """Best-effort, bounded, NEVER-RAISING teardown of the throwaway site.

    Runs even when the test's own assertions timed out, so the dedicated site (and any
    still-running orphan migrate against it) is torn down. It NEVER raises: a
    failed/slow drop must not mask the real test failure, and the site is isolated from
    the default site anyway. ``bench drop-site``'s ``DROP DATABASE`` can block on a
    metadata lock held by a still-draining orphan migrate, so the drop is bounded.
    """
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{site}")
    if code != 0:
        return
    try:
        harness.run_cwcli("rm-site", inst.name, site, "--yes", timeout=300)
    except subprocess.TimeoutExpired:
        warnings.warn(f"teardown of {site} timed out; leaving it for session sweep", stacklevel=2)


def test_sigterm_mid_migrate_takes_the_site_back_out_of_maintenance(running_instance):
    inst = running_instance
    try:
        _create_site(inst, MIGRATE_SITE)
        # A fresh site is not in maintenance.
        assert _maintenance_mode(inst, MIGRATE_SITE) in (0, None)

        proc = subprocess.Popen(
            [harness.CWCLI, "axi", "migrate", inst.name, "--site", MIGRATE_SITE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Gate the kill on the migrate PROCESS being observably running, not on
            # maintenance==1 and not on the migrate lock. cwcli sets maintenance ON
            # itself BEFORE it launches bench migrate, so a maintenance==1 gate can
            # fire on cwcli's own write before the orphan boots (raced on v16); the
            # lock gate never fires on v14/v15 (they do not hold it observably). A
            # running bench-migrate process is the one signal present on every major,
            # and it guarantees there is a genuine orphan for cwcli's cleanup to end.
            harness.wait_until(
                lambda: harness.migrate_process_running(inst.name, MIGRATE_SITE),
                timeout=240,
                interval=0.5,
                desc="the in-container migrate is running",
            )
            # cwcli has set maintenance ON by the time its migrate is running.
            assert _maintenance_mode(inst, MIGRATE_SITE) == 1
            # The kill under test: a plain SIGTERM, exactly what `kill`/a service stop
            # sends. Before the fix this killed cwcli with maintenance left ON.
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # THE GUARANTEE, version-independent, no kill-timing gate:
        # (1) the migrate process cwcli started is GONE - cwcli ended it on the unwind
        #     (SIGTERM->SIGKILL), rather than leaving it orphaned;
        # (2) maintenance mode is 0 and STAYS 0 over a settle window - cwcli cleared it
        #     LAST, and the ended orphan cannot re-assert it (before the fix a v15+
        #     orphan re-set it and it stuck; on v14 nothing but cwcli ever clears it).
        harness.wait_until(
            lambda: not harness.migrate_process_running(inst.name, MIGRATE_SITE),
            timeout=120,
            interval=2,
            desc="the in-container migrate cwcli started is gone",
        )
        harness.wait_until(
            lambda: _maintenance_mode(inst, MIGRATE_SITE) == 0,
            timeout=90,
            interval=1,
            desc="site taken back out of maintenance after SIGTERM",
        )
        settle_deadline = time.time() + SETTLE_SECONDS
        while time.time() < settle_deadline:
            assert (
                _maintenance_mode(inst, MIGRATE_SITE) == 0
            ), "site re-entered maintenance after cwcli exited (orphaned migrate not ended)"
            time.sleep(1)
    finally:
        _drop_site(inst, MIGRATE_SITE)
