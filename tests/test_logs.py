"""``cwcli logs``: per-process supervisord log tailing.

Behaviors pinned:

- **Default is non-follow.** ``logs`` used to default ``--follow`` on, so a plain
  ``cwcli logs proj`` blocked forever tailing (``tail -F``). The default is now
  off; ``-f`` / ``--follow`` opts in.
- **``-it`` is gated on ``sys.stdin.isatty()``.** ``docker exec -it`` errors "the
  input device is not a TTY" under a pipe/agent, so the ``-i``/``-t`` flags are
  only added when actually interactive.
- **``--process`` tails one program's file; omitted tails them all** (a combined
  view over the per-process supervisord log files).

The default/parsing assertions go through the real Typer parser (``CliRunner``);
the ``isatty`` gating is exercised by calling the command function directly, since
``CliRunner`` redirects ``sys.stdin`` and would defeat an ``isatty`` monkeypatch.
"""

from __future__ import annotations

import subprocess
import types

import pytest
import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import logs as logs_mod
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import logs as core_logs
from caffeinated_whale_cli.core import resolvers, supervision
from caffeinated_whale_cli.utils import docker_utils

runner = CliRunner()


class _FakeFrappe:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    status = "running"

    def reload(self):
        pass


def _wire(monkeypatch, *, isatty: bool, returncode: int = 0):
    """Neutralize the Docker preflight and wire the ``core.logs_plan`` seam.

    The resolve now lives in ``core/logs.py``, so the collaborators are patched
    THERE (``get_project_containers``, ``cached_benches``, ``procfile_programs``,
    ``_existing_files``), not on the command module. The frontend's own bits
    (``ensure_containers_running``, ``sys.stdin``, ``subprocess.run``) stay patched
    on ``logs_mod``.

    ``returncode`` is what the faked ``tail`` exits with. The fake models
    ``subprocess.run``'s REAL ``check=`` semantics - it raises
    ``CalledProcessError`` only when ``check=True`` - because that is precisely
    what made the old ``except subprocess.CalledProcessError`` handler
    unreachable, and what a future ``check=True`` "fix" would break (it would
    turn a user's Ctrl+C into an exception; see the 130 test).
    """
    # @handle_docker_errors preflight (no real Docker on the unit tier).
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(logs_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [_FakeFrappe()])
    monkeypatch.setattr(
        resolvers, "cached_benches", lambda _p: [{"path": "/w/bench", "label": None}]
    )
    monkeypatch.setattr(supervision, "procfile_programs", lambda c, b: ["web", "worker_default"])
    # Every candidate log "exists" (echo the list back), so the tail targets them.
    monkeypatch.setattr(core_logs, "_existing_files", lambda container, files: files)
    monkeypatch.setattr(logs_mod.sys, "stdin", types.SimpleNamespace(isatty=lambda: isatty))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if k.get("check") and returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd)
        return types.SimpleNamespace(returncode=returncode, stdout="")

    monkeypatch.setattr(logs_mod.subprocess, "run", fake_run)
    return calls


def _tail_cmd(calls: list[list[str]]) -> list[str]:
    # The tail run is always the FIRST docker exec; a non-TTY `--follow` adds a
    # second exec afterwards (the orphan-tail reap), which is never what we assert on.
    return calls[0]


def _app():
    app = typer.Typer()
    app.command()(logs_mod.logs)
    return app


def _call_logs(follow: bool, process: str | None = None):
    """Invoke the command function directly with every param explicit (Typer's
    ``Option`` defaults are left as objects otherwise; see the inspect tests)."""
    logs_mod.logs(
        project_name="proj",
        follow=follow,
        lines=100,
        bench=None,
        process=process,
        yes=False,
        verbose=False,
    )


def test_default_is_non_follow(monkeypatch):
    # Real Typer parsing: no --follow given -> the new default (False) -> `tail` (no -F).
    calls = _wire(monkeypatch, isatty=True)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 0
    tail = _tail_cmd(calls)
    assert "-F" not in tail  # not following
    assert "tail" in tail
    # No --process -> every program's log file is tailed (combined view).
    assert "/w/bench/logs/web.supervisor.log" in tail
    assert "/w/bench/logs/worker_default.supervisor.log" in tail


def test_follow_flag_opts_in(monkeypatch):
    calls = _wire(monkeypatch, isatty=True)
    result = runner.invoke(_app(), ["proj", "--follow"])
    assert result.exit_code == 0
    # CliRunner replaces sys.stdin, so this runs the non-TTY follow path where the
    # tail is wrapped in `sh -c` (the orphan-reap machinery); -F rides in the script.
    assert "-F" in " ".join(str(part) for part in _tail_cmd(calls))  # -f opted into following


