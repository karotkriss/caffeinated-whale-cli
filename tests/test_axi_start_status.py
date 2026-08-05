"""``cwcli axi start`` / ``cwcli axi status`` verbs (task 5.3).

The core is stubbed, so these exercise the axi rendering / exit mapping / never-
prompt port-conflict pre-step in isolation: TOON on stdout, no progress text on
stdout, the exit codes, the needs-choice / CONFLICT flag-naming, and the
definitive offline state.
"""

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import start as start_mod
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.start import ProcessLaunch, StartOutcome
from caffeinated_whale_cli.core.status import BenchStatus, StatusReport
from caffeinated_whale_cli.core.stop import StopOutcome
from caffeinated_whale_cli.core.supervision import ProcessHealth

runner = CliRunner()


def _start_outcome(already_running=False):
    return StartOutcome(
        project="proj",
        container="proj-frappe-1",
        bench_path="/workspace/frappe-bench",
        supervisor="supervisord",
        log_path="/workspace/frappe-bench/logs",
        already_running=already_running,
        processes=[ProcessLaunch(label="web", pid=101 if already_running else None)],
    )


def _stop_result(project="other-proj"):
    return Result(
        status=Status.OK,
        data=StopOutcome(
            project=project, stopped=1, already_stopped=False, containers=[f"{project}-frappe-1"]
        ),
    )


def _bench_status(
    overall="running",
    processes=None,
    *,
    index=0,
    path="/w/b0",
    web_port=8000,
    not_cwcli_supervised=False,
    bench_present="present",
):
    return BenchStatus(
        index=index,
        bench_path=path,
        label=None,
        overall=overall,
        supervisor_up=overall in ("running", "degraded") and not not_cwcli_supervised,
        web_port=web_port,
        web_port_verified=True,
        web_site="site.localhost",
        web_http_code="200" if overall == "running" else None,
        processes=processes if processes is not None else [],
        not_cwcli_supervised=not_cwcli_supervised,
        bench_present=bench_present,
    )


def _status_report(overall="running", processes=None, *, not_cwcli_supervised=False, benches=None):
    if benches is None:
        benches = (
            []
            if overall == "offline"
            else [_bench_status(overall, processes, not_cwcli_supervised=not_cwcli_supervised)]
        )
    return StatusReport(
        overall=overall,
        project="proj",
        container_running=overall != "offline",
        benches=benches,
    )


def _no_conflicts(monkeypatch, running=True):
    """No port-conflict pre-step interference (container already up by default)."""
    monkeypatch.setattr(start_mod, "_frappe_running", lambda name: running)
    monkeypatch.setattr(start_mod, "detect_port_conflicts", lambda name: ([], []))


# --------------------------------------------------------------------------- axi start


