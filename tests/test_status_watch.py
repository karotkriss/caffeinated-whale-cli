"""``cwcli status --watch`` frontend: the live view is quiet and TTY-aware.

The load-bearing behavior is that a repeated ``--watch`` tick re-polls with
``probe_web=False`` so it NEVER runs the ``curl localhost:8000`` web probe -
watching health must not spam the bench's access logs. Also pinned: the non-TTY
single-snapshot degrade (stdout OR stderr not a TTY - the live view renders to
stderr, so both streams must be interactive), the ``--interval`` 1s floor, and a
clean ``KeyboardInterrupt`` exit (exit 0, nothing on stdout).
"""

import pytest
import typer

from caffeinated_whale_cli.commands import status as status_mod
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.status import BenchStatus, StatusReport
from caffeinated_whale_cli.core.supervision import ProcessHealth
from caffeinated_whale_cli.utils import docker_utils


def _bench(overall="running", processes=None, *, index=0, path="/w/b0", web_port=8000):
    return BenchStatus(
        index=index,
        bench_path=path,
        label=None,
        overall=overall,
        supervisor_up=overall in ("running", "degraded"),
        web_port=web_port,
        web_port_verified=True,
        web_site="site.localhost",
        web_http_code=None,  # watch mode never carries a web code
        processes=(
            processes
            if processes is not None
            else [ProcessHealth(label="web", up=True, pid=101, uptime_s=499, rss_kb=80000)]
        ),
    )


def _report(overall="running", benches=None):
    return StatusReport(
        overall=overall,
        project="proj",
        container_running=overall != "offline",
        benches=[_bench(overall)] if benches is None else benches,
    )


class _FakeLive:
    """Stand-in for ``rich.live.Live`` (no terminal control in the unit tier)."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, *a, **k):
        pass


def _neutralize_docker(monkeypatch):
    # Defuse @handle_docker_errors' real docker CLI/daemon preflight (no Docker here).
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )


def _record_probe_web(monkeypatch):
    """Replace core.status with a recorder; return the list of probe_web values seen."""
    calls: list = []

    def _fake_status(*a, **k):
        calls.append(k.get("probe_web"))
        return Result(status=Status.OK, data=_report("running"))

    monkeypatch.setattr(status_mod.core_status, "status", _fake_status)
    return calls


def test_render_table_includes_state_column():
    # The --watch live table must surface supervisord's authoritative state (not
    # just up/down), matching the one-shot stderr detail.
    from rich.console import Console

    report = _report(
        "degraded",
        benches=[
            _bench("degraded", [ProcessHealth(label="worker:default", up=False, state="BACKOFF")])
        ],
    )
    table = status_mod._render_table(report)
    console = Console(width=120)
    with console.capture() as capture:
        console.print(table)
    rendered = capture.get()
    assert "state" in rendered
    assert "BACKOFF" in rendered


def test_watch_tty_never_runs_the_web_probe(monkeypatch, capsys):
    # THE load-bearing behavior: every watch tick re-polls with probe_web=False,
    # so the loop makes ZERO web requests against the bench.
    _neutralize_docker(monkeypatch)
    monkeypatch.setattr(status_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(status_mod.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(status_mod, "Live", _FakeLive)
    calls = _record_probe_web(monkeypatch)

    ticks = {"n": 0}

    def _sleep(_):
        ticks["n"] += 1
        if ticks["n"] >= 2:  # let one full re-poll happen, then break out
            raise KeyboardInterrupt

    monkeypatch.setattr(status_mod.time, "sleep", _sleep)

    with pytest.raises(typer.Exit) as exc:
        status_mod.status(project_name="proj", bench=None, verbose=False, watch=True, interval=2.0)
    assert exc.value.exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # watch writes NOTHING to stdout
    assert len(calls) >= 2  # the initial poll plus at least one re-poll tick
    assert all(pw is False for pw in calls)  # every poll suppressed the web probe


def test_watch_non_tty_degrades_to_single_quiet_snapshot(monkeypatch, capsys):
    # Non-TTY (piped/redirected): no live loop - one snapshot, still probe_web=False
    # (watch semantics stay quiet), and the token lands on stdout.
    _neutralize_docker(monkeypatch)
    monkeypatch.setattr(status_mod.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(status_mod.sys.stderr, "isatty", lambda: True)
    calls = _record_probe_web(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        status_mod.status(project_name="proj", bench=None, verbose=False, watch=True, interval=2.0)
    assert exc.value.exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "running"  # single snapshot token on stdout
    assert calls == [False]  # exactly one poll, web probe suppressed


def test_watch_redirected_stderr_degrades_to_single_quiet_snapshot(monkeypatch, capsys):
    # stdout is a TTY but stderr is redirected (e.g. `2>err.log`): the live view
    # renders to stderr, so this must also degrade rather than start a Live that
    # silently renders nothing.
    _neutralize_docker(monkeypatch)
    monkeypatch.setattr(status_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(status_mod.sys.stderr, "isatty", lambda: False)
    calls = _record_probe_web(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        status_mod.status(project_name="proj", bench=None, verbose=False, watch=True, interval=2.0)
    assert exc.value.exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "running"  # single snapshot token on stdout
    assert calls == [False]  # exactly one poll, web probe suppressed


@pytest.mark.parametrize("given,expected", [(0.0, 1.0), (0.5, 1.0), (2.0, 2.0), (5.0, 5.0)])
def test_watch_interval_floored_at_one_second(given, expected, monkeypatch):
    # --interval is floored at 1s so --interval 0 can't hammer the docker daemon.
    _neutralize_docker(monkeypatch)
    monkeypatch.setattr(status_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(status_mod.sys.stderr, "isatty", lambda: True)
    seen: list = []
    monkeypatch.setattr(
        status_mod, "_watch_loop", lambda project, bench, verbose, interval: seen.append(interval)
    )

    with pytest.raises(typer.Exit) as exc:
        status_mod.status(
            project_name="proj", bench=None, verbose=False, watch=True, interval=given
        )
    assert exc.value.exit_code == 0
    assert seen == [expected]


def test_the_bench_column_appears_only_when_more_than_one_bench_is_reported():
    # The single-bench live view is byte-identical to what it has always been; the
    # column is added only where it carries information.
    from rich.console import Console

    def _render(report):
        console = Console(width=140)
        with console.capture() as capture:
            console.print(status_mod._render_table(report))
        return capture.get()

    single = _render(_report("running"))
    assert "bench" not in single.split("\n")[1]

    multi = _render(
        _report(
            "running",
            benches=[
                _bench("online", index=0, path="/w/b0"),
                _bench("running", index=1, path="/w/b1", web_port=8001),
            ],
        )
    )
    assert "bench" in multi
    assert "0" in multi and "1" in multi
