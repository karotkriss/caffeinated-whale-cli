"""Real-Docker E2E for the per-process supervisor (add-per-process-supervisor).

The features honcho's all-or-nothing model made impossible, proven on a real
bench:

  - the bench runs under **supervisord** (not honcho), installed into the bench env.
  - ``cwcli restart --process <label>`` cycles ONE program (new pid) while its
    siblings keep their pids and the site keeps serving.
  - ``cwcli axi restart --process <label>`` emits the outcome as one TOON document.
  - **auto-heal**: killing one program's process gets it auto-restarted by
    supervisord (a new pid), with the web server undisturbed.
  - ``cwcli logs --process`` tails one program's file.
  - an unknown ``--process`` is a usage error listing the valid labels.

PIDs are read from supervisord itself (``supervisorctl status`` - the authoritative
owner of each program's pid), not by guessing a program's cmdline. Runs on the
shared session instance; ``_ensure_serving`` (from the net module) keeps sibling
tests undisturbed.
"""

from __future__ import annotations

import pytest

from . import harness
from .test_start_status_e2e import (
    BENCH_PY as _PY,
)
from .test_start_status_e2e import (
    SUPERVISOR_CFG as _CFG,
)
from .test_start_status_e2e import (
    _ensure_serving,
    _wait_web_ready,
    _web_reachable,
)

pytestmark = pytest.mark.e2e

BENCH = harness.DEFAULT_BENCH_PATH


def _proc_pids(project: str, pattern: str) -> list[int]:
    """Sorted PIDs whose cmdline matches ``pattern`` (a bracketed grep avoids self-match)."""
    code, out = harness.exec_in_frappe(
        project, f"ps -eo pid,args | grep '{pattern}' | awk '{{print $1}}'"
    )
    return sorted(int(x) for x in out.split() if x.strip().isdigit())


def _sup_pid(project: str, program: str) -> int | None:
    """The pid supervisord tracks for ``program`` (its authoritative view), or None."""
    code, out = harness.exec_in_frappe(
        project, f"{_PY} -m supervisor.supervisorctl -c {_CFG} status {program}"
    )
    parts = out.split()
    for i, tok in enumerate(parts):
        if tok == "pid" and i + 1 < len(parts):
            raw = parts[i + 1].rstrip(",")
            return int(raw) if raw.isdigit() else None
    return None


# --------------------------------------------------------------------------- #
# TEMPORARY DIAGNOSTIC - remove before this branch ships.
#
# Question it exists to settle: when a sibling program's pid changes across a
# `restart --process web`, did that program EXIT ON ITS OWN (a crash - supervisord
# logs "exit status N") or was it SIGNALLED (supervisord logs "terminated by
# SIGxxx", or "stopped:" when supervisord asked it to stop)? Those two readings
# imply completely different fixes, and the distinction is recorded verbatim in
# supervisord's own log, so it is read rather than inferred.
#
# It prints on EVERY leg, passing ones included, because the v14 leg fails where
# v15 and v16 pass - so the evidence that matters is the DIFFERENCE between them,
# which a failure-only dump could not show.
# --------------------------------------------------------------------------- #
_SUPERVISORD_LOG = f"{BENCH}/logs/.cwcli-supervisord.log"


def _diag(project: str, when: str) -> None:
    _, transitions = harness.exec_in_frappe(
        project,
        f"grep -E 'schedule|web' {_SUPERVISORD_LOG} 2>/dev/null | tail -25",
    )
    _, sched_log = harness.exec_in_frappe(
        project, f"tail -15 {BENCH}/logs/schedule.supervisor.log 2>/dev/null"
    )
    # Process groups test the one concrete mechanism by which restarting web could
    # signal a sibling: `stopasgroup`/`killasgroup` signal the whole process GROUP,
    # so if schedule shares web's PGID, restarting web necessarily takes it down.
    _, groups = harness.exec_in_frappe(
        project, "ps -eo pid,pgid,args | grep -E '[s]chedule|[b]ench serve|[f]rappe serve'"
    )
    print(f"\n===== DIAG {when} ({project}) =====")
    print(f"--- supervisord transitions ---\n{transitions.strip()}")
    print(f"--- schedule program log ---\n{sched_log.strip()}")
    print(f"--- pid/pgid ---\n{groups.strip()}")


