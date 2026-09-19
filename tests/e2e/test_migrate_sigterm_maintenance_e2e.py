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

**Why this runs against its OWN throwaway site, not the shared default site.**
cwcli cannot kill the in-container ``bench migrate`` it launched (Docker has no
kill-exec API), so it keeps running orphaned after cwcli exits. On a Frappe major
whose ``bench migrate`` manages maintenance mode ITSELF (v15+), that orphaned run
re-asserts maintenance for the rest of its run, and a full migrate of a
much-mutated shared site can take LONGER than any fixed deadline on a slow CI leg.
When this test used the shared session default site, an over-long orphan drain both
timed the test out AND left that shared site in maintenance, so sibling tests that
probe it by ``Host`` header got 503. Maintenance mode is PER SITE
(``sites/<site>/site_config.json``), so migrating a dedicated throwaway site keeps
the orphan's maintenance flag entirely off the default site every other test
depends on: the blast radius cannot reach a sibling no matter how long the orphan
runs or whether this test's own assertions pass. A freshly created site also
migrates quickly (its patches were applied at ``new-site``), so the drain is bounded.

The genuine assertion is unchanged: after SIGTERM, cwcli's own cleanup ran, so once
the orphan drains the site is out of maintenance. That stays a real proof, because
on a Frappe that does NOT self-manage maintenance (v14) nothing but cwcli's cleanup
would ever clear it, and before the fix it stayed 1 forever.

The unit-level signal->unwind mechanism is pinned by ``tests/test_signals.py``; this
is the end-to-end proof against a genuine bench.
"""

from __future__ import annotations

import json
import signal
import subprocess
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


def _maintenance_mode(inst, site: str) -> int | None:
    """Read ``maintenance_mode`` LIVE from ``site``'s site_config.json in-container.

    Returns 1/0, or None when the file is unreadable/unparseable (fail-honest, so a
    transient read miss never masquerades as "not in maintenance").
    """
    code, out = harness.exec_in_frappe(
        inst.name, f"cat {inst.bench}/sites/{site}/site_config.json"
    )
    if code != 0:
        return None
    try:
        return 1 if json.loads(out).get("maintenance_mode") else 0
    except (json.JSONDecodeError, TypeError):
        return None


def _migrate_lock_held(inst, site: str) -> bool:
    """True only while frappe's migrate lock for ``site`` is GENUINELY held by a
    live process.

    Mirrors ``core.bench_ops._migrate_lock_held``: ``bench migrate`` wraps its run
    in an ``fcntl`` advisory lock but leaves the lock FILE on disk after releasing
    it, so ``test -f`` stays True forever and can never signal that the orphaned
    migrate finished. ``flock -n`` acquires the SAME kernel primitive and succeeds
    (exit 0) the instant the lock is released, whether or not the file remains.
    """
    lock = f"{inst.bench}/sites/{site}/locks/bench_migrate.lock"
    code, _ = harness.exec_in_frappe(
        inst.name,
        f"test -f {lock} || exit 0; flock -n -E 200 {lock} -c true",
    )
    return code == 200


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

    Runs even when the test's own assertions or the orphan drain timed out, so the
    dedicated site (and any still-running orphan migrate against it) is torn down.
    It NEVER raises: a failed/slow drop must not mask the real test failure, and the
    site is isolated from the default site anyway, so leaving it for the session
    sweep is harmless. ``bench drop-site``'s ``DROP DATABASE`` can block on a
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
            # Catch the exact maintenance-ON window (poll fast; cwcli sets maintenance
            # ON before it runs bench migrate, and bench migrate on a v16 site holds
            # the window open long enough to catch reliably).
            harness.wait_until(
                lambda: _maintenance_mode(inst, MIGRATE_SITE) == 1,
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

        # THE ASSERTION: the SIGTERM unwound the `finally`, so the site is taken back
        # OUT of maintenance. (Before the fix this stayed 1 forever.)
        #
        # cwcli cannot kill the orphaned in-container `bench migrate`, which on v15+
        # re-asserts maintenance for the rest of its run. Let it drain FIRST (its fcntl
        # lock releases the instant it ends, stale lock file or not) with a generous
        # deadline, THEN require the site back out of maintenance. It stays a genuine
        # proof: on a Frappe that does NOT self-manage maintenance (v14) nothing but
        # cwcli's cleanup would ever clear it, and before the fix it stayed 1 forever.
        # The whole exchange is confined to MIGRATE_SITE, so the default site every
        # sibling probes is never in maintenance regardless of how long the orphan runs.
        harness.wait_until(
            lambda: not _migrate_lock_held(inst, MIGRATE_SITE),
            timeout=600,
            interval=3,
            desc="orphaned migrate releases its lock",
        )
        harness.wait_until(
            lambda: _maintenance_mode(inst, MIGRATE_SITE) == 0,
            timeout=180,
            interval=1,
            desc="site taken back out of maintenance after SIGTERM",
        )
        harness.wait_for_site_ready(inst.name, MIGRATE_SITE, inst.bench)
        assert _maintenance_mode(inst, MIGRATE_SITE) == 0
    finally:
        _drop_site(inst, MIGRATE_SITE)
