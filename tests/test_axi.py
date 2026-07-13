"""``cwcli axi`` surface: TOON serializer, verb exit-mapping, content-first home.

Covers task 4.5. The core is stubbed (so these exercise the axi rendering / exit
mapping in isolation): TOON on stdout for success, no progress text on stdout,
typed-error rendering + exit codes, needs-choice -> flag-naming usage error exit
2, and the ls-DTO home populated + empty. Plus the shared TOON encoder's shape.
"""

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.backup import BackupOutcome
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.where import WhereMatch, WhereResult
from caffeinated_whale_cli.utils import toon

runner = CliRunner()


def _outcome():
    return BackupOutcome(
        site="s.localhost",
        bench_path="/workspace/frappe-bench",
        artifact_path="/workspace/frappe-bench/sites/s.localhost/private/backups/x.sql.gz",
        included_files=False,
    )


def _instances_ok(instances):
    """Patch the ls core producer axi consumes to return a fixed instance list."""
    return lambda **k: Result(status=Status.OK, data=instances)


# ---------------------------------------------------------------------------- axi backup


class TestAxiBackup:
    def test_success_emits_toon_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_backup, "backup", lambda *a, **k: Result(status=Status.OK, data=_outcome())
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj", "--site", "s.localhost"])
        assert result.exit_code == 0
        assert "site: s.localhost" in result.stdout
        assert "included_files: false" in result.stdout
        # No progress/status line an agent could misread as data.
        assert "Creating backup" not in result.stdout
        assert "..." not in result.stdout

    def test_warnings_render_in_toon(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_backup,
            "backup",
            lambda *a, **k: Result(
                status=Status.OK,
                data=_outcome(),
                warnings=[Message("default_site.resolved", "Using default site: s.localhost")],
            ),
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 0
        assert "warnings[1]:" in result.stdout
        assert "Using default site: s.localhost" in result.stdout

    def test_typed_error_renders_on_stdout_exit_1(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'proj' not found.")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 1
        assert result.stdout.startswith("error: Project 'proj' not found.")
        assert "Traceback" not in result.stdout

    def test_usage_error_exit_2(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(ErrorKind.USAGE, "site.invalid_chars", "Invalid site name 'a;b'.")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj", "--site", "a;b"])
        assert result.exit_code == 2
        assert "error: Invalid site name" in result.stdout

    def test_error_hint_rendered(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_RUNNING, "x", "not running", hint="cwcli start proj")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 1
        assert "help: cwcli start proj" in result.stdout

    def test_needs_choice_select_bench_is_usage_error_exit_2(self, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="pick",
            options=[{"value": "0", "label": "/w/b0"}, {"value": "1", "label": "/w/b1"}],
        )
        monkeypatch.setattr(
            axi_mod.core_backup,
            "backup",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 2
        assert "--bench <index|label>" in result.stdout
        # The benches ride in a proper TOON options[N]: block (uniform TOON, not
        # bare indented lines).
        assert "options[2]:" in result.stdout
        assert "[0] /w/b0" in result.stdout
        assert "[1] /w/b1" in result.stdout

    def test_needs_choice_confirm_start_is_usage_error_exit_2(self, monkeypatch):
        choice = Choice(kind="confirm_start", param="auto_start", prompt="not running. Start it?")
        monkeypatch.setattr(
            axi_mod.core_backup,
            "backup",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 2
        assert "error:" in result.stdout


# ------------------------------------------------------------------------------ axi home


class TestAxiHome:
    def test_home_shows_instances(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_list,
            "list_instances",
            _instances_ok(
                [
                    InstanceDTO(project_name="proj-a", status="running", ports=["8000", "8001"]),
                    InstanceDTO(project_name="proj-b", status="exited", ports=[]),
                ]
            ),
        )
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 0
        assert result.stdout.startswith("bin: ")
        assert "description: " in result.stdout
        assert "instances[2]{projectName,status,ports}:" in result.stdout
        assert "proj-a,running,8000 8001" in result.stdout
        assert "proj-b,exited,N/A" in result.stdout
        assert "help[3]:" in result.stdout

    def test_home_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 0
        assert "instances: 0 Frappe instances found" in result.stdout
        assert "help[" in result.stdout

    def test_home_docker_error_is_structured_stdout(self, monkeypatch):
        def _raise(**k):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker.")

        monkeypatch.setattr(axi_mod.core_list, "list_instances", _raise)
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 1
        assert result.stdout.startswith("error: Could not connect to Docker.")
        assert "Traceback" not in result.stdout


# -------------------------------------------------------------------------------- axi ls


class TestAxiLs:
    def test_ls_emits_toon_table_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_list,
            "list_instances",
            _instances_ok(
                [
                    InstanceDTO(project_name="proj-a", status="running", ports=["8000", "8001"]),
                    InstanceDTO(project_name="proj-b", status="exited", ports=[]),
                ]
            ),
        )
        result = runner.invoke(axi_mod.app, ["ls"])
        assert result.exit_code == 0
        # Focused verb: JUST the instances block, no bin/description/help wrapper.
        assert result.stdout.startswith("instances[2]{projectName,status,ports}:")
        assert "proj-a,running,8000 8001" in result.stdout
        assert "proj-b,exited,N/A" in result.stdout
        assert "bin:" not in result.stdout

    def test_ls_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        result = runner.invoke(axi_mod.app, ["ls"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "instances: 0 Frappe instances found"

    def test_ls_never_emits_json(self, monkeypatch):
        # axi is TOON-only; there is no --json/-j flag on the verb.
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        assert runner.invoke(axi_mod.app, ["ls", "--json"]).exit_code == 2
        assert runner.invoke(axi_mod.app, ["ls", "-j"]).exit_code == 2

    def test_ls_docker_error_exit_1(self, monkeypatch):
        def _raise(**k):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker.")

        monkeypatch.setattr(axi_mod.core_list, "list_instances", _raise)
        result = runner.invoke(axi_mod.app, ["ls"])
        assert result.exit_code == 1
        assert result.stdout.startswith("error: Could not connect to Docker.")


# ----------------------------------------------------------------------------- axi where


class TestAxiWhere:
    def test_where_emits_toon_matches_exit_0(self, monkeypatch):
        matches = [
            WhereMatch(
                type="app",
                project="proj-a",
                bench="/w/b",
                name="erpnext",
                version="15.0.0",
                branch="version-15",
                site="s.localhost",
                installed=True,
            ),
            WhereMatch(type="site", project="proj-a", bench="/w/b", name="s.localhost"),
        ]
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(status=Status.OK, data=WhereResult(matches=matches)),
        )
        result = runner.invoke(axi_mod.app, ["where", "erp"])
        assert result.exit_code == 0
        assert result.stdout.startswith(
            "matches[2]{type,project,bench,name,version,branch,site,installed}:"
        )
        assert "app,proj-a,/w/b,erpnext,15.0.0,version-15,s.localhost,true" in result.stdout
        assert "site,proj-a,/w/b,s.localhost,null,null,null,false" in result.stdout

    def test_where_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(status=Status.OK, data=WhereResult(matches=[])),
        )
        result = runner.invoke(axi_mod.app, ["where", "zzz"])
        assert result.exit_code == 0
        # An empty typed collection is still a definitive TOON empty state.
        assert result.stdout.strip() == "matches[0]:"

    def test_where_usage_error_exit_2(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(
                ErrorKind.USAGE,
                "where.apps_sites_conflict",
                "Cannot use --apps and --sites together.",
            )

        monkeypatch.setattr(axi_mod.core_where, "where", _raise)
        result = runner.invoke(axi_mod.app, ["where", "x", "--apps", "--sites"])
        assert result.exit_code == 2
        assert "error: Cannot use --apps and --sites together." in result.stdout


# ---------------------------------------------------------------------------- toon encoder


class TestToonEncoder:
    def test_flat_object(self):
        doc = toon.encode({"a": "x", "b": True, "n": None})
        assert doc == "a: x\nb: true\nn: null"

    def test_numeric_string_quoted(self):
        # so an agent doesn't read the string "42" as the number 42
        assert toon.encode({"id": "42"}) == 'id: "42"'
        assert toon.encode({"id": 42}) == "id: 42"

    def test_table_shape(self):
        rows = [{"id": "a", "n": 1}, {"id": "b", "n": 2}]
        out = toon.table("rows", rows, ["id", "n"])
        assert out.splitlines()[0] == "rows[2]{id,n}:"
        assert out.splitlines()[1] == "  a,1"

    def test_value_with_comma_is_quoted(self):
        assert toon.encode({"k": "a,b"}) == 'k: "a,b"'

    def test_block_one_item_per_line(self):
        out = toon.block("help", ["do x", "do y"])
        assert out == "help[2]:\n  do x\n  do y"

    def test_self_check_runs(self):
        toon._self_check()  # asserts internally; must not raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
