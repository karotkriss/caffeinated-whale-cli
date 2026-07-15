"""``cwcli axi unlock`` / ``cwcli axi stop`` - TOON output, exit codes, no prompts.

Both verbs are thin serializers over the SAME core functions the human CLI calls,
so these pin the frontend contract only: one TOON document on stdout, the
status/error -> exit-code mapping, the structured `removed` list, and the
definitive already-stopped/already-unlocked success states.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.stop import StopOutcome
from caffeinated_whale_cli.core.unlock import UnlockOutcome

runner = CliRunner()

LOCKS = "/workspace/frappe-bench/sites/s.localhost/locks"


def _unlock_outcome(*, removed=None, already_unlocked=False):
    return UnlockOutcome(
        site="s.localhost",
        bench_path="/workspace/frappe-bench",
        locks_path=LOCKS,
        removed=removed if removed is not None else [f"{LOCKS}/doctype.lock", LOCKS],
        already_unlocked=already_unlocked,
    )


def _stop_outcome(*, stopped=2, already_stopped=False):
    return StopOutcome(
        project="proj",
        stopped=stopped,
        already_stopped=already_stopped,
        containers=["proj-frappe-1", "proj-db-1"] if stopped else [],
    )


class TestAxiUnlock:
    def test_emits_the_removed_paths_as_a_structured_list(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_unlock,
            "unlock",
            lambda *a, **k: Result(status=Status.OK, data=_unlock_outcome()),
        )
        result = runner.invoke(axi_mod.app, ["unlock", "proj", "--site", "s.localhost"])

        assert result.exit_code == 0
        # A structured block, not an opaque blob of command output.
        assert "removed[2]:" in result.stdout
        assert f"{LOCKS}/doctype.lock" in result.stdout

    def test_already_unlocked_is_a_definitive_success(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_unlock,
            "unlock",
            lambda *a, **k: Result(
                status=Status.OK, data=_unlock_outcome(removed=[], already_unlocked=True)
            ),
        )
        result = runner.invoke(axi_mod.app, ["unlock", "proj"])

        assert result.exit_code == 0
        assert "already_unlocked: true" in result.stdout

    def test_warnings_ride_along(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_unlock,
            "unlock",
            lambda *a, **k: Result(
                status=Status.OK,
                data=_unlock_outcome(),
                warnings=[Message("default_site.resolved", "Using default site: s.localhost")],
            ),
        )
        result = runner.invoke(axi_mod.app, ["unlock", "proj"])
        assert result.exit_code == 0
        assert "default site" in result.stdout

    def test_multi_bench_is_a_usage_error_naming_the_flag(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_unlock,
            "unlock",
            lambda *a, **k: Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="select_bench",
                    param="bench",
                    prompt="multiple benches",
                    options=[{"value": "0", "label": "/w/b0"}],
                ),
            ),
        )
        result = runner.invoke(axi_mod.app, ["unlock", "proj"])

        assert result.exit_code == 2
        assert "pass --bench" in result.stdout

    def test_stopped_container_is_an_error_and_never_prompts(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_unlock,
            "unlock",
            lambda *a, **k: Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_start",
                    param="auto_start",
                    prompt="Frappe container for project 'proj' is not running. Start it?",
                ),
            ),
        )
        result = runner.invoke(axi_mod.app, ["unlock", "proj"], input="y\n")

        assert result.exit_code == 2
        assert "not running" in result.stdout
        assert "cwcli start" in result.stdout  # names the escape hatch

    @pytest.mark.parametrize(
        "kind,expected",
        [(ErrorKind.NOT_FOUND, 1), (ErrorKind.USAGE, 2), (ErrorKind.PRECONDITION, 1)],
    )
    def test_error_kinds_map_to_exit_codes(self, monkeypatch, kind, expected):
        def _raise(*a, **k):
            raise CwcliError(kind, "some.code", "it broke")

        monkeypatch.setattr(axi_mod.core_unlock, "unlock", _raise)
        result = runner.invoke(axi_mod.app, ["unlock", "proj"])

        assert result.exit_code == expected
        assert "error: it broke" in result.stdout


class TestAxiStop:
    def test_emits_the_outcome_as_toon(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_stop, "stop", lambda p: Result(status=Status.OK, data=_stop_outcome())
        )
        result = runner.invoke(axi_mod.app, ["stop", "proj"])

        assert result.exit_code == 0
        assert "stopped: 2" in result.stdout
        assert "proj-frappe-1" in result.stdout

    def test_already_stopped_is_a_definitive_success_so_stop_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_stop,
            "stop",
            lambda p: Result(status=Status.OK, data=_stop_outcome(stopped=0, already_stopped=True)),
        )
        result = runner.invoke(axi_mod.app, ["stop", "proj"])

        assert result.exit_code == 0
        assert "already_stopped: true" in result.stdout

    def test_missing_project_is_a_structured_error_not_a_traceback(self, monkeypatch):
        def _raise(p):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'x' not found.")

        monkeypatch.setattr(axi_mod.core_stop, "stop", _raise)
        result = runner.invoke(axi_mod.app, ["stop", "x"])

        assert result.exit_code == 1
        # The apostrophes make this TOON-special, so `toon.kv` quotes the value -
        # that is what keeps the line parseable rather than mis-split.
        assert result.stdout == "error: \"Project 'x' not found.\"\n"
        assert "Traceback" not in result.stdout

    def test_daemon_down_is_a_structured_error(self, monkeypatch):
        def _raise(p):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect.")

        monkeypatch.setattr(axi_mod.core_stop, "stop", _raise)
        result = runner.invoke(axi_mod.app, ["stop", "proj"])

        assert result.exit_code == 1
        assert "error: Could not connect." in result.stdout