class TestAxiStart:
    def test_success_emits_toon_exit_0(self, monkeypatch):
        _no_conflicts(monkeypatch)
        monkeypatch.setattr(
            axi_mod.core_start,
            "start",
            lambda *a, **k: Result(status=Status.OK, data=_start_outcome()),
        )
        result = runner.invoke(axi_mod.app, ["start", "proj", "--yes"])
        assert result.exit_code == 0
        assert "already_running: false" in result.stdout
        assert "supervisor: supervisord" in result.stdout
        assert "processes[1]{label,pid}:" in result.stdout
        # No progress/status text an agent could misread as data.
        assert "Starting" not in result.stdout
        assert "..." not in result.stdout

    def test_no_autorestart_flag_reaches_core_start(self, monkeypatch):
        # An agent driving cwcli via axi must be able to launch with self-heal off,
        # mirroring the human `cwcli start --no-autorestart` flag.
        _no_conflicts(monkeypatch)
        seen = {}

        def _fake_start(*a, **k):
            seen.update(k)
            return Result(status=Status.OK, data=_start_outcome())

        monkeypatch.setattr(axi_mod.core_start, "start", _fake_start)
        result = runner.invoke(axi_mod.app, ["start", "proj", "--no-autorestart"])
        assert result.exit_code == 0
        assert seen["autorestart"] is False

    def test_already_running_noop_is_emitted(self, monkeypatch):
        _no_conflicts(monkeypatch)
        monkeypatch.setattr(
            axi_mod.core_start,
            "start",
            lambda *a, **k: Result(status=Status.OK, data=_start_outcome(already_running=True)),
        )
        result = runner.invoke(axi_mod.app, ["start", "proj"])
        assert result.exit_code == 0
        assert "already_running: true" in result.stdout

    def test_multi_bench_names_the_flag_exit_2(self, monkeypatch):
        _no_conflicts(monkeypatch)
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'proj' has multiple benches; select one.",
            options=[{"value": "0", "label": "/w/b0"}, {"value": "1", "label": "/w/b1"}],
        )
        monkeypatch.setattr(
            axi_mod.core_start,
            "start",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["start", "proj"])
        assert result.exit_code == 2
        assert "--bench" in result.stdout
        assert "options[2]:" in result.stdout

    def test_port_conflict_names_yes_without_prompting_exit_1(self, monkeypatch):
        # Container not up + a Frappe project holds the ports + no --yes.
        monkeypatch.setattr(start_mod, "_frappe_running", lambda name: False)
        monkeypatch.setattr(start_mod, "detect_port_conflicts", lambda name: (["other-proj"], []))
        # core.start must NOT be reached.
        monkeypatch.setattr(
            axi_mod.core_start,
            "start",
            lambda *a, **k: pytest.fail("core.start reached on conflict"),
        )
        result = runner.invoke(axi_mod.app, ["start", "proj"])
        assert result.exit_code == 1
        assert "other-proj" in result.stdout
        assert "--yes" in result.stdout

    def test_yes_auto_resolves_frappe_conflict_then_starts(self, monkeypatch):
        monkeypatch.setattr(start_mod, "_frappe_running", lambda name: False)
        calls = {"n": 0}

        def _detect(name):
            calls["n"] += 1
            # First call finds the conflict; the post-stop recheck finds it clear.
            return (["other-proj"], []) if calls["n"] == 1 else ([], [])

        monkeypatch.setattr(start_mod, "detect_port_conflicts", _detect)
        stopped = []
        # axi calls core.stop DIRECTLY (never the CLI helper): a core callee cannot
        # print, so no branch of it can corrupt the one-TOON-document contract.
        monkeypatch.setattr(
            axi_mod.core_stop, "stop", lambda proj: stopped.append(proj) or _stop_result()
        )
        monkeypatch.setattr(
            axi_mod.core_start,
            "start",
            lambda *a, **k: Result(status=Status.OK, data=_start_outcome()),
        )
        result = runner.invoke(axi_mod.app, ["start", "proj", "--yes"])
        assert result.exit_code == 0
        assert stopped == ["other-proj"]
        assert "already_running: false" in result.stdout

    def test_residual_conflict_after_stop_is_reported_exit_1(self, monkeypatch):
        # The recheck still finds a conflict after stopping (teardown race or a
        # non-Frappe process grabbed the port) - must not fall through to core.start.
        monkeypatch.setattr(start_mod, "_frappe_running", lambda name: False)
        monkeypatch.setattr(start_mod, "detect_port_conflicts", lambda name: (["other-proj"], []))
        monkeypatch.setattr(axi_mod.core_stop, "stop", lambda proj: _stop_result())
        monkeypatch.setattr(
            axi_mod.core_start,
            "start",
            lambda *a, **k: pytest.fail("core.start reached on residual conflict"),
        )
        result = runner.invoke(axi_mod.app, ["start", "proj", "--yes"])
        assert result.exit_code == 1
        assert "still in use" in result.stdout

    def test_non_frappe_conflict_is_unresolvable_exit_1(self, monkeypatch):
        monkeypatch.setattr(start_mod, "_frappe_running", lambda name: False)
        monkeypatch.setattr(start_mod, "detect_port_conflicts", lambda name: ([], [8000]))
        result = runner.invoke(axi_mod.app, ["start", "proj", "--yes"])
        assert result.exit_code == 1
        assert "8000" in result.stdout
        assert "non-Frappe" in result.stdout

    def test_typed_error_renders_exit_1(self, monkeypatch):
        _no_conflicts(monkeypatch)

        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'proj' not found.")

        monkeypatch.setattr(axi_mod.core_start, "start", _raise)
        result = runner.invoke(axi_mod.app, ["start", "proj", "--yes"])
        assert result.exit_code == 1
        # The message carries single quotes (TOON-special), so toon.kv quotes it.
        assert "error: \"Project 'proj' not found.\"" in result.stdout


# -------------------------------------------------------------------------- axi status