# --------------------------------------------------------------------------- #
# 1. the bench runs under supervisord, installed into the bench env
# --------------------------------------------------------------------------- #
def test_bench_runs_under_supervisord(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    # supervisord is the live supervisor (not honcho).
    assert _proc_pids(inst.name, "[s]upervisord"), "supervisord must be the live supervisor"
    assert not _proc_pids(inst.name, "[h]oncho start"), "honcho must not be the supervisor"

    # supervisor was installed into the bench virtualenv (the first-supervise bootstrap).
    code, _ = harness.exec_in_frappe(inst.name, f"{_PY} -c 'import supervisor'")
    assert code == 0, "supervisor must be importable in the bench env"

    # the generated config + launcher are on the workspace volume.
    for rel in ("logs/.cwcli-supervisor.conf", "logs/.cwcli-run.sh"):
        code, _ = harness.exec_in_frappe(inst.name, f"test -f {BENCH}/{rel}")
        assert code == 0, f"{rel} must exist"


# --------------------------------------------------------------------------- #
# 2. cwcli restart --process cycles ONE program, siblings + site undisturbed
# --------------------------------------------------------------------------- #
def test_restart_process_cycles_one_leaves_siblings(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    web_before = _sup_pid(inst.name, "web")
    sched_before = _sup_pid(inst.name, "schedule")
    _diag(inst.name, "BEFORE restart")  # TEMPORARY DIAGNOSTIC
    assert web_before and sched_before, "web + schedule must be up before the restart"

    res = harness.run_cwcli("restart", inst.name, "--process", "web")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "restarted process" in res.stdout, res.stdout

    # web got a NEW pid; schedule (a sibling) kept its pid; the site still serves.
    harness.wait_until(
        lambda: _sup_pid(inst.name, "web") not in (None, web_before),
        timeout=120,
        interval=3,
        desc="web restarted with a new pid",
    )
    sched_after = _sup_pid(inst.name, "schedule")
    _diag(inst.name, f"AFTER restart (schedule {sched_before} -> {sched_after})")  # TEMPORARY
    assert sched_after == sched_before, "siblings must be untouched"
    _wait_web_ready(inst.name)
    assert _web_reachable(inst.name)


# --------------------------------------------------------------------------- #
# 3. cwcli axi restart --process emits one TOON outcome
# --------------------------------------------------------------------------- #
def test_axi_restart_emits_toon_outcome(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("axi", "restart", inst.name, "--process", "schedule")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "label: schedule" in res.stdout, res.stdout
    assert "supervisor_state:" in res.stdout, res.stdout
    assert "new_pid:" in res.stdout, res.stdout
    # stdout stays TOON-clean.
    assert "Restarting" not in res.stdout
    _wait_web_ready(inst.name)


def test_axi_restart_unknown_process_is_usage_error(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("axi", "restart", inst.name, "--process", "nope")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "--process" in res.stdout, res.stdout
    # the valid labels are listed so an agent can retry.
    assert "options[" in res.stdout, res.stdout


# --------------------------------------------------------------------------- #
# 4. auto-heal: a killed program is auto-restarted, web undisturbed
# --------------------------------------------------------------------------- #
def test_killed_program_is_auto_restarted(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    sched_before = _sup_pid(inst.name, "schedule")
    assert sched_before, "schedule must be up before the kill"

    # Kill schedule directly (NOT via cwcli): supervisord's autorestart should
    # bring it back on its own, with no cwcli process running.
    harness.exec_in_frappe(inst.name, f"kill -TERM {sched_before} || true")

    harness.wait_until(
        lambda: _sup_pid(inst.name, "schedule") not in (None, sched_before),
        timeout=120,
        interval=3,
        desc="schedule auto-restarted by supervisord",
    )
    # The web server was never touched.
    assert _web_reachable(inst.name), "web must stay up while a sibling self-heals"


# --------------------------------------------------------------------------- #
# 5. cwcli logs --process tails one program's file
# --------------------------------------------------------------------------- #
def test_logs_process_tails_one_file(running_instance):
    import io

    import pexpect

    inst = running_instance
    _ensure_serving(inst.name)

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
# 6. status reports supervisord per-process state
# --------------------------------------------------------------------------- #
def test_axi_status_reports_supervisord_state(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("axi", "status", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    assert lines[0] == "overall: running", res.stdout
    # the per-process table carries supervisord's state column. It is NESTED under
    # its bench now (report-status-per-bench), so it is indented, not at column 0.
    assert any(
        line.lstrip().startswith("processes[") and "state" in line for line in lines
    ), res.stdout
    assert "RUNNING" in res.stdout, res.stdout
