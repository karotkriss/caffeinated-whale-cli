"""``cwcli axi restart`` verb: one-shot single-program restart as TOON.

The core is stubbed, so these exercise the axi rendering / exit mapping in
isolation: the ``ProcessRestartOutcome`` TOON on stdout, the unknown-process and
multi-bench needs-choice flag-naming usage errors, the required ``--process``, and
the typed-error mapping.
"""

from __future__ import annotations

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.restart import ProcessRestartOutcome

runner = CliRunner()


def _outcome():
    return ProcessRestartOutcome(
        project="proj",
        bench_path="/workspace/frappe-bench",
        label="web",
        old_pid=101,
        new_pid=202,
        supervisor_state="RUNNING",
    )


class TestAxiRestart:
    def test_success_emits_toon_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_restart,
            "restart_process",
            lambda *a, **k: Result(status=Status.OK, data=_outcome()),
        )
        result = runner.invoke(axi_mod.app, ["restart", "proj", "--process", "web"])
        assert result.exit_code == 0
        assert "label: web" in result.stdout
        assert "old_pid: 101" in result.stdout
        assert "new_pid: 202" in result.stdout
        assert "supervisor_state: RUNNING" in result.stdout
        assert "Restarting" not in result.stdout
        assert "..." not in result.stdout

    def test_unknown_process_names_the_flag_exit_2(self, monkeypatch):
        choice = Choice(
            kind="select_process",
            param="process",
            prompt="No process 'wroker' in bench '/w/b'. Select one of its programs.",
            options=[{"value": "web", "label": "web"}, {"value": "worker", "label": "worker"}],
        )
        monkeypatch.setattr(
            axi_mod.core_restart,
            "restart_process",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["restart", "proj", "--process", "wroker"])
        assert result.exit_code == 2
        assert "--process" in result.stdout
        assert "options[2]:" in result.stdout

    def test_multi_bench_names_bench_flag_exit_2(self, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'proj' has multiple benches; select one.",
            options=[{"value": "0", "label": "/w/b0"}, {"value": "1", "label": "/w/b1"}],
        )
        monkeypatch.setattr(
            axi_mod.core_restart,
            "restart_process",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["restart", "proj", "--process", "web"])
        assert result.exit_code == 2
        assert "--bench" in result.stdout

    def test_process_is_required_exit_2(self):
        # --process omitted -> a usage error (exit 2), never a whole-stack guess.
        result = runner.invoke(axi_mod.app, ["restart", "proj"])
        assert result.exit_code == 2

    def test_typed_error_renders_exit_1(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(
                ErrorKind.NOT_RUNNING,
                "supervisor.not_running",
                "Bench '/w/b' is not started (no supervisord running).",
            )

        monkeypatch.setattr(axi_mod.core_restart, "restart_process", _raise)
        result = runner.invoke(axi_mod.app, ["restart", "proj", "--process", "web"])
        assert result.exit_code == 1
        assert "not started" in result.stdout