class TestAxiStatus:
    def test_success_leads_with_overall_exit_0(self, monkeypatch):
        report = _status_report(
            overall="running",
            processes=[
                ProcessHealth(
                    label="web", up=True, pid=101, uptime_s=499, cpu_pct=0.5, rss_kb=80000
                )
            ],
        )
        monkeypatch.setattr(
            axi_mod.core_status, "status", lambda *a, **k: Result(status=Status.OK, data=report)
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 0
        # overall is the FIRST line of the TOON document.
        assert result.stdout.splitlines()[0] == "overall: running"
        assert "processes[1]{label,up,pid,uptime_s,cpu_pct,rss_kb,state}:" in result.stdout
        assert "..." not in result.stdout

    def test_offline_is_a_definitive_state(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_status,
            "status",
            lambda *a, **k: Result(status=Status.OK, data=_status_report(overall="offline")),
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "overall: offline"
        assert "container_running: false" in result.stdout

    def test_not_cwcli_supervised_flag_and_hint_in_toon(self, monkeypatch):
        # An agent driving `cwcli axi status` on a honcho instance must see the true
        # process state (up), the not_cwcli_supervised flag, and the hint - not a
        # false all-down.
        report = _status_report(
            overall="running",
            processes=[ProcessHealth(label="web", up=True, pid=201, uptime_s=499)],
            not_cwcli_supervised=True,
        )
        monkeypatch.setattr(
            axi_mod.core_status,
            "status",
            lambda *a, **k: Result(
                status=Status.OK,
                data=report,
                warnings=[
                    Message("supervisor.not_cwcli", "run `cwcli start` to bring it under...")
                ],
            ),
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "overall: running"
        assert "not_cwcli_supervised: true" in result.stdout
        assert "cwcli start" in result.stdout  # the hint rides in the warnings block

    def test_missing_host_port_warning_is_visible_in_toon(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_status,
            "status",
            lambda *a, **k: Result(
                status=Status.OK,
                data=_status_report(overall="running"),
                warnings=[
                    Message(
                        "status.no_host_port",
                        "'proj' is running, but container port 8000 is not reachable "
                        "from outside the container.",
                    )
                ],
            ),
        )

        result = runner.invoke(axi_mod.app, ["status", "proj"])

        assert result.exit_code == 0
        assert "container port 8000" in result.stdout
        assert "not reachable from outside the container" in result.stdout

    def test_multi_bench_reports_every_bench_exit_0(self, monkeypatch):
        # REPLACES test_multi_bench_names_the_flag_exit_2 (this class's copy only -
        # TestAxiStart's identically-named test pins `axi start`'s refusal, which this
        # change does not touch). The refusal was a signal to go read
        # `cwcli axi benches` and poll once per bench, reassembling the instance view
        # from documents that never said which bench they described. That enumeration
        # now arrives inline, in one document, each bench named.
        report = _status_report(
            overall="running",
            benches=[
                _bench_status("online", index=0, path="/w/b0"),
                _bench_status(
                    "running",
                    [ProcessHealth(label="web", up=True, pid=201)],
                    index=1,
                    path="/w/b1",
                    web_port=8001,
                ),
            ],
        )
        monkeypatch.setattr(
            axi_mod.core_status, "status", lambda *a, **k: Result(status=Status.OK, data=report)
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "overall: running"
        assert "benches[2]:" in result.stdout
        # Each bench names itself and the port its own code was measured on.
        assert "bench_path: /w/b0" in result.stdout
        assert "bench_path: /w/b1" in result.stdout
        assert "web_port: 8001" in result.stdout
        # Nested records survive as TOON, never a Python repr (the axi inspect rule).
        assert "{'" not in result.stdout

    def test_docker_error_renders_exit_1(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(
                ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker daemon."
            )

        monkeypatch.setattr(axi_mod.core_status, "status", _raise)
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 1
        assert "error: Could not connect to Docker daemon." in result.stdout


class TestAxiStatusReportsBenchExistence:
    """``bench_present`` on the agent surface.

    A deleted bench and a never-started bench are indistinguishable to the marker
    and to supervisord, so ``overall: online`` used to be the only thing an agent
    saw about a directory that no longer existed. Positive first: a live bench is
    still reported, and reported ``present``.
    """

    def test_a_live_bench_is_present_in_the_toon(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_status,
            "status",
            lambda *a, **k: Result(status=Status.OK, data=_status_report(overall="running")),
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])

        assert result.exit_code == 0
        assert "bench_present: present" in result.stdout

    def test_a_removed_bench_says_so_next_to_its_health(self, monkeypatch):
        report = _status_report(
            overall="online",
            benches=[_bench_status(overall="online", bench_present="absent")],
        )
        monkeypatch.setattr(
            axi_mod.core_status, "status", lambda *a, **k: Result(status=Status.OK, data=report)
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])

        assert "bench_present: absent" in result.stdout
        # The health half is unchanged and still there - the token qualifies the
        # row, it does not replace it or add a fifth `overall` token.
        assert "overall: online" in result.stdout
