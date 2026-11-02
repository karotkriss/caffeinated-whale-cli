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
from caffeinated_whale_cli.core.envelope import Message, Result, Status
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


class _FakeContainer:
    """A frappe container whose live port bindings publish each container port P at
    host port P+10000 - so the human web line can resolve a HOST-reachable address
    (BUG-5). ``get_frappe_container=None`` (below) simulates the unresolvable case.
    """

    ports = {
        "8000/tcp": [{"HostPort": "18000"}],
        "8001/tcp": [{"HostPort": "18001"}],
    }


def _run(monkeypatch, capsys, report, *, container=_FakeContainer, warnings=()):
    # Neutralize @handle_docker_errors' real docker CLI/daemon preflight (this
    # unit tier runs without Docker; see test_yes_flag.py's `_neutralize`).
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(
        status_mod.core_status,
        "status",
        lambda *a, **k: Result(status=Status.OK, data=report, warnings=list(warnings)),
    )
    # The human web line resolves the HOST address from the container's live
    # bindings; ``container=None`` makes that unresolvable (the in-container fallback).
    monkeypatch.setattr(
        status_mod.core_docker,
        "get_frappe_container",
        lambda name: None if container is None else container(),
    )
    with pytest.raises(typer.Exit) as exc:
        status_mod.status(
            project_name="proj",
            bench=None,
            path=None,
            verbose=False,
            watch=False,
            interval=2.0,
            clear_maintenance=False,
        )
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
        status_mod.status(
            project_name="proj",
            bench=None,
            path=None,
            verbose=False,
            watch=False,
            interval=2.0,
            clear_maintenance=False,
        )
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    # Nothing on stdout (no misleading "offline" token); the error is on stderr.
    assert captured.out.strip() == ""
    assert "No such project 'proj'." in captured.err