def test_process_tails_one_file(monkeypatch):
    calls = _wire(monkeypatch, isatty=True)
    result = runner.invoke(_app(), ["proj", "--process", "worker:default"])
    assert result.exit_code == 0
    tail = _tail_cmd(calls)
    assert "/w/bench/logs/worker_default.supervisor.log" in tail
    assert "/w/bench/logs/web.supervisor.log" not in tail


def test_unknown_process_errors(monkeypatch):
    _wire(monkeypatch, isatty=True)
    result = runner.invoke(_app(), ["proj", "--process", "nope"])
    assert result.exit_code == 1


def test_no_it_flag_without_tty(monkeypatch):
    # Non-TTY (piped/agent): `docker exec` must NOT carry -it.
    calls = _wire(monkeypatch, isatty=False)
    _call_logs(follow=False)
    assert "-it" not in _tail_cmd(calls)


def test_it_flag_added_with_tty(monkeypatch):
    # Interactive TTY: -it is present so tail -F stays attached.
    calls = _wire(monkeypatch, isatty=True)
    _call_logs(follow=True)
    tail = _tail_cmd(calls)
    assert "-it" in tail
    assert "-F" in tail


def test_supervised_path_never_calls_fallback(monkeypatch):
    # When cwcli-supervisord per-process logs exist, the honcho/bench-start fallback
    # must NOT run (the supervised path stays byte-for-byte as before).
    calls = _wire(monkeypatch, isatty=True)

    def _boom(*a, **k):
        raise AssertionError("fallback discover_unsupervised_stack called on supervised path")

    monkeypatch.setattr(supervision, "discover_unsupervised_stack", _boom)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 0
    assert "/w/bench/logs/web.supervisor.log" in _tail_cmd(calls)


# ------------------------------ tail's exit code ------------------------------
# `logs` called `subprocess.run(tail_cmd)` with no check=, discarded the result,
# and caught `subprocess.CalledProcessError` beneath it - which `subprocess.run`
# raises ONLY when check=True. So the returncode was thrown away and the handler
# was unreachable dead code: `cwcli logs` exited 0 no matter what tail did (PR #83).
#
# The tail stays in the frontend and is NOT re-pointed onto `core.exec_stream`;
# see `core/logs.py`'s module docstring and
# `openspec/changes/migrate-logs-core/design.md` (Decision 1) for the measured
# reasons (an orphan `tail -F` per Ctrl+C, `exec.stream_lost` on a routine stop,
# the tty/demux conflict). These five pin PR #83's fix through the public surface,
# and the migration must keep them green in substance.


def test_failing_tail_exits_non_zero(monkeypatch):
    # The headline repro, through the real Typer parser: tail exits 1, so must cwcli.
    _wire(monkeypatch, isatty=False, returncode=1)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 1


def test_failing_tail_propagates_the_real_code(monkeypatch):
    # 137 = SIGKILL (128+9). The code is propagated, not flattened to 1.
    _wire(monkeypatch, isatty=False, returncode=137)
    with pytest.raises(typer.Exit) as excinfo:
        _call_logs(follow=False)
    assert excinfo.value.exit_code == 137


def test_ctrl_c_through_dockers_tty_is_a_clean_exit(monkeypatch):
    # Verified against real docker: on the `-it` path docker puts the terminal in
    # raw mode and forwards ^C INTO the container, so the user's own Ctrl+C arrives
    # as tail exiting 130 and NOT as a KeyboardInterrupt here. Reporting that as a
    # failure would make every interactive `cwcli logs -f` exit non-zero - which is
    # why the fix is not simply `check=True`.
    _wire(monkeypatch, isatty=True, returncode=130)
    _call_logs(follow=True)  # returns normally == exit 0


def test_keyboard_interrupt_is_a_clean_exit(monkeypatch):
    # The non-TTY path (no `-it`): SIGINT reaches cwcli itself instead.
    _wire(monkeypatch, isatty=False)

    calls: list[list[str]] = []

    def interrupted(cmd, *a, **k):
        calls.append(cmd)
        # Only the tail run is interrupted; the best-effort orphan-reap that runs
        # in the `finally` must still complete (it models a real docker exec).
        if "tail" in cmd or any("tail" in str(part) for part in cmd):
            raise KeyboardInterrupt
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(logs_mod.subprocess, "run", interrupted)
    _call_logs(follow=True)  # returns normally == exit 0


