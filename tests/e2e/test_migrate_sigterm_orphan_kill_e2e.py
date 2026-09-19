"""Real-Docker E2E for the v16 SIGTERM/maintenance RACE (BUG-11, second layer).

The sibling ``test_migrate_sigterm_maintenance_e2e`` proves the signal->unwind
mechanism: a SIGTERM makes cwcli's cleanup run at all. It does NOT reliably
exercise the race that the unwind alone cannot fix, because a fast throwaway
site's orphaned migrate dies before it can re-assert maintenance (see the
firstmate divergence analysis). This test makes that race DETERMINISTIC.

The race: cwcli cannot kill the in-container ``bench migrate`` by closing the
exec socket (Docker has no kill-exec API), so on SIGTERM the migrate keeps
running orphaned. On Frappe v15+ it self-manages maintenance mode and re-asserts
it AFTER cwcli clears it, then dies without clearing it - the site is stuck at
HTTP 503 with nothing to clear it. The fix (``core.bench_ops`` interrupt
cleanup) ENDS that process first - found by a unique per-invocation marker in
its environ - then clears maintenance LAST with a settle/re-check.

To reproduce the race deterministically and on EVERY version (v14 does not
self-manage maintenance, so the orphan cannot re-assert it on its own), this
installs a tiny E2E-only app on a DEDICATED throwaway site whose migrate runs a
patch that HOLDS maintenance mode ON in a loop for a window that outlasts cwcli's
cleanup. So:

- WITHOUT the product fix: cwcli clears maintenance once and exits, but the
  orphaned patch keeps writing it back ON, so the site stays stuck. (Proven on a
  branch carrying THIS test but not the product change - see the PR text.)
- WITH the fix: cwcli terminates the patch's process tree before clearing, so
  the loop stops and the site is taken - and STAYS - out of maintenance.

Everything is confined to the dedicated site + a dedicated app, both removed in
a finalizer, so the shared default site every sibling test depends on is never
touched no matter how long the orphan runs or whether this test's assertions
pass. The app's patch runs ONLY when migrating a site that has the app installed,
which is the dedicated site alone.
"""

from __future__ import annotations

import base64
import json
import signal
import subprocess
import time
import warnings

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = pytest.mark.e2e

# The dedicated throwaway site and the E2E-only app whose migrate is slow. Named
# with the shared cwe2e- convention; the session sweep removes the whole instance
# at teardown regardless, and the finalizer removes both explicitly.
SLOW_SITE = "cwe2e-slow-migrate.localhost"
SLOW_APP = "cwe2e_slowmigrate"

# How long the orphaned patch HOLDS maintenance mode ON if left alive. It only has
# to outlast the test's own assertion window below; WITH the fix the process is
# killed at once, so this never actually slows the passing path.
PATCH_HOLD_SECONDS = 150
# How long the test verifies maintenance STAYS cleared after cwcli exits. The
# looping orphan would flip it back within its 0.5s tick, so this catches a
# regression fast.
SETTLE_SECONDS = 15


def _maintenance(inst, site: str) -> int | None:
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


def _migrate_lock_held(inst, site: str) -> bool:
    """True only while frappe's migrate lock for ``site`` is GENUINELY held by a
    live process - the proxy for "the migrate process cwcli started is still alive".

    ``bench migrate`` wraps its run in an ``fcntl`` advisory lock but leaves the
    lock FILE on disk after releasing it, so ``test -f`` stays True forever;
    ``flock -n`` acquires the SAME kernel primitive and succeeds (exit 0) the
    instant the lock is released, whether or not the file remains. Mirrors
    ``core.bench_ops._migrate_lock_held``.
    """
    lock = f"{inst.bench}/sites/{site}/locks/bench_migrate.lock"
    code, _ = harness.exec_in_frappe(
        inst.name, f"test -f {lock} || exit 0; flock -n -E 200 {lock} -c true"
    )
    return code == 200


