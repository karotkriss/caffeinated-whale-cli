"""``cwcli ls`` human frontend: renderers consume the core DTO, and the queued
``--json`` empty-set fix emits a definitive ``[]``.

``ls`` is ``@handle_docker_errors``-decorated, so the ``wired`` fixture makes the
daemon look up (patching ``docker_utils.shutil.which`` + ``docker.from_env``);
the instance data itself comes from a patched ``core_list.list_instances``.
"""

import json

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import list as list_mod
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.utils import docker_utils

runner = CliRunner()


class _PingClient:
    def ping(self):
        return True


@pytest.fixture
def wired(monkeypatch):
    """Make @handle_docker_errors pass (docker present + daemon up)."""
    monkeypatch.setattr(docker_utils.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: _PingClient())


def _patch_instances(monkeypatch, instances):
    monkeypatch.setattr(
        list_mod.core_list, "list_instances", lambda **k: Result(status=Status.OK, data=instances)
    )


class TestLsJson:
    def test_json_empty_emits_bracket_pair(self, wired, monkeypatch):
        # The queued fix: an empty instance set is a definitive `[]`, not silence.
        _patch_instances(monkeypatch, [])
        result = runner.invoke(list_mod.app, ["--json"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "[]"

    def test_json_shape_preserved(self, wired, monkeypatch):
        _patch_instances(
            monkeypatch,
            [InstanceDTO(project_name="p1", status="running", ports=["8000", "8001"])],
        )
        result = runner.invoke(list_mod.app, ["--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert parsed == [{"projectName": "p1", "ports": ["8000", "8001"], "status": "running"}]


class TestLsOtherModes:
    def test_quiet_lists_names(self, wired, monkeypatch):
        _patch_instances(
            monkeypatch,
            [
                InstanceDTO(project_name="p1", status="running", ports=[]),
                InstanceDTO(project_name="p2", status="exited", ports=[]),
            ],
        )
        result = runner.invoke(list_mod.app, ["--quiet"])
        assert result.exit_code == 0
        assert result.stdout.split() == ["p1", "p2"]

    def test_quiet_empty_is_silent(self, wired, monkeypatch):
        _patch_instances(monkeypatch, [])
        result = runner.invoke(list_mod.app, ["--quiet"])
        assert result.exit_code == 0
        assert result.stdout.strip() == ""

    def test_table_condenses_port_ranges(self, wired, monkeypatch):
        _patch_instances(
            monkeypatch,
            [InstanceDTO(project_name="p1", status="running", ports=["8000", "8001", "8002"])],
        )
        result = runner.invoke(list_mod.app, [])
        assert result.exit_code == 0
        assert "8000-8002" in result.stdout

    def test_table_empty_prints_message(self, wired, monkeypatch):
        _patch_instances(monkeypatch, [])
        result = runner.invoke(list_mod.app, [])
        assert result.exit_code == 0
        assert "No Frappe instances found" in result.stdout
