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
from caffeinated_whale_cli.core.status import BenchStatus, StatusReport
from caffeinated_whale_cli.core.supervision import ProcessHealth
from caffeinated_whale_cli.utils import docker_utils


def _bench(
    overall,
    processes=None,
    *,
    index=0,
    path="/w/b0",
    not_cwcli_supervised=False,
    web_port=8000,
    web_port_verified=True,
    web_site="site.localhost",
    bench_present="present",
):
    return BenchStatus(
        index=index,
        bench_path=path,
        label=None,
        overall=overall,
        supervisor_up=overall in ("running", "degraded") and not not_cwcli_supervised,
        web_port=web_port if web_port_verified else None,
        web_port_verified=web_port_verified,
        web_site=web_site if web_port_verified else None,
        web_http_code="200" if overall == "running" and web_port_verified else None,
        processes=processes if processes is not None else [],
        not_cwcli_supervised=not_cwcli_supervised,
        bench_present=bench_present,
    )


def _report(overall, processes=None, *, not_cwcli_supervised=False, benches=None):
    if benches is None:
        # An `offline` instance carries NO benches (nothing was probed).
        benches = (
            []
            if overall == "offline"
            else [_bench(overall, processes, not_cwcli_supervised=not_cwcli_supervised)]
        )
    return StatusReport(
        overall=overall,
        project="proj",
        container_running=overall != "offline",
        benches=benches,
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


def test_not_cwcli_supervised_hint_and_heading_on_stderr(monkeypatch, capsys):
    # A honcho / bench-start instance: the real overall token on stdout, the
    # not-cwcli-supervised heading + actionable hint on stderr (never "supervisor
    # down", which would be a lie while honcho serves).
    procs = [ProcessHealth(label="web", up=True, pid=201, uptime_s=499)]
    captured = _run(monkeypatch, capsys, _report("running", procs, not_cwcli_supervised=True))
    assert captured.out.strip() == "running"
    assert "not under cwcli supervision" in captured.err
    assert "cwcli start" in captured.err
    assert "supervisor down" not in captured.err


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


def test_multi_bench_stdout_is_still_exactly_one_token(monkeypatch, capsys):
    # The one-token contract is load-bearing and survives the restructure: a
    # multi-bench project prints the INSTANCE fold, and every per-bench detail goes
    # to stderr. There is no prompt on this path any more - the bare form used to
    # ask which bench.
    report = StatusReport(
        overall="running",
        project="proj",
        container_running=True,
        benches=[
            _bench("online", index=0, path="/w/b0"),
            _bench(
                "running",
                [ProcessHealth(label="web", up=True, pid=201)],
                index=1,
                path="/w/b1",
                web_port=8001,
            ),
        ],
    )
    captured = _run(monkeypatch, capsys, report)
    assert captured.out.strip() == "running"
    assert "/w/b0" in captured.err and "/w/b1" in captured.err


def test_the_web_line_names_the_port_and_site_it_probed(monkeypatch, capsys):
    # An unattributed "web http: 404" is what let one bench's code stand in for
    # another's, so the port is part of the answer - and so is the site, because
    # Frappe answers per Host and the code is that site's code.
    report = _report(
        "running",
        benches=[_bench("running", index=1, path="/w/b1", web_port=8001, web_site="two.localhost")],
    )
    captured = _run(monkeypatch, capsys, report)
    assert "web two.localhost:8001 -> 200" in captured.err


def test_the_web_line_still_names_the_port_when_no_site_is_known(monkeypatch, capsys):
    report = _report(
        "running", benches=[_bench("running", index=1, path="/w/b1", web_port=8001, web_site=None)]
    )
    captured = _run(monkeypatch, capsys, report)
    assert "web :8001 -> 200" in captured.err


def test_an_unknown_port_says_so_rather_than_implying_8000(monkeypatch, capsys):
    report = _report("running", benches=[_bench("running", path="/w/b1", web_port_verified=False)])
    captured = _run(monkeypatch, capsys, report)
    assert "port unknown" in captured.err
    assert "8000" not in captured.err


def test_a_live_bench_heading_carries_no_gone_marker(monkeypatch, capsys):
    # Positive first: the normal case stays quiet. Only a row that cannot be
    # trusted earns ink.
    captured = _run(monkeypatch, capsys, _report("running"))
    assert "GONE" not in captured.err
    assert "not verified" not in captured.err
    assert "running" in captured.err


def test_an_unverified_bench_heading_labels_cache_without_hiding_health(monkeypatch, capsys):
    report = _report("online", benches=[_bench("online", bench_present="unverified")])
    captured = _run(monkeypatch, capsys, report)

    assert "not verified" in captured.err
    assert "online" in captured.err
    assert "supervisor down" in captured.err
    assert captured.out.strip() == "online"


def test_a_removed_bench_heading_says_gone_before_its_health(monkeypatch, capsys):
    # `online` on a deleted bench reads as "here, just not up" - the exact wrong
    # conclusion, said confidently.
    report = _report("online", benches=[_bench("online", bench_present="absent")])
    captured = _run(monkeypatch, capsys, report)

    assert "GONE" in captured.err
    assert "inspect" in captured.err
    # stdout stays exactly one token, as ever.
    assert captured.out.strip() == "online"
