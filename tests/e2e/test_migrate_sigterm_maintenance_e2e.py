"""Real-Docker E2E for BUG-11: an interrupted ``migrate`` must NOT leave the site
stuck in maintenance mode, and cwcli must END the in-container migrate it started -
deterministic on Frappe v14, v15 and v16.

``cwcli migrate`` puts a site into maintenance mode, runs ``bench migrate``, and on
interrupt (Ctrl+C / SIGTERM / SIGHUP, unified by ``utils/signals.py``) unwinds its
cleanup. The unwind alone is not enough: Docker has no kill-exec API and closing the
exec socket does not stop the ``bench migrate`` cwcli launched, so it keeps running
orphaned. On v15+ that orphan self-manages maintenance and can re-assert it AFTER
cwcli clears it, leaving the site at HTTP 503. The product fix (``core.bench_ops``
interrupt cleanup) ENDS the exact process cwcli started - found by the unique
per-invocation ``CWCLI_MIGRATE_TOKEN`` in its environ, SIGTERM then SIGKILL, waited
for - and only then clears maintenance LAST with a settle/re-check.

**How this is made DETERMINISTIC on every major without a timing gate.** Earlier
approaches raced: gating the kill on ``maintenance==1`` fired before the orphan
booted (v16), gating on the migrate lock never fired on v14/v15 (they do not hold it
observably), and slowing the migrate with a custom app risked poisoning the shared
bench. Instead this test FREEZES the migrate: it waits until the in-container process
carrying this invocation's ``CWCLI_MIGRATE_TOKEN`` exists (the same environ marker the
product uses), ``SIGSTOP``s that exact process tree, and only THEN sends SIGTERM to
cwcli. The frozen orphan is provably alive but cannot finish, clear, or re-assert
maintenance, so timing is exact on v14/v15/v16 alike - no sleep-as-synchronisation,
no maintenance/lock precondition.

**The discriminating assertion** is that the token process is GONE after cwcli exits:
cwcli's cleanup SIGTERM-then-SIGKILLs the frozen orphan (SIGKILL reaps a stopped
process). WITHOUT the product fix cwcli only closes the exec socket, so the stopped
orphan survives and this assertion fails. Maintenance is then 0 and stays 0.

Everything is confined to a dedicated throwaway site and a never-raising finalizer
that unfreezes/kills any leftover token process, drops the site, and verifies the
shared bench still lists apps - nothing this test does can break the shared instance.

The unit-level signal->unwind mechanism is pinned by ``tests/test_signals.py`` and
the interrupt cleanup by ``tests/test_core_bench_ops.py`` / ``tests/test_core_update.py``;
this is the end-to-end proof against a genuine bench on every major.
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

# A dedicated throwaway site for THIS test's migrate, so nothing it does touches the
# shared default site. The session sweep removes the whole instance regardless.
MIGRATE_SITE = "cwe2e-sigterm-migrate.localhost"

# The env var cwcli stamps on the migrate exec it launches (core.bench_ops.migrate_env).
# Only cwcli's migrate carries it, so it identifies exactly the process(es) to freeze.
MIGRATE_TOKEN_ENV = "CWCLI_MIGRATE_TOKEN"

# How long the test verifies maintenance STAYS cleared after cwcli exits.
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


def _token_pids(inst) -> list[str]:
    """PIDs of in-container processes whose environ carries ``CWCLI_MIGRATE_TOKEN=`` -
    the migrate process tree cwcli launched (and any children, which inherit the env).

    The test does not need the token value: only one cwcli migrate runs against the
    dedicated site. Reads ``/proc/<pid>/environ`` (readable because the test's exec and
    the migrate run as the same container user, exactly as the product's own scan
    relies on). Portable across Frappe majors, unlike the v16-only migrate lock.
    """
    script = (
        "for d in /proc/[0-9]*; do "
        f'grep -aqzF -- {MIGRATE_TOKEN_ENV}= "$d/environ" 2>/dev/null && echo "${{d##*/}}"; '
        "done"
    )
    _code, out = harness.exec_in_frappe(inst.name, script)
    return [p for p in out.split() if p.isdigit()]


def _signal_token_pids(inst, sig: str) -> None:
    """Send ``sig`` to every in-container process carrying the migrate token."""
    pids = _token_pids(inst)
    if pids:
        harness.exec_in_frappe(inst.name, f"kill -{sig} {' '.join(pids)} 2>/dev/null || true")


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
    """Best-effort, bounded, NEVER-RAISING teardown of the throwaway site."""
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{site}")
    if code != 0:
        return
    try:
        harness.run_cwcli("rm-site", inst.name, site, "--yes", timeout=300)
    except subprocess.TimeoutExpired:
        warnings.warn(f"teardown of {site} timed out; leaving it for session sweep", stacklevel=2)


def _bench_lists_apps(inst, site: str) -> bool:
    """True if the bench can list apps for ``site`` - a cheap proof the shared bench is
    not poisoned (the failure signature when a bad app entry breaks frappe imports)."""
    code, _ = harness.exec_in_frappe(inst.name, f"cd {inst.bench} && bench --site {site} list-apps")
    return code == 0


def test_sigterm_mid_migrate_ends_the_orphan_and_keeps_the_site_out_of_maintenance(
    running_instance,
):
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
            # Wait until cwcli's in-container migrate process exists (its environ carries
            # the token), then FREEZE it so the timing is exact: it is now alive but
            # cannot finish, clear maintenance, or re-assert it. This replaces every
            # fragile timing gate (maintenance==1 raced v16; the lock never fired on
            # v14/v15) and needs no slow migrate (which risked poisoning the bench).
            harness.wait_until(
                lambda: bool(_token_pids(inst)),
                timeout=240,
                interval=0.5,
                desc="the in-container migrate cwcli started exists",
            )
            _signal_token_pids(inst, "STOP")
            # Interrupt cwcli. Its cleanup must END the frozen orphan (SIGTERM then
            # SIGKILL - SIGKILL reaps a stopped process), then clear maintenance LAST.
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # THE DISCRIMINATING GUARANTEE: the migrate process cwcli started is GONE.
        # Without the product fix cwcli only closes the exec socket and the STOPPED
        # orphan survives, so this fails; with the fix cwcli SIGKILLed it.
        harness.wait_until(
            lambda: not _token_pids(inst),
            timeout=120,
            interval=2,
            desc="the in-container migrate cwcli started is gone",
        )
        # And maintenance is 0 and STAYS 0 over a settle window (the ended orphan can
        # never re-assert it; on v14 nothing but cwcli's cleanup ever clears it).
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
        # Never-raising: unfreeze then kill any leftover token process (so a stopped
        # orphan can never linger), drop the throwaway site, and verify the shared bench
        # is healthy - nothing this test did may leave the shared instance broken.
        _signal_token_pids(inst, "CONT")
        _signal_token_pids(inst, "KILL")
        _drop_site(inst, MIGRATE_SITE)
        if not _bench_lists_apps(inst, harness.DEFAULT_SITE):
            warnings.warn("shared bench cannot list apps after this test's teardown", stacklevel=2)