def _slow_patch_running(inst) -> bool:
    """True once the deliberately-slow migrate patch has started (its sentinel file).

    The test SIGTERMs cwcli only once this is True, so there is a genuine
    in-container migrate to orphan and it is provably inside the maintenance-holding
    loop when the signal lands.
    """
    code, _ = harness.exec_in_frappe(
        inst.name, f"test -f {inst.bench}/sites/{SLOW_SITE}/cwe2e-slow-patch-running"
    )
    return code == 0


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


# The migrate patch: hold maintenance mode ON in a loop for a window that outlasts
# cwcli's SIGTERM cleanup. It NEVER rewrites site_config.json with fewer keys (it
# read-modify-writes and skips a torn read), and writes atomically via os.replace,
# so it cannot corrupt the site's config even while cwcli concurrently clears
# maintenance. This is the deterministic stand-in for what a real v15+ orphaned
# migrate does to maintenance mode; on v14 it is what creates the race at all.
_PATCH_SOURCE = """\
import json
import os
import time

import frappe


def execute():
    site_config = frappe.get_site_path("site_config.json")
    # Announce that the slow patch is running so the test SIGTERMs cwcli only once
    # there is a genuine in-container migrate to orphan.
    with open(frappe.get_site_path("cwe2e-slow-patch-running"), "w") as fh:
        fh.write("1")
    deadline = time.monotonic() + __HOLD__
    while time.monotonic() < deadline:
        try:
            with open(site_config) as fh:
                cfg = json.load(fh)
        except Exception:
            # A torn read while cwcli concurrently writes: never write a partial
            # config (that would drop db_name/encryption_key); just try next tick.
            time.sleep(0.5)
            continue
        cfg["maintenance_mode"] = 1
        tmp = site_config + ".cwe2e.tmp"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh)
        os.replace(tmp, site_config)  # atomic, so cwcli never reads a torn file
        time.sleep(0.5)
"""


def _install_slow_migrate_app(inst) -> None:
    """Create a tiny E2E-only app, install it on the dedicated site, then add the
    slow patch AFTER install so it is genuinely unapplied and runs on the migrate.

    Manual, minimal, and version-agnostic (no reliance on ``bench new-app``'s
    interactive prompts). The patch is added post-install so migrate - not install
    - is what runs it.
    """
    patch = _PATCH_SOURCE.replace("__HOLD__", str(PATCH_HOLD_SECONDS))
    scaffold = f"""
set -e
cd {inst.bench}
APPDIR=apps/{SLOW_APP}/{SLOW_APP}
mkdir -p "$APPDIR/patches"
cat > apps/{SLOW_APP}/pyproject.toml <<'PYPROJECT'
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "{SLOW_APP}"
version = "0.0.1"

[tool.setuptools]
packages = ["{SLOW_APP}"]
PYPROJECT
cat > "$APPDIR/__init__.py" <<'INIT'
__version__ = "0.0.1"
INIT
cat > "$APPDIR/hooks.py" <<'HOOKS'
app_name = "{SLOW_APP}"
app_title = "CWE2E Slow Migrate"
app_publisher = "cwcli-e2e"
app_description = "E2E-only app whose migrate patch deliberately holds maintenance mode."
app_email = "e2e@example.com"
app_license = "mit"
HOOKS
: > "$APPDIR/modules.txt"
: > "$APPDIR/patches.txt"
touch "$APPDIR/patches/__init__.py"
env/bin/pip install -q -e apps/{SLOW_APP}
# Register the app bench-wide so install-app can find it.
grep -qxF '{SLOW_APP}' sites/apps.txt 2>/dev/null || echo '{SLOW_APP}' >> sites/apps.txt
bench --site {SLOW_SITE} install-app {SLOW_APP}
# Prove the app is genuinely installed on the site before we rely on its patch.
bench --site {SLOW_SITE} list-apps | grep -qw {SLOW_APP}
"""
    code, out = harness.exec_in_frappe(inst.name, scaffold)
    assert code == 0, f"failed to create/install the slow-migrate app:\n{out}"

    # Add the patch AFTER install so it is unapplied and runs on migrate. Written
    # via base64 so the Python source survives shell quoting untouched.
    encoded = base64.b64encode(patch.encode()).decode()
    add_patch = f"""
set -e
cd {inst.bench}
APPDIR=apps/{SLOW_APP}/{SLOW_APP}
echo '{encoded}' | base64 -d > "$APPDIR/patches/slow_maintenance.py"
printf '[pre_model_sync]\\n{SLOW_APP}.patches.slow_maintenance\\n' > "$APPDIR/patches.txt"
"""
    code, out = harness.exec_in_frappe(inst.name, add_patch)
    assert code == 0, f"failed to add the slow patch:\n{out}"