def test_bare_status_prints_the_all_instances_summary(monkeypatch, capsys):
    """Bare `cwcli status` (GH #231) reads like "how is everything" - it prints
    the same all-instances summary `cwcli ls` gives, exit 0, never a missing-
    argument usage error."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    calls = []
    monkeypatch.setattr(status_mod, "render_instances_summary", lambda **k: calls.append(k) or None)

    def _boom(*a, **k):
        raise AssertionError("bare status must not reach core.status at all")

    monkeypatch.setattr(status_mod.core_status, "status", _boom)

    with pytest.raises(typer.Exit) as exc:
        status_mod.status(
            project_name=None,
            bench=None,
            path=None,
            verbose=False,
            watch=False,
            interval=2.0,
            clear_maintenance=False,
        )
    assert exc.value.exit_code == 0
    assert calls == [{"verbose": False}]


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


def test_the_web_line_names_the_host_address_and_site_it_probed(monkeypatch, capsys):
    # An unattributed "web http: 404" is what let one bench's code stand in for
    # another's, so the port is part of the answer - and so is the site, because
    # Frappe answers per Host and the code is that site's code. The address shown is
    # the HOST-published one a person can open (container 8001 -> host 18001), not the
    # container-internal port (BUG-5).
    report = _report(
        "running",
        benches=[_bench("running", index=1, path="/w/b1", web_port=8001, web_site="two.localhost")],
    )
    captured = _run(monkeypatch, capsys, report)
    assert "web two.localhost:18001 -> 200" in captured.err
    # The unreachable container port is never presented as the address.
    assert "two.localhost:8001 ->" not in captured.err


def test_the_web_line_still_names_the_host_address_when_no_site_is_known(monkeypatch, capsys):
    report = _report(
        "running", benches=[_bench("running", index=1, path="/w/b1", web_port=8001, web_site=None)]
    )
    captured = _run(monkeypatch, capsys, report)
    assert "web localhost:18001 -> 200" in captured.err


def test_the_web_line_labels_the_container_port_when_the_host_binding_is_unresolvable(
    monkeypatch, capsys
):
    # When the host binding cannot be resolved (no container, or no published port),
    # the container port is shown but labelled "(in-container)" so nobody tries to
    # open a port that is unreachable from the host (BUG-5).
    report = _report(
        "running",
        benches=[_bench("running", index=1, path="/w/b1", web_port=8001, web_site="two.localhost")],
    )
    captured = _run(monkeypatch, capsys, report, container=None)
    assert "web two.localhost:8001 (in-container) -> 200" in captured.err


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


def test_maintenance_mode_is_named_plainly_with_the_way_out(monkeypatch, capsys):
    # BUG-11: a site stuck in maintenance answers 503 with no other signal, so the
    # human surface says so plainly (even without --verbose) and names the remedy.
    import dataclasses

    bench = dataclasses.replace(_bench("degraded"), maintenance_mode=True)
    report = _report("degraded", benches=[bench])
    # The renderer prints the core's own warning under the bench it names, so the
    # remedy is written in exactly one place.
    warning = Message(
        "status.maintenance_mode",
        "MAINTENANCE MODE ON for site 'site.localhost' (it answers 503 to users). "
        "Take it out with: cwcli run proj --site site.localhost set-maintenance-mode off",
        detail={"bench_path": bench.bench_path},
    )
    captured = _run(monkeypatch, capsys, report, warnings=[warning])
    err = harness_collapse(captured.err)

    assert "MAINTENANCE MODE ON" in err
    assert "site.localhost" in err
    assert "set-maintenance-mode off" in err
    # stdout stays exactly one machine token.
    assert captured.out.strip() == "degraded"


def test_a_bench_left_down_by_bench_update_is_named_with_the_bench_wide_way_out(
    monkeypatch, capsys
):
    # The REAL core produces the warnings: a failed `bench update` left both flags in
    # the bench's common config. Both faults print under the bench heading even
    # without --verbose, and neither is echoed twice.
    from tests.test_core_status import wire_bench_left_down_by_bench_update

    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    container = wire_bench_left_down_by_bench_update(monkeypatch)
    monkeypatch.setattr(status_mod.core_docker, "get_frappe_container", lambda name: container)

    for verbose in (False, True):
        with pytest.raises(typer.Exit):
            status_mod.status(
                project_name="proj",
                bench=None,
                path=None,
                verbose=verbose,
                watch=False,
                interval=2.0,
                clear_maintenance=False,
            )
        err = harness_collapse(capsys.readouterr().err)

        assert "maintenance_mode is set bench-wide" in err
        assert "SCHEDULER PAUSED bench-wide" in err
        assert "resumes it in the same step" in err
        # One way out, named once; a non-TTY is never asked.
        assert err.count("cwcli status proj --clear-maintenance") == 1
        assert "set-maintenance-mode off" not in err
        assert "Out of maintenance mode" not in err


def harness_collapse(text: str) -> str:
    """Rich wraps long lines at the console width; compare on collapsed whitespace."""
    return " ".join(text.split())


# --------------------------------------------- the maintenance-mode question


from dataclasses import replace  # noqa: E402

from caffeinated_whale_cli.core import maintenance as core_maintenance  # noqa: E402


def _stuck(*, busy=(), owners=()):
    return core_maintenance.BenchMaintenance(
        bench_path="/w/b0",
        bench_flags=["maintenance_mode", "pause_scheduler"],
        sites=[core_maintenance.SiteMaintenance(site="site.example.com", on=True, cause="bench")],
        down=["site.example.com"],
        owners=list(owners),
        busy=list(busy),
        command="cwcli status proj --clear-maintenance",
    )


class _Asked:
    """Stands in for questionary.confirm and records what was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.asked: list = []

    def __call__(self, message, default=False, auto_enter=True):
        self.asked.append((message, default))
        return type("Q", (), {"ask": lambda _s: self.answer})()


def _maintenance_run(
    monkeypatch,
    capsys,
    maintenance,
    *,
    tty,
    answer=None,
    flag=False,
    clear_ok=True,
    report=None,
    unanswered=False,
):
    if report is None:
        report = _report("running", benches=[replace(_bench("running"), maintenance=maintenance)])
    fetched: list = []
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(
        status_mod.core_status,
        "status",
        lambda *a, **k: fetched.append(k) or Result(status=Status.OK, data=report),
    )
    monkeypatch.setattr(status_mod.core_docker, "get_frappe_container", lambda name: None)
    monkeypatch.setattr(status_mod, "_interactive", lambda: tty)
    asked = _Asked(answer)
    monkeypatch.setattr(status_mod.questionary, "confirm", asked)
    cleared: list = []

    def clear(project, *, path=None, consent=False, on_event=None, **_):
        cleared.append((project, path, consent))
        on_event(core_maintenance.MaintenanceStepEnd(phase="check", status="ok", message="x"))
        outcome = core_maintenance.MaintenanceClearReport(
            ok=clear_ok,
            project=project,
            bench_path="/w/b0",
            sites=["site.example.com"],
            answering=["site.example.com"] if clear_ok and not unanswered else [],
            still_down=[] if clear_ok else ["site.example.com"],
            verified=not unanswered,
        )
        return Result(status=Status.OK, data=outcome)

    monkeypatch.setattr(status_mod.core_maintenance, "clear", clear)
    with pytest.raises(typer.Exit) as exc:
        status_mod.status(
            project_name="proj",
            bench=None,
            path=None,
            verbose=False,
            watch=False,
            interval=2.0,
            clear_maintenance=flag,
        )
    out = capsys.readouterr()
    _maintenance_run.fetched = fetched
    return exc.value.exit_code, out, asked.asked, cleared


