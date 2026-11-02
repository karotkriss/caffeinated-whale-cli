"""Real-Docker E2E: cwcli finds a bench left in maintenance mode, asks, and takes it out.

The incident this guards: a bench's ``sites/common_site_config.json`` held
``maintenance_mode: 1`` and ``pause_scheduler: 1`` (what ``bench update`` writes for
its whole run and clears only on success), so every site answered HTTP 503
``SessionStopped`` and nothing in cwcli said so or offered the fix.

On the shared session instance this sets exactly those bench-wide flags, waits until
the site really answers 503 (Frappe v16 answers from a 60-second config cache, so
the files and the answer disagree for up to a minute), then proves, in both modes:

- **refusal while a migrate runs**: a real ``bench migrate`` on a throwaway site is
  frozen (``SIGSTOP``, the technique ``test_migrate_sigterm_maintenance_e2e`` uses),
  and ``cwcli status --clear-maintenance`` refuses with exit 1, names it, and writes
  nothing - on every major, because the in-bench process scan finds it (v16 also
  holds the migrate lock);
- **non-TTY**: ``cwcli status`` never asks, names ``--clear-maintenance``, exits 0
  and keeps stdout to its one token;
- **TTY**: on a real pty ``cwcli status`` asks; answering yes clears both flags
  (the scheduler resumes with them) and the site answers Frappe's ping with 200.

A ``finally`` always puts the flags back to 0 and removes the throwaway site, so
sibling tests on the shared instance are undisturbed.
"""

from __future__ import annotations

import json
import subprocess
import warnings

import pexpect
import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW
from .test_start_status_e2e import _ensure_serving

pytestmark = pytest.mark.e2e

# A dedicated site for the frozen migrate, so the shared default site is never
# migrated half-way.
MIGRATE_SITE = "cwe2e-maint-migrate.localhost"
# Marks the frozen migrate's process tree (inherited by its children), so the test
# freezes and ends exactly it.
FREEZE_ENV = "CWE2E_MAINT_FREEZE"


def _common(inst) -> dict:
    code, out = harness.exec_in_frappe(inst.name, f"cat {inst.bench}/sites/common_site_config.json")
    assert code == 0, out
    return json.loads(out)


def _set_bench_flags(inst, value: int) -> None:
    for flag in ("maintenance_mode", "pause_scheduler"):
        code, out = harness.exec_in_frappe(
            inst.name, f"cd {inst.bench} && bench set-config --global --parse {flag} {value}"
        )
        assert code == 0, out


def _ping(inst, port: int) -> tuple[str, bool]:
    """The site's answer to Frappe's ping: (http_code, SessionStopped in the body)."""
    _code, out = harness.exec_in_frappe(
        inst.name,
        f"curl -s --max-time 10 -H 'Host: {inst.site}' -w ' %{{http_code}}' "
        f"http://localhost:{port}/api/method/ping",
    )
    return out.rsplit(" ", 1)[-1].strip(), "SessionStopped" in out


def _marked_pids(inst) -> list[str]:
    script = (
        "for d in /proc/[0-9]*; do "
        f'grep -aqzF -- {FREEZE_ENV}=1 "$d/environ" 2>/dev/null && echo "${{d##*/}}"; '
        "done"
    )
    _code, out = harness.exec_in_frappe(inst.name, script)
    return [p for p in out.split() if p.isdigit()]


def _signal_marked(inst, sig: str) -> None:
    pids = _marked_pids(inst)
    if pids:
        harness.exec_in_frappe(inst.name, f"kill -{sig} {' '.join(pids)} 2>/dev/null || true")


def _create_site(inst, site: str) -> None:
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


def _drop_site(inst, site: str) -> None:
    """Best-effort, bounded, NEVER-RAISING teardown of the throwaway site."""
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{site}")
    if code != 0:
        return
    try:
        harness.run_cwcli("rm-site", inst.name, site, "--yes", timeout=300)
    except subprocess.TimeoutExpired:
        warnings.warn(f"teardown of {site} timed out; leaving it for session sweep", stacklevel=2)


def test_a_bench_left_in_maintenance_is_found_offered_and_taken_out(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    port = int(_common(inst).get("webserver_port") or 8000)
    cid = harness.frappe_container_id(inst.name)
    try:
        _create_site(inst, MIGRATE_SITE)
        _set_bench_flags(inst, 1)
        # Frappe v16 answers from its config cache for up to a minute: wait for the
        # real 503 so the rest of the test sees what a user sees.
        harness.wait_until(
            lambda: _ping(inst, port) == ("503", True),
            timeout=150,
            interval=3,
            desc="the site answers 503 SessionStopped",
        )

        # --- refusal while a real migrate runs on the bench -----------------------
        subprocess.Popen(
            [
                "docker",
                "exec",
                "-d",
                "-e",
                f"{FREEZE_ENV}=1",
                "-w",
                inst.bench,
                cid,
                "bench",
                "--site",
                MIGRATE_SITE,
                "migrate",
            ]
        ).wait(timeout=60)
        harness.wait_until(
            lambda: bool(_marked_pids(inst)),
            timeout=120,
            interval=0.5,
            desc="the in-container migrate exists",
        )
        _signal_marked(inst, "STOP")
        refused = harness.run_cwcli("status", inst.name, "--clear-maintenance", timeout=300)
        err = harness.collapse_ws(harness.strip_ansi(refused.stderr))
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert "Not cleared:" in err
        assert "'bench migrate' is still running" in err or "migrate lock" in err, err
        assert _common(inst).get("maintenance_mode") == 1  # nothing was written
        _signal_marked(inst, "KILL")
        harness.wait_until(
            lambda: not _marked_pids(inst), timeout=60, interval=1, desc="the migrate is gone"
        )

        # --- a non-TTY is never asked and is told the flag ------------------------
        quiet = harness.run_cwcli("status", inst.name, timeout=300)
        quiet_err = harness.collapse_ws(harness.strip_ansi(quiet.stderr))
        assert quiet.returncode == 0, quiet.stdout + quiet.stderr
        assert quiet.stdout.strip() in ("running", "degraded")
        assert "maintenance_mode is set bench-wide" in quiet_err
        assert "--clear-maintenance" in quiet_err
        assert "Out of maintenance mode" not in quiet_err
        assert _common(inst).get("maintenance_mode") == 1

        # --- a terminal is asked, and yes takes the bench out ---------------------
        child = harness.spawn_cwcli(["status", inst.name], timeout=300)
        try:
            child.expect("out of maintenance mode and resume the scheduler", timeout=180)
            harness.expect_prompt_ready(child)
            child.send("y")
            child.sendline("")
            child.expect("Out of maintenance mode:", timeout=240)
            child.expect(pexpect.EOF, timeout=120)
        finally:
            child.close()
        assert child.exitstatus == 0, child.before

        common = _common(inst)
        assert common.get("maintenance_mode") == 0
        assert common.get("pause_scheduler") == 0  # the scheduler resumed with it
        assert _ping(inst, port) == ("200", False)
    finally:
        _signal_marked(inst, "KILL")
        try:
            _set_bench_flags(inst, 0)
        except AssertionError as e:  # pragma: no cover - defensive teardown
            warnings.warn(f"could not reset the bench-wide flags: {e}", stacklevel=2)
        _drop_site(inst, MIGRATE_SITE)
