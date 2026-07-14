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
from caffeinated_whale_cli.commands import stop as stop_mod
from caffeinated_whale_cli.core.envelope import Choice, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.start import ProcessLaunch, StartOutcome
from caffeinated_whale_cli.core.status import StatusReport
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


def _status_report(overall="running", processes=None):
    return StatusReport(
        overall=overall,
        project="proj",
        container_running=overall != "offline",
        supervisor_up=overall in ("running", "degraded"),
        web_http_code="200" if overall == "running" else None,
        processes=processes if processes is not None else [],
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
        monkeypatch.setattr(
            stop_mod, "_stop_project", lambda proj, verbose=False: stopped.append(proj)
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
        monkeypatch.setattr(stop_mod, "_stop_project", lambda proj, verbose=False: None)
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

    def test_multi_bench_names_the_flag_exit_2(self, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'proj' has multiple benches; select one.",
            options=[{"value": "0", "label": "/w/b0"}, {"value": "1", "label": "/w/b1"}],
        )
        monkeypatch.setattr(
            axi_mod.core_status,
            "status",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 2
        assert "--bench" in result.stdout

    def test_docker_error_renders_exit_1(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(
                ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker daemon."
            )

        monkeypatch.setattr(axi_mod.core_status, "status", _raise)
        result = runner.invoke(axi_mod.app, ["status", "proj"])
        assert result.exit_code == 1
        assert "error: Could not connect to Docker daemon." in result.stdout
