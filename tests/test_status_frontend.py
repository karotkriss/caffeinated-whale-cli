"""``cwcli status`` frontend: the ``overall`` token is the ONLY thing on stdout.

This pins the invariant the PR-1 E2E net asserts (``stdout.strip() == "running"``
/ ``"offline"``): the reseated human ``status`` prints the pre-computed aggregate
token and NOTHING else on stdout, while the per-process health detail and the web
probe go to stderr. It also confirms exit 0 across the lifecycle states.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import status as status_mod
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.status import StatusReport
from caffeinated_whale_cli.core.supervision import ProcessHealth


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
    monkeypatch.setattr(
        status_mod.core_status, "status", lambda *a, **k: Result(status=Status.OK, data=report)
    )
    with pytest.raises(typer.Exit) as exc:
        status_mod.status(project_name="proj", bench=None, verbose=False)
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
