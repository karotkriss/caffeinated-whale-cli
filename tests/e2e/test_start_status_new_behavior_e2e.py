"""Real-Docker E2E for the NEW start/status behavior (migrate-start-status-core),
updated for the supervisord per-process supervisor (add-per-process-supervisor).

These sit ON TOP of the structure-agnostic PR-1 net (``test_start_status_e2e.py``,
which stays green unchanged) and assert the migration's ADDED behavior - the
things the net deliberately does NOT pin:

  - ``start`` is genuinely idempotent: a re-run is a no-op that leaves EXACTLY ONE
    supervisord supervisor (the double-start bug stays fixed), reported as
    ``already_running: true`` by ``cwcli axi start``.
  - ``status`` reports REAL per-process health (up/uptime/CPU/RSS/state) + the
    aggregate, via both the human token (stdout) and ``cwcli axi status`` TOON.
  - ``degraded``: with the marker present but the supervisor down, status says
    ``degraded``.
  - the captured logs are per-process files on the bench ``logs/`` volume (not
    ``/tmp``, and no combined ``bench-start.log``).
  - multi-bench with no selector REFUSES non-interactively (never silently picks).

Runs on the shared session instance; every test that leaves it un-served relies on
``_ensure_serving`` (imported from the net module) to restore sibling tests.
"""

from __future__ import annotations

import io

import pytest

from . import harness
from .test_start_status_e2e import _ensure_serving, _wait_web_ready, _web_reachable

pytestmark = pytest.mark.e2e


def _supervisord_count(project: str) -> int:
    """How many supervisord supervisors are live in the container (double-start guard)."""
    code, out = harness.exec_in_frappe(project, "ps -eo args | grep -c '[s]upervisord' || true")
    if code != 0:
        return -1
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


