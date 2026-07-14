"""``core.where`` branch coverage against a throwaway SQLite cache.

Exercises the search/dedup/sort and the ``--apps``/``--sites`` USAGE conflict.
The core reads only the cache DB, prints/exits nothing, and returns
``Result(OK, WhereResult(...))`` or raises ``CwcliError``.
"""

import dataclasses

import peewee
import pytest

from caffeinated_whale_cli.core import where as core_where
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import db_utils


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point db_utils at a throwaway sqlite file, restoring the real one after."""
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


def _seed():
    # proj-a: erpnext available AND installed (dedup should prefer installed),
    # payments only available; site erpnext.localhost.
    db_utils.cache_project_data(
        "proj-a",
        [
            {
                "path": "/workspace/frappe-bench",
                "available_apps": ["frappe", "erpnext", "payments"],
                "sites": [
                    {
                        "name": "erpnext.localhost",
                        "installed_apps": [
                            "frappe 15.0.0 version-15",
                            "erpnext 15.0.0 version-15",
                        ],
                    }
                ],
            }
        ],
    )
    # proj-b: erpnext only available (not installed anywhere).
    db_utils.cache_project_data(
        "proj-b",
        [
            {
                "path": "/workspace/frappe-bench",
                "available_apps": ["erpnext"],
                "sites": [],
            }
        ],
    )


def _by(matches, **kw):
    return [m for m in matches if all(getattr(m, k) == v for k, v in kw.items())]


class TestWhere:
    def test_apps_and_sites_conflict_raises_usage(self, temp_db):
        with pytest.raises(CwcliError) as exc:
            core_where.where("x", apps_only=True, sites_only=True)
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "where.apps_sites_conflict"

    @pytest.mark.parametrize("helper", ["_search_apps", "_search_sites"])
    def test_peewee_error_becomes_typed_error(self, temp_db, monkeypatch, helper):
        """A raw peewee error (corrupt/locked cache) never leaks past the core boundary."""

        def _boom(*a, **k):
            raise peewee.OperationalError("database is locked")

        monkeypatch.setattr(core_where, helper, _boom)
        with pytest.raises(CwcliError) as exc:
            core_where.where("x")
        assert exc.value.kind is ErrorKind.INTERNAL
        assert exc.value.code == "cache.read_failed"

    def test_matches_apps_and_sites_sorted(self, temp_db):
        _seed()
        result = core_where.where("erpnext")
        assert result.status is Status.OK
        matches = result.data.matches
        # Sorted by (project, type, name): proj-a app, proj-a site, proj-b app.
        assert [(m.project, m.type, m.name) for m in matches] == [
            ("proj-a", "app", "erpnext"),
            ("proj-a", "site", "erpnext.localhost"),
            ("proj-b", "app", "erpnext"),
        ]

    def test_installed_preferred_over_available_dedup(self, temp_db):
        _seed()
        matches = core_where.where("erpnext", apps_only=True).data.matches
        proj_a = _by(matches, project="proj-a", type="app")
        # Only ONE proj-a erpnext row, and it is the installed one (version/branch set).
        assert len(proj_a) == 1
        assert proj_a[0].installed is True
        assert proj_a[0].version == "15.0.0"
        assert proj_a[0].branch == "version-15"
        assert proj_a[0].site == "erpnext.localhost"

    def test_available_only_app_survives(self, temp_db):
        _seed()
        matches = core_where.where("payments", apps_only=True).data.matches
        assert len(matches) == 1
        assert matches[0].name == "payments"
        assert matches[0].installed is False

    def test_installed_only_filters_available(self, temp_db):
        _seed()
        # payments is available but never installed -> excluded under installed_only.
        matches = core_where.where("payments", installed_only=True).data.matches
        assert matches == []

    def test_sites_only_scopes_out_apps(self, temp_db):
        _seed()
        matches = core_where.where("erpnext", sites_only=True).data.matches
        assert matches and all(m.type == "site" for m in matches)

    def test_apps_only_scopes_out_sites(self, temp_db):
        _seed()
        matches = core_where.where("erpnext", apps_only=True).data.matches
        assert matches and all(m.type == "app" for m in matches)

    def test_result_dto_is_json_safe(self, temp_db):
        _seed()
        result = core_where.where("erpnext")
        blob = dataclasses.asdict(result.data)
        assert set(blob) == {"matches"}
        for row in blob["matches"]:
            assert set(row) == {
                "type",
                "project",
                "bench",
                "name",
                "version",
                "branch",
                "site",
                "installed",
            }
