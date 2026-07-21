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


#: Captured at import, before any fixture patches it, so a test can put the real
#: implementation back and drive its Docker-error branch.
_REAL_LIVE_PROJECT_NAMES = core_where._live_project_names


@pytest.fixture(autouse=True)
def live_projects(monkeypatch):
    """Stub the live compose-project listing; tests mutate the set to drive staleness.

    Autouse so no unit test reaches a real Docker daemon. The default has both
    seeded projects live, which is what the pre-verification assertions assume.
    """
    live = {"proj-a", "proj-b"}
    monkeypatch.setattr(core_where, "_live_project_names", lambda: set(live))
    return live


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
        assert set(blob) == {"matches", "verified"}
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
                "project_state",
            }


class TestVerifiedVsRemembered:
    """A cached answer must never be presented as a verified one.

    The defect these pin: ``where`` served rows for a project that had been
    removed, with ``installed=true`` and nothing marking them as remembered, and
    served the identical rows while the Docker daemon was unreachable - the one
    moment it could have known cheaply that it was guessing.
    """

    def test_absent_project_is_never_reported_as_present(self, temp_db, live_projects):
        _seed()
        live_projects.discard("proj-b")  # proj-b removed since it was cached

        result = core_where.where("erpnext")

        assert result.data.verified is True
        states = {m.project: m.project_state for m in result.data.matches}
        assert states["proj-b"] == core_where.PROJECT_ABSENT
        assert states["proj-a"] == core_where.PROJECT_PRESENT
        # A stale hit is not a silent success: it degrades the envelope and names
        # the offending project, so a caller that only reads warnings still sees it.
        assert result.status is Status.WARNING
        assert [w.code for w in result.warnings] == ["where.stale_projects"]
        assert result.warnings[0].detail == {"projects": ["proj-b"]}

    def test_absent_row_is_reported_not_pruned(self, temp_db, live_projects):
        """A read command reports; it does not mutate the cache to hide the problem."""
        _seed()
        live_projects.discard("proj-b")

        result = core_where.where("erpnext")

        assert _by(result.data.matches, project="proj-b")

    def test_unreachable_daemon_degrades_to_unverified(self, temp_db, monkeypatch):
        _seed()
        monkeypatch.setattr(core_where, "_live_project_names", lambda: None)

        result = core_where.where("erpnext")

        assert result.data.verified is False
        assert result.data.matches, "an unreachable daemon must not silently drop matches"
        assert all(m.project_state == core_where.PROJECT_UNVERIFIED for m in result.data.matches)
        assert result.status is Status.WARNING
        assert [w.code for w in result.warnings] == ["where.unverified"]

    def test_unreachable_daemon_is_not_read_as_everything_absent(self, temp_db, monkeypatch):
        """``None`` must not degrade to an empty live set, which would read as all-gone."""
        _seed()
        monkeypatch.setattr(core_where, "_live_project_names", lambda: None)

        result = core_where.where("erpnext")

        assert not [m for m in result.data.matches if m.project_state == core_where.PROJECT_ABSENT]

    def test_docker_error_never_escapes_as_a_failure(self, temp_db, monkeypatch):
        """A search is still answerable without Docker - honestly, not fatally."""
        _seed()

        def _boom():
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "nope")

        monkeypatch.setattr(core_where, "list_instances", _boom)
        # Undo the autouse stub so the real _live_project_names runs against _boom.
        monkeypatch.setattr(core_where, "_live_project_names", _REAL_LIVE_PROJECT_NAMES)

        result = core_where.where("erpnext")

        assert result.data.verified is False
        assert all(m.project_state == core_where.PROJECT_UNVERIFIED for m in result.data.matches)

    def test_verify_false_skips_the_live_call_and_claims_nothing(self, temp_db, monkeypatch):
        """The performance escape hatch must not buy speed by pretending to verify."""

        def _must_not_run():
            raise AssertionError("verify=False must not touch Docker")

        monkeypatch.setattr(core_where, "_live_project_names", _must_not_run)
        _seed()

        result = core_where.where("erpnext", verify=False)

        assert result.data.verified is False
        assert all(m.project_state == core_where.PROJECT_UNVERIFIED for m in result.data.matches)
        # Opting out is not an anomaly, so it does not warn - but it also does not vouch.
        assert result.status is Status.OK
        assert result.warnings == []

    def test_default_is_verified(self, temp_db):
        """Verification is on by default; a caller gets the honest answer unasked."""
        _seed()

        result = core_where.where("erpnext")

        assert result.data.verified is True
        assert all(m.project_state == core_where.PROJECT_PRESENT for m in result.data.matches)
