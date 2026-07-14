"""``cwcli status`` frontend: the ``overall`` token is the ONLY thing on stdout.

This pins the invariant the PR-1 E2E net asserts (``stdout.strip() == "running"``
/ ``"offline"``): the reseated human ``status`` prints the pre-computed aggregate
token and NOTHING else on stdout, while the per-process health detail and the web
probe go to stderr. It also confirms exit 0 across the lifecycle states (a stopped
project stays offline/exit-0) while a truly-nonexistent project exits non-zero.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import status as status_mod
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.status import StatusReport
from caffeinated_whale_cli.core.supervision import ProcessHealth
from caffeinated_whale_cli.utils import docker_utils


def _report(overall, processes=None):
    return StatusReport(
        overall=overall,
        project="proj",
        container_running=overall != "offline",
        supervisor_up=overall in ("running", "degraded"),
        web_http_code="200" if overall == "running" else None,
        processes=processes if processes is not None else [],
    )


def _run(monkeypatch, capsys, report):
    # Neutralize @handle_docker_errors' real docker CLI/daemon preflight (this
    # unit tier runs without Docker; see test_yes_flag.py's `_neutralize`).
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(
        status_mod.core_status, "status", lambda *a, **k: Result(status=Status.OK, data=report)
    )
    with pytest.raises(typer.Exit) as exc:
        status_mod.status(project_name="proj", bench=None, verbose=False, watch=False, interval=2.0)
    assert exc.value.exit_code == 0
    return capsys.readouterr()


@pytest.mark.parametrize("overall", ["offline", "online", "running", "degraded"])
def test_stdout_is_only_the_overall_token(overall, monkeypatch, capsys):
    captured = _run(monkeypatch, capsys, _report(overall))
    # The ENTIRE stdout, stripped, is exactly the aggregate token (the net's contract).
    assert captured.out.strip() == overall


def test_per_process_detail_goes_to_stderr_not_stdout(monkeypatch, capsys):
    procs = [
        ProcessHealth(label="web", up=True, pid=101, uptime_s=499, cpu_pct=0.5, rss_kb=80000),
        ProcessHealth(label="worker:default", up=False),
    ]
    captured = _run(monkeypatch, capsys, _report("running", procs))
    # stdout stays the single token; process detail is on stderr only.
    assert captured.out.strip() == "running"
    assert "web" in captured.err
    assert "pid=101" in captured.err
    assert "web" not in captured.out


def test_process_state_is_visible_on_stderr(monkeypatch, capsys):
    # The whole point of reading supervisorctl state: a BACKOFF/FATAL program must
    # be distinguishable from a clean down, not render identically as bare "down".
    procs = [
        ProcessHealth(label="worker:default", up=False, state="FATAL"),
        ProcessHealth(label="web", up=True, pid=101, state="RUNNING"),
    ]
    captured = _run(monkeypatch, capsys, _report("degraded", procs))
    assert "state=FATAL" in captured.err
    assert "state=RUNNING" in captured.err


def test_stopped_project_is_offline_exit_0(monkeypatch, capsys):
    # Regression guard: a real-but-stopped project stays offline/exit 0 (the token
    # on stdout), NOT a non-zero error. This is the contract the NOT_FOUND
    # distinction must never disturb.
    captured = _run(monkeypatch, capsys, _report("offline"))
    assert captured.out.strip() == "offline"


def test_nonexistent_project_exits_nonzero(monkeypatch, capsys):
    # A truly-nonexistent project raises NOT_FOUND in the core; the frontend maps
    # it to a non-zero exit with a clear message on stderr - distinct from offline.
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )

    def _raise(*a, **k):
        raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "No such project 'proj'.")

    monkeypatch.setattr(status_mod.core_status, "status", _raise)
    with pytest.raises(typer.Exit) as exc:
        status_mod.status(project_name="proj", bench=None, verbose=False, watch=False, interval=2.0)
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    # Nothing on stdout (no misleading "offline" token); the error is on stderr.
    assert captured.out.strip() == ""
    assert "No such project 'proj'." in captured.err