def _cleanup(inst) -> None:
    """Best-effort, bounded, NEVER-RAISING teardown of the dedicated site AND the
    E2E-only app, so nothing survives for a sibling test on the shared instance.

    NEVER raises: a slow/failed drop must not mask the real test failure, and the
    site+app are isolated from the default site anyway.
    """
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{SLOW_SITE}")
    if code == 0:
        try:
            harness.run_cwcli("rm-site", inst.name, SLOW_SITE, "--yes", timeout=300)
        except subprocess.TimeoutExpired:
            warnings.warn(
                f"teardown of {SLOW_SITE} timed out; leaving it for the session sweep",
                stacklevel=2,
            )
    # Remove the app bench-wide (pip + apps/ dir + apps.txt entry) so no sibling
    # test sees it. Best-effort; the session sweep removes the whole instance anyway.
    harness.exec_in_frappe(
        inst.name,
        f"""
cd {inst.bench} || exit 0
env/bin/pip uninstall -y {SLOW_APP} >/dev/null 2>&1 || true
rm -rf apps/{SLOW_APP}
if [ -f sites/apps.txt ]; then
  grep -vxF '{SLOW_APP}' sites/apps.txt > sites/apps.txt.cwe2e || true
  mv sites/apps.txt.cwe2e sites/apps.txt || true
fi
""",
    )


def test_sigterm_mid_slow_migrate_kills_the_orphan_and_keeps_the_site_out_of_maintenance(
    running_instance,
):
    inst = running_instance
    try:
        _create_site(inst, SLOW_SITE)
        _install_slow_migrate_app(inst)
        # A fresh site is not in maintenance.
        assert _maintenance(inst, SLOW_SITE) in (0, None)

        proc = subprocess.Popen(
            [harness.CWCLI, "axi", "migrate", inst.name, "--site", SLOW_SITE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # cwcli sets maintenance ON before it runs bench migrate.
            harness.wait_until(
                lambda: _maintenance(inst, SLOW_SITE) == 1,
                timeout=240,
                interval=0.25,
                desc="site enters maintenance mode",
            )
            # Wait until the deliberately-slow patch is genuinely running, so there
            # is a real in-container migrate to orphan and it is inside the
            # maintenance-holding loop when SIGTERM lands.
            harness.wait_until(
                lambda: _slow_patch_running(inst),
                timeout=240,
                interval=0.5,
                desc="the slow migrate patch is running",
            )
            # The kill under test: a plain SIGTERM, exactly what `kill`/a service
            # stop sends. cwcli must end the orphaned migrate it started, THEN clear
            # maintenance last.
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=180)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # THE GUARANTEE: after the interrupt the site is taken - and STAYS - out of
        # maintenance, because cwcli ended the orphaned migrate before clearing. The
        # looping orphan (without the fix) would flip maintenance back ON within its
        # 0.5s tick, failing the settle check fast.
        harness.wait_until(
            lambda: _maintenance(inst, SLOW_SITE) == 0,
            timeout=90,
            interval=1,
            desc="site taken back out of maintenance after SIGTERM",
        )
        settle_deadline = time.time() + SETTLE_SECONDS
        while time.time() < settle_deadline:
            assert (
                _maintenance(inst, SLOW_SITE) == 0
            ), "site re-entered maintenance after cwcli exited (orphaned migrate not stopped)"
            time.sleep(1)

        # The process cwcli started is gone: its migrate lock has been released (an
        # orphan still running the hold loop would keep it held).
        assert not _migrate_lock_held(
            inst, SLOW_SITE
        ), "the in-container migrate cwcli started is still holding its lock"
    finally:
        _cleanup(inst)
