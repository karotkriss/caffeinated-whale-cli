"""Real-Docker E2E for the ``status`` not-cwcli-supervised FALLBACK.

Guards the regression where ``cwcli status`` reported EVERY process down for a
bench cwcli's supervisord did not start (a pre-v3 instance, or a plain
``bench start``): v3 switched status detection to supervisord-only, found no
supervisord, and falsely reported all-down while the processes genuinely served.

The fallback (``core.supervision.discover_unsupervised_stack``) detects the honcho
/ ``bench start`` process tree instead and reports each process's TRUE state, flags
the report ``not_cwcli_supervised`` with an actionable hint, and NEVER launches
supervisord (a status read must not mutate a live instance - migrating is
``cwcli start``'s job).

Both legs run against the shared session instance and RESTORE it to the
supervisord-served state on the way out, so sibling tests are undisturbed.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from . import harness

pytestmark = pytest.mark.e2e


def _web_reachable(project: str) -> bool:
    code, _ = harness.exec_in_frappe(
        project, "curl -s --max-time 5 -o /dev/null http://localhost:8000"
    )
    return code == 0


def _wait_web_ready(project: str, *, timeout: int = 300) -> None:
    harness.wait_until(
        lambda: _web_reachable(project),
        timeout=timeout,
        interval=5,
        desc=f"{project} web :8000 serving",
    )


def _ensure_supervisord_serving(project: str) -> None:
    """Guarantee the bench is served by cwcli's supervisord (restores the shared state)."""
    st = harness.run_cwcli("status", project)
    supervised = st.stdout.strip() == "running" and "not under cwcli supervision" not in st.stderr
    if supervised and _web_reachable(project):
        return
    harness.run_cwcli("stop", project)
    result = harness.run_cwcli("start", project, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    _wait_web_ready(project)


def _stop_supervisord(project: str) -> None:
    """SIGTERM cwcli's supervisord for the bench, then wait until :8000 stops."""
    # supervisord shuts its whole program group down on SIGTERM; kill by name is
    # safe here because the throwaway instance runs exactly one supervisord.
    harness.exec_in_frappe(project, "pkill -TERM -f supervisord || true")
    harness.wait_until(
        lambda: not _web_reachable(project),
        timeout=120,
        interval=3,
        desc=f"{project} web down after supervisord teardown",
    )


def _launch_honcho(project: str) -> None:
    """Start the bench under honcho (``bench start``) detached - the pre-v3 shape."""
    cid = harness.frappe_container_id(project)
    assert cid, f"no frappe container for {project}"
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            "-w",
            harness.DEFAULT_BENCH_PATH,
            cid,
            "bash",
            "-lc",
            "exec bench start >> logs/benchstart-e2e.log 2>&1",
        ],
        check=True,
    )
    _wait_web_ready(project)


def _kill_honcho(project: str) -> None:
    harness.exec_in_frappe(project, "pkill -TERM -f honcho || true")


# --------------------------------------------------------------------------- #
# 1. supervisord path preserved (the v3 path must be untouched)
# --------------------------------------------------------------------------- #
def test_supervisord_started_instance_uses_supervisord_path(running_instance):
    """A cwcli-supervisord-started instance reports via the supervisord path:
    ``supervisor_up: true`` and NOT flagged ``not_cwcli_supervised`` - the v3 path
    is preserved exactly."""
    inst = running_instance
    _ensure_supervisord_serving(inst.name)

    axi = harness.run_cwcli("axi", "status", inst.name)
    assert axi.returncode == 0, axi.stdout + axi.stderr
    assert axi.stdout.splitlines()[0] == "overall: running", axi.stdout
    assert "supervisor_up: true" in axi.stdout, axi.stdout
    assert "not_cwcli_supervised: false" in axi.stdout, axi.stdout
    # Every process reports its supervisord state (only meaningful when supervised).
    assert "RUNNING" in axi.stdout, axi.stdout


# --------------------------------------------------------------------------- #
# 2. not-cwcli-supervised fallback (the regression fix), both modes
# --------------------------------------------------------------------------- #
def test_honcho_instance_reports_processes_up_not_all_down(running_instance):
    """The regression guard: with the bench served by honcho (no cwcli supervisord),
    ``status`` reports the processes UP + the not-cwcli-supervised hint, NOT all-down.

    Runs BOTH modes: non-interactive (``cwcli status`` token + stderr hint, and
    ``cwcli axi status`` TOON), and interactive (the hint rendered on a real pty).
    Restores the supervisord-served state afterward so sibling tests are undisturbed.
    """
    import pexpect

    inst = running_instance
    _ensure_supervisord_serving(inst.name)

    try:
        _stop_supervisord(inst.name)
        _launch_honcho(inst.name)
        # Sanity: the processes really are up under honcho, no cwcli supervisord.
        assert _web_reachable(inst.name), "honcho must be serving for this test"

        # --- non-interactive: the machine token + the stderr hint --------------
        st = harness.run_cwcli("status", inst.name)
        assert st.returncode == 0, st.stdout + st.stderr
        # The true aggregate, NOT offline/online-all-down.
        assert st.stdout.strip() == "running", st.stdout + st.stderr
        assert "not under cwcli supervision" in st.stderr, st.stderr
        assert "cwcli start" in st.stderr, st.stderr
        # The per-process breakdown shows real up processes (not every one "down").
        assert "up" in st.stderr and "web" in st.stderr, st.stderr

        # --- non-interactive: axi TOON (agent contract) -----------------------
        axi = harness.run_cwcli("axi", "status", inst.name)
        assert axi.returncode == 0, axi.stdout + axi.stderr
        assert axi.stdout.splitlines()[0] == "overall: running", axi.stdout
        assert "supervisor_up: false" in axi.stdout, axi.stdout
        assert "not_cwcli_supervised: true" in axi.stdout, axi.stdout
        assert "web,true," in axi.stdout, axi.stdout  # web is genuinely up
        assert "cwcli start" in axi.stdout, axi.stdout  # hint rides in warnings

        # --- interactive: the hint renders on a real pty ----------------------
        child = harness.spawn_cwcli(["status", inst.name, "--verbose"], timeout=120)
        log = io.StringIO()
        child.logfile_read = log
        try:
            child.expect(pexpect.EOF, timeout=90)
        finally:
            child.close(force=True)
        out = harness.strip_ansi(log.getvalue())
        assert "not under cwcli supervision" in out, out[-1500:]
        assert "web" in out and "up" in out, out[-1500:]
    finally:
        # Restore the shared instance to the supervisord-served state.
        _kill_honcho(inst.name)
        _ensure_supervisord_serving(inst.name)