def test_non_tty_follow_reaps_the_container_tail(monkeypatch):
    # The orphan-tail leak: on the non-TTY `--follow` path, Ctrl+C kills the
    # `docker exec` client but its exec'd `tail -F` keeps running in the container
    # (Docker has no kill-exec API). The tail is wrapped to record its own PID, and
    # a `finally` reap kills that PID so no orphan survives the follower's exit.
    calls = _wire(monkeypatch, isatty=False)
    _call_logs(follow=True)

    # First exec: the PID-recording tail wrapper.
    tail_run = calls[0]
    assert tail_run[:2] == ["docker", "exec"]
    assert "-it" not in tail_run  # non-TTY
    script = tail_run[-1]
    assert "echo $$ >" in script and "exec tail" in script and "-F" in script

    # Second exec: the reap. Kills the recorded PID; needs only kill/cat/rm.
    reap = calls[-1]
    assert reap[:2] == ["docker", "exec"]
    assert "kill $(cat" in reap[-1]
    # Same pidfile written then killed - no orphan left behind.
    import re

    pidfile = re.search(r"(/tmp/cwcli-logs-\S+\.pid)", script).group(1)
    assert pidfile in reap[-1]


def test_successful_tail_exits_zero(monkeypatch):
    _wire(monkeypatch, isatty=False, returncode=0)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 0


# ------------------------- not-cwcli-supervised fallback -------------------------
# A bench running under honcho / `bench start` (pre-v3, or a plain `bench start`)
# has NO `*.supervisor.log` files, so the supervisord path finds nothing. The
# fallback discovers the bench's REAL log files under logs/ and tails those.


def _wire_unsupervised(monkeypatch, *, manager_up, real_files, isatty=False):
    """No supervisord logs; a honcho manager may (manager_up) be running the bench.

    Same seam as ``_wire``, but the existence probe finds nothing (forcing the
    fallback) and ``discover_unsupervised_stack`` / ``_discover_bench_log_files``
    are wired on ``core_logs``.
    """
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(logs_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [_FakeFrappe()])
    monkeypatch.setattr(
        resolvers, "cached_benches", lambda _p: [{"path": "/w/bench", "label": None}]
    )
    monkeypatch.setattr(supervision, "procfile_programs", lambda c, b: ["web", "worker_default"])
    # No supervisord per-process logs exist -> forces the fallback branch.
    monkeypatch.setattr(core_logs, "_existing_files", lambda container, files: [])
    monkeypatch.setattr(
        supervision,
        "discover_unsupervised_stack",
        lambda c, b: supervision.UnsupervisedStack(manager_up=manager_up, processes=[]),
    )
    monkeypatch.setattr(
        core_logs, "_discover_bench_log_files", lambda container, b: list(real_files)
    )
    monkeypatch.setattr(logs_mod.sys, "stdin", types.SimpleNamespace(isatty=lambda: isatty))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(logs_mod.subprocess, "run", fake_run)
    return calls


def test_fallback_tails_real_logs_when_honcho_running(monkeypatch):
    real = ["/w/bench/logs/bench.log", "/w/bench/logs/web.log", "/w/bench/logs/worker.log"]
    calls = _wire_unsupervised(monkeypatch, manager_up=True, real_files=real, isatty=True)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 0
    tail = _tail_cmd(calls)
    assert "tail" in tail
    for f in real:
        assert f in tail
    # It must NOT invent supervisord names that don't exist.
    assert "/w/bench/logs/web.supervisor.log" not in tail


def test_fallback_process_filters_real_logs(monkeypatch):
    real = [
        "/w/bench/logs/web.error.log",
        "/w/bench/logs/web.log",
        "/w/bench/logs/worker.log",
    ]
    calls = _wire_unsupervised(monkeypatch, manager_up=True, real_files=real, isatty=True)
    result = runner.invoke(_app(), ["proj", "--process", "web"])
    assert result.exit_code == 0
    tail = _tail_cmd(calls)
    assert "/w/bench/logs/web.log" in tail
    assert "/w/bench/logs/web.error.log" in tail
    assert "/w/bench/logs/worker.log" not in tail


def test_not_running_reports_start_hint(monkeypatch):
    # Container up but NEITHER supervisord nor honcho manages the bench: honest
    # "may not be running" guidance, exit 1, and NO tail is attempted.
    calls = _wire_unsupervised(monkeypatch, manager_up=False, real_files=[], isatty=True)
    result = runner.invoke(_app(), ["proj"])
    assert result.exit_code == 1
    assert calls == []  # never reached the tail


def test_running_but_no_matching_log_is_not_reported_as_down(monkeypatch):
    # honcho IS running but the requested process has no log file yet: the message
    # must NOT say the bench may not be running (it IS running).
    calls = _wire_unsupervised(
        monkeypatch, manager_up=True, real_files=["/w/bench/logs/web.log"], isatty=True
    )
    result = runner.invoke(_app(), ["proj", "--process", "worker:default"])
    assert result.exit_code == 1
    assert calls == []
    assert "may not be running" not in result.output.lower()
