"""``cwcli where`` human frontend over ``core.where``: table/JSON rendering, the
mutually-exclusive ``--apps``/``--sites`` exit code, and the definitive ``[]``
empty state. Drives the real cache DB (throwaway sqlite) end to end.
"""

import json

import pytest
import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import where as where_mod
from caffeinated_whale_cli.core import where as core_where
from caffeinated_whale_cli.utils import db_utils

runner = CliRunner()

app = typer.Typer()
app.command()(where_mod.where)


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    orig_path = db_utils.DB_PATH
    dbfile = tmp_path / "cache.db"
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    yield dbfile
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(orig_path))


@pytest.fixture(autouse=True)
def live_projects(monkeypatch):
    """Stub the live compose-project listing so no unit test reaches a real daemon."""
    live = {"proj-a"}
    monkeypatch.setattr(core_where, "_live_project_names", lambda: set(live))
    return live


def _seed():
    db_utils.cache_project_data(
        "proj-a",
        [
            {
                "path": "/workspace/frappe-bench",
                "available_apps": ["frappe", "erpnext"],
                "sites": [
                    {
                        "name": "erpnext.localhost",
                        "installed_apps": ["erpnext 15.0.0 version-15"],
                    }
                ],
            }
        ],
    )


class TestWhereJson:
    def test_json_empty_emits_bracket_pair(self, temp_db):
        _seed()
        result = runner.invoke(app, ["nomatch-zzz", "--json"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "[]"

    def test_json_shape_app_and_site_records(self, temp_db):
        _seed()
        result = runner.invoke(app, ["erpnext", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        app_rows = [r for r in parsed if r["type"] == "app"]
        site_rows = [r for r in parsed if r["type"] == "site"]
        # App records carry the full key set; site records stay lean (historical shape).
        assert set(app_rows[0]) == {
            "type",
            "project",
            "bench",
            "name",
            "version",
            "branch",
            "site",
            "installed",
            "project_state",
        }
        assert set(site_rows[0]) == {"type", "project", "bench", "name", "project_state"}
        assert app_rows[0]["installed"] is True
        assert app_rows[0]["project_state"] == "present"


class TestWhereStalenessIsVisibleToAHuman:
    """The human frontend must surface the same distinction the JSON carries."""

    def test_absent_instance_is_labelled_and_warned_about(self, temp_db, live_projects):
        _seed()
        live_projects.clear()  # proj-a removed since it was cached

        result = runner.invoke(app, ["erpnext"])

        assert result.exit_code == 0
        assert "absent" in result.stdout
        assert "no longer exist" in result.stdout

    def test_absent_instance_is_absent_in_json(self, temp_db, live_projects):
        _seed()
        live_projects.clear()

        result = runner.invoke(app, ["erpnext", "--json"])

        assert result.exit_code == 0
        assert {r["project_state"] for r in json.loads(result.stdout)} == {"absent"}

    def test_no_verify_claims_nothing(self, temp_db, monkeypatch):
        def _must_not_run():
            raise AssertionError("--no-verify must not touch Docker")

        monkeypatch.setattr(core_where, "_live_project_names", _must_not_run)
        _seed()

        result = runner.invoke(app, ["erpnext", "--json", "--no-verify"])

        assert result.exit_code == 0
        assert {r["project_state"] for r in json.loads(result.stdout)} == {"unverified"}


class TestWhereTableAndErrors:
    def test_apps_and_sites_mutually_exclusive_exit_1(self, temp_db):
        result = runner.invoke(app, ["erpnext", "--apps", "--sites"])
        assert result.exit_code == 1
        assert "Cannot use --apps and --sites together." in result.stdout

    def test_table_renders_and_summarizes(self, temp_db):
        _seed()
        result = runner.invoke(app, ["erpnext"])
        assert result.exit_code == 0
        assert "erpnext" in result.stdout
        assert "Found" in result.stdout and "match" in result.stdout

    def test_no_match_plain_message(self, temp_db):
        _seed()
        result = runner.invoke(app, ["nomatch-zzz"])
        assert result.exit_code == 0
        assert "No matches found for 'nomatch-zzz'." in result.stdout