class TestMaintenanceQuestion:
    def test_a_terminal_is_asked_and_yes_takes_the_sites_out(self, monkeypatch, capsys):
        code, out, asked, cleared = _maintenance_run(
            monkeypatch, capsys, _stuck(), tty=True, answer=True
        )

        assert asked == [
            ("Take site.example.com out of maintenance mode and resume the scheduler?", False)
        ]
        assert cleared == [("proj", "/w/b0", True)]
        assert "Out of maintenance mode: site.example.com" in out.err
        assert out.out.strip() == "running"  # the token stays alone on stdout
        assert code == 0

    def test_no_leaves_it_and_names_the_command(self, monkeypatch, capsys):
        code, out, asked, cleared = _maintenance_run(
            monkeypatch, capsys, _stuck(), tty=True, answer=False
        )

        assert asked and cleared == []
        assert "cwcli status proj --clear-maintenance" in harness_collapse(out.err)
        assert code == 0

    def test_a_known_leftover_defaults_to_yes(self, monkeypatch, capsys):
        owner = core_maintenance.MaintenanceOwner(
            site="site.example.com", verb="apps update", running=False
        )
        _, _, asked, _ = _maintenance_run(
            monkeypatch, capsys, _stuck(owners=[owner]), tty=True, answer=False
        )

        assert asked[0][1] is True

    def test_a_non_tty_is_never_asked(self, monkeypatch, capsys):
        code, out, asked, cleared = _maintenance_run(monkeypatch, capsys, _stuck(), tty=False)

        assert asked == [] and cleared == []
        assert code == 0

    def test_the_flag_clears_without_asking(self, monkeypatch, capsys):
        code, out, asked, cleared = _maintenance_run(
            monkeypatch, capsys, _stuck(), tty=False, flag=True
        )

        assert asked == [] and cleared == [("proj", "/w/b0", True)]
        assert code == 0

    def test_a_site_that_did_not_answer_is_not_called_out_of_maintenance(self, monkeypatch, capsys):
        code, out, _, _ = _maintenance_run(
            monkeypatch, capsys, _stuck(), tty=False, flag=True, unanswered=True
        )

        assert code == 0
        assert "Out of maintenance mode" not in out.err
        assert "Flags cleared, not checked: site.example.com" in out.err

    def test_the_flag_exits_1_when_a_site_still_does_not_answer(self, monkeypatch, capsys):
        code, out, _, _ = _maintenance_run(
            monkeypatch, capsys, _stuck(), tty=False, flag=True, clear_ok=False
        )

        assert code == 1
        assert "Still in maintenance mode: site.example.com" in out.err

    def test_the_flag_reads_maintenance_and_refuses_a_bench_it_could_not_read(
        self, monkeypatch, capsys
    ):
        code, out, _, cleared = _maintenance_run(monkeypatch, capsys, None, tty=False, flag=True)

        assert _maintenance_run.fetched[0]["read_maintenance"] is True
        assert cleared == [] and code == 1
        assert "could not read maintenance mode on /w/b0" in harness_collapse(out.err)
        assert "No site is in maintenance mode" not in out.err

    def test_the_flag_refuses_a_stopped_instance(self, monkeypatch, capsys):
        code, out, _, cleared = _maintenance_run(
            monkeypatch, capsys, None, tty=False, flag=True, report=_report("offline")
        )

        assert cleared == [] and code == 1
        assert "could not read maintenance mode on proj because the container is not running" in (
            harness_collapse(out.err)
        )

    def test_busy_is_never_offered_and_the_flag_is_refused(self, monkeypatch, capsys):
        busy = ["an in-container 'bench migrate' is still running (pid 5)"]
        code, out, asked, cleared = _maintenance_run(
            monkeypatch, capsys, _stuck(busy=busy), tty=True, answer=True
        )
        assert asked == [] and cleared == [] and code == 0

        code, out, asked, cleared = _maintenance_run(
            monkeypatch, capsys, _stuck(busy=busy), tty=False, flag=True
        )
        assert cleared == [] and code == 1
        assert "Not cleared: an in-container 'bench migrate' is still running" in harness_collapse(
            out.err
        )