# --------------------------------------------------------------------------- #
# 1. idempotent start - exactly one supervisor, reported as already_running
# --------------------------------------------------------------------------- #
def test_start_rerun_is_idempotent_single_supervisor(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    assert _supervisord_count(inst.name) == 1, "setup: exactly one supervisord after serving"

    # axi start on an already-running bench is a clean no-op (exit 0, TOON, no prompt).
    res = harness.run_cwcli("axi", "start", inst.name, "--yes")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "already_running: true" in res.stdout, res.stdout
    assert "supervisor: supervisord" in res.stdout, res.stdout
    # stdout stays TOON-clean (no progress chatter).
    assert "Starting" not in res.stdout

    # The load-bearing invariant: NO second supervisord was spawned.
    assert _supervisord_count(inst.name) == 1, "a re-run must not spawn a second supervisord"
    assert _web_reachable(inst.name), "the instance is still serving after the no-op"


# --------------------------------------------------------------------------- #
# 2. status reports real per-process health
# --------------------------------------------------------------------------- #
def test_axi_status_reports_per_process_health(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("axi", "status", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    # overall aggregate leads the TOON document.
    assert lines[0] == "overall: running", res.stdout
    # A real per-process table with resource columns, and at least the web row up.
    assert any(line.startswith("processes[") and "uptime_s" in line for line in lines), res.stdout
    assert "supervisor_up: true" in res.stdout


def test_human_status_running_token_with_detail_on_stderr(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("status", inst.name, "-v")
    assert res.returncode == 0, res.stdout + res.stderr
    # stdout is ONLY the aggregate token (the PR-1 net contract, preserved).
    assert res.stdout.strip() == "running", res.stdout
    # the per-process breakdown is on stderr.
    assert any(tok in res.stderr for tok in ("web", "redis", "worker", "schedule")), res.stderr


# --------------------------------------------------------------------------- #
# 3. degraded when the supervisor is down but the marker is present
# --------------------------------------------------------------------------- #
def test_status_degraded_when_supervisor_down(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)  # writes the supervisor marker

    # Kill supervisord itself (it shuts its program group down); the marker stays.
    # (Killing one program would auto-restart, so target the supervisor.)
    harness.exec_in_frappe(inst.name, "pkill -TERM -f '[s]upervisord' || true")
    harness.wait_until(
        lambda: not _web_reachable(inst.name),
        timeout=120,
        interval=3,
        desc=f"{inst.name} supervisord down",
    )

    res = harness.run_cwcli("status", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "degraded", res.stdout

    axi = harness.run_cwcli("axi", "status", inst.name)
    assert axi.stdout.splitlines()[0] == "overall: degraded", axi.stdout
    assert "supervisor_up: false" in axi.stdout


# --------------------------------------------------------------------------- #
# 4. logs are per-process files on the workspace volume, not /tmp or a combined log
# --------------------------------------------------------------------------- #
def test_logs_are_per_process_files_on_the_volume(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    bench = harness.DEFAULT_BENCH_PATH
    # supervisord writes one <program>.supervisor.log per Procfile program.
    code, _ = harness.exec_in_frappe(inst.name, f"test -f {bench}/logs/web.supervisor.log")
    assert code == 0, f"web's per-process log must exist at {bench}/logs/web.supervisor.log"
    # The old honcho combined log and the ephemeral /tmp path are no longer used.
    code_combined, _ = harness.exec_in_frappe(inst.name, f"test -f {bench}/logs/bench-start.log")
    assert code_combined != 0, "the old combined bench-start.log must no longer be written"
    code_tmp, _ = harness.exec_in_frappe(inst.name, f"test -f /tmp/bench-{inst.name}.log")
    assert code_tmp != 0, "the old /tmp/bench-<project>.log must no longer be written"

    # cwcli logs --process tails one program's file and shows real content.
    import pexpect

    child = harness.spawn_cwcli(["logs", inst.name, "--process", "web", "--no-follow"], timeout=300)
    log = io.StringIO()
    child.logfile_read = log
    try:
        child.expect(pexpect.EOF, timeout=180)
    finally:
        child.close(force=True)
    out = harness.strip_ansi(log.getvalue())
    assert f"Viewing bench logs for '{inst.name}'" in out, out[-1500:]


# --------------------------------------------------------------------------- #
# 5. multi-bench with no selector refuses non-interactively (never silently picks)
# --------------------------------------------------------------------------- #
def test_multibench_no_selector_refuses_noninteractively(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    second = "/workspace/cwe2e-second-bench"

    # A cheap SECOND bench skeleton that inspect's detector recognizes
    # (apps/ + sites/ + sites/common_site_config.json). inspect discovers benches
    # ONLY under registered search roots (`cwcli init` self-registers each bench's
    # path via `config add-path`; there is no broad filesystem scan), so a bare
    # mkdir is invisible - register the skeleton's path so `cwcli inspect` finds it
    # and the cache reports two benches.
    harness.exec_in_frappe(
        inst.name,
        f"mkdir -p {second}/apps {second}/sites && echo '{{}}' > {second}/sites/common_site_config.json",
    )
    try:
        reg = harness.run_cwcli("config", "add-path", second)
        assert reg.returncode == 0, reg.stdout + reg.stderr

        # Populate the cache with BOTH benches.
        insp = harness.run_cwcli("inspect", inst.name)
        assert insp.returncode == 0, insp.stdout + insp.stderr

        # axi start with no --bench: a usage error naming --bench, exit 2, never prompts.
        res = harness.run_cwcli("axi", "start", inst.name, "--yes")
        assert res.returncode == 2, res.stdout + res.stderr
        assert "--bench" in res.stdout, res.stdout

        # human status from a non-TTY with no --bench: refuse (non-zero), never pick one.
        st = harness.run_cwcli("status", inst.name)
        assert st.returncode != 0, st.stdout + st.stderr
    finally:
        # Restore the single-bench state so sibling tests are undisturbed: drop the
        # registered path + the skeleton, then re-inspect back to one bench.
        harness.run_cwcli("config", "remove-path", second)
        harness.exec_in_frappe(inst.name, f"rm -rf {second}")
        harness.run_cwcli("inspect", inst.name)
        # Make sure the shared instance is still serving for whatever runs next.
        _ensure_serving(inst.name)
        _wait_web_ready(inst.name)
