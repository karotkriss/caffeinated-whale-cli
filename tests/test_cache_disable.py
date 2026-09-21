"""Tests for the "disable caching entirely" switch.

Covers the config key + ``CWCLI_NO_CACHE`` env override and their precedence, the
``config show`` / ``config cache`` surfaces, and the behavioural guarantees: a
disabled read never serves a stale on-disk row, resolves live instead, and leaves
the on-disk cache file untouched. The live-resolution path is driven by a monkey-
patched ``core.inspect.inspect`` so these stay in the fast (no-Docker) tier; the
real container populate is proven in the E2E leg.
"""

import pytest

from caffeinated_whale_cli.core import config as core_config
from caffeinated_whale_cli.utils import cache, config_utils, db_utils

BENCH = "/workspace/frappe-bench"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated CWCLI_HOME with the peewee db re-pointed at a throwaway file.

    The env var is deleted so each test controls CWCLI_NO_CACHE itself, and the
    live-populate memo/reentrancy globals are reset so tests never bleed.
    """
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path))
    monkeypatch.delenv("CWCLI_NO_CACHE", raising=False)
    dbfile = tmp_path / "cache" / "cwc-cache.db"
    dbfile.parent.mkdir(parents=True, exist_ok=True)
    orig_path = db_utils.DB_PATH
    monkeypatch.setattr(db_utils, "DB_PATH", dbfile)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", tmp_path / "config" / "config.toml")
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    db_utils._live_populated.clear()
    db_utils._populating = False
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(dbfile))
    db_utils.initialize_database()
    yield tmp_path
    if not db_utils.db.is_closed():
        db_utils.db.close()
    db_utils.db.init(str(orig_path))
    db_utils._live_populated.clear()
    db_utils._populating = False


def _seed_disk_cache():
    """Write one project row to the (enabled) on-disk cache and return its mtime."""
    db_utils.cache_project_data(
        "proj",
        [
            {
                "path": BENCH,
                "sites": [{"name": "stale.site", "installed_apps": []}],
                "available_apps": ["frappe", "stale_app"],
            }
        ],
    )
    return db_utils.DB_PATH.stat().st_mtime_ns


# --------------------------------------------------------------- the config gate


class TestCacheDisabledGate:
    def test_default_is_enabled(self, home):
        assert config_utils.cache_disabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_env_truthy_disables(self, home, monkeypatch, value):
        monkeypatch.setenv("CWCLI_NO_CACHE", value)
        assert config_utils.cache_disabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_env_falsy_or_empty_falls_through_to_config(self, home, monkeypatch, value):
        # Config default is enabled, so a falsy/empty env leaves caching on.
        monkeypatch.setenv("CWCLI_NO_CACHE", value)
        assert config_utils.cache_disabled() is False

    def test_config_key_disables(self, home):
        config_utils.set_cache_enabled(False)
        assert config_utils.cache_disabled() is True
        config_utils.set_cache_enabled(True)
        assert config_utils.cache_disabled() is False

    def test_env_overrides_config_both_ways(self, home, monkeypatch):
        # config OFF but env re-enables for this shell
        config_utils.set_cache_enabled(False)
        monkeypatch.setenv("CWCLI_NO_CACHE", "0")
        assert config_utils.cache_disabled() is False
        # config ON but env disables for this shell
        config_utils.set_cache_enabled(True)
        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        assert config_utils.cache_disabled() is True

    def test_read_cache_config_never_creates_the_config_file(self, home):
        # Precedence reader runs on the hot path; it must not write a default config.
        assert not config_utils.CONFIG_FILE.exists()
        config_utils.read_cache_config()
        assert not config_utils.CONFIG_FILE.exists()


# ------------------------------------------------------------- the db behaviour


class TestDisabledReadsResolveLive:
    def test_db_points_at_memory_when_disabled(self, home, monkeypatch):
        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        db_utils.initialize_database()
        assert db_utils.db.database == db_utils._MEMORY_DB

    def test_stale_disk_row_is_not_served_and_disk_is_untouched(self, home, monkeypatch):
        mtime = _seed_disk_cache()
        assert db_utils.get_cached_project_data("proj") is not None  # enabled: served

        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        # No container here, so the live populate finds nothing -> a genuine miss,
        # NOT the stale disk row.
        assert db_utils.get_cached_project_data("proj") is None

        # The on-disk cache is left in place, byte-for-byte, so re-enabling restores it.
        assert db_utils.DB_PATH.exists()
        assert db_utils.DB_PATH.stat().st_mtime_ns == mtime

    def test_disabled_read_returns_live_data_over_the_stale_row(self, home, monkeypatch):
        mtime = _seed_disk_cache()
        monkeypatch.setenv("CWCLI_NO_CACHE", "1")

        fresh = {
            "path": BENCH,
            "sites": [{"name": "fresh.site", "installed_apps": ["frappe 16.0.0 version-16"]}],
            "available_apps": ["frappe", "fresh_app"],
        }

        def fake_inspect(project_name, **kwargs):
            # Stand in for a live container inspect: write fresh data into whatever
            # DB is active (the ephemeral in-memory store while disabled).
            db_utils.cache_project_data(project_name, [fresh])

        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", fake_inspect)

        data = db_utils.get_cached_project_data("proj")
        assert data is not None
        names = [s["name"] for b in data["bench_instances"] for s in b["sites"]]
        assert names == ["fresh.site"]  # live, not the stale.site on disk
        apps = data["bench_instances"][0]["available_apps"]
        assert "fresh_app" in apps and "stale_app" not in apps

        # Disk cache still holds the stale row, untouched.
        assert db_utils.DB_PATH.stat().st_mtime_ns == mtime

    def test_re_enabling_restores_the_untouched_disk_cache(self, home, monkeypatch):
        _seed_disk_cache()
        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        assert db_utils.get_cached_project_data("proj") is None
        monkeypatch.delenv("CWCLI_NO_CACHE")
        restored = db_utils.get_cached_project_data("proj")
        assert restored is not None
        assert restored["bench_instances"][0]["sites"][0]["name"] == "stale.site"


# ---------------------------------------------------------- the command surfaces


class TestConfigSurfaces:
    def test_show_config_reports_cache_state(self, home, monkeypatch):
        report = core_config.show_config().data
        assert report is not None
        assert report.cache_enabled is True
        assert report.cache_env_override is False

        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        report = core_config.show_config().data
        assert report.cache_enabled is False
        assert report.cache_env_override is True

    def test_set_cache_toggles_the_key(self, home):
        state = core_config.set_cache(False).data
        assert state is not None and state.enabled is False
        assert config_utils.cache_disabled() is True
        state = core_config.set_cache(True).data
        assert state.enabled is True
        assert config_utils.cache_disabled() is False

    def test_set_cache_reports_env_override(self, home, monkeypatch):
        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        # config says enable, but the env var wins and keeps it disabled.
        state = core_config.set_cache(True).data
        assert state is not None
        assert state.env_override is True
        assert state.enabled is False


class TestSideEffects:
    def test_auto_inspect_enable_refused_when_disabled(self, home, monkeypatch):
        from caffeinated_whale_cli.core import auto_inspect
        from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        with pytest.raises(CwcliError) as exc:
            auto_inspect.enable()
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "auto_inspect.cache_disabled"

    def test_recache_is_a_noop_when_disabled(self, home, monkeypatch):
        called = False

        def boom(*a, **k):
            nonlocal called
            called = True

        monkeypatch.setenv("CWCLI_NO_CACHE", "1")
        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", boom)
        assert cache.recache_project("proj") is True
        assert called is False  # no inspect, nothing to refresh

    def test_completion_returns_empty_when_disabled(self, home, monkeypatch):
        from caffeinated_whale_cli.utils import completion_utils

        monkeypatch.setenv("CWCLI_NO_CACHE", "1")

        class Ctx:
            params = {"project_name": "proj"}

        # Would otherwise trigger a live inspect; must degrade to empty instead.
        called = False

        def boom(*a, **k):
            nonlocal called
            called = True

        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", boom)
        assert completion_utils.complete_app_names(Ctx()) == []
        assert completion_utils.complete_site_names(Ctx()) == []
        assert called is False
