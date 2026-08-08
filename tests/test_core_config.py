"""``core.config`` unit tests: every branch, the NEEDS_CHOICE fork, plain-data
DTOs, and core silence - all against a tmp_path config, never the real one."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import config as core_config
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import auto_inspect, config_utils, db_utils, startup


def _assert_plain(data):
    """Every value reachable from asdict() is a builtin scalar/list/dict."""

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        else:
            assert value is None or isinstance(value, (str, int, float, bool)), repr(value)

    walk(asdict(data) if not isinstance(data, list) else {"items": [asdict(d) for d in data]})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config_utils, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", cfg_dir / "config.toml")
    run_dir = tmp_path / "run"
    monkeypatch.setattr(auto_inspect, "PID_DIR", run_dir)
    monkeypatch.setattr(auto_inspect, "PID_FILE", run_dir / "auto-inspect.pid")
    monkeypatch.setattr(auto_inspect, "LOG_FILE", run_dir / "auto-inspect.log")
    monkeypatch.setattr(auto_inspect, "is_running", lambda: False)
    monkeypatch.setattr(startup, "is_startup_installed", lambda unit=None: False)

    state = SimpleNamespace(cached=[], cleared_all=0, cleared_projects=[])
    monkeypatch.setattr(db_utils, "get_all_cached_projects", lambda: list(state.cached))
    monkeypatch.setattr(
        db_utils, "clear_all_cache", lambda: setattr(state, "cleared_all", state.cleared_all + 1)
    )

    def _clear_project(name):
        state.cleared_projects.append(name)
        return any(p.name == name for p in state.cached)

    monkeypatch.setattr(db_utils, "clear_cache_for_project", _clear_project)
    return state


class TestShowConfig:
    def test_reports_every_store(self, cfg, capsys):
        config_utils.add_custom_path("/opt/benches")
        result = core_config.show_config()
        assert result.status is Status.OK
        report = result.data
        assert report.config_file == str(config_utils.CONFIG_FILE)
        assert report.cache_db == str(db_utils.DB_PATH)
        assert report.search_paths == ["/opt/benches"]
        assert report.auto_inspect.enabled is False
        assert report.show_tips is True
        _assert_plain(report)
        assert capsys.readouterr() == ("", "")  # the core prints nothing at all


class TestAddSearchPath:
    def test_adds_a_normalized_path(self, cfg):
        result = core_config.add_search_path("/opt/benches/")
        assert result.status is Status.OK
        assert result.data.changed is True
        assert result.data.path == "/opt/benches"
        assert result.data.search_paths == ["/opt/benches"]
        _assert_plain(result.data)

    def test_expands_the_user_home(self, cfg, monkeypatch):
        monkeypatch.setenv("HOME", "/home/someone")
        result = core_config.add_search_path("~/benches")
        assert result.data.path == "/home/someone/benches"

    def test_refuses_a_relative_path_without_writing(self, cfg):
        with pytest.raises(CwcliError) as exc:
            core_config.add_search_path("not/absolute/../weird")
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "path.not_absolute"
        assert not config_utils.CONFIG_FILE.exists() or (
            config_utils.load_config()["search_paths"]["custom_bench_paths"] == []
        )

    def test_duplicate_after_normalization_is_a_no_op_success(self, cfg):
        core_config.add_search_path("/a/b")
        result = core_config.add_search_path("/a/b/")
        assert result.status is Status.OK
        assert result.data.changed is False
        assert result.data.search_paths == ["/a/b"]

    def test_dedups_against_a_legacy_unnormalized_entry(self, cfg):
        # An entry stored by the old exact-string add-path.
        config = config_utils.load_config()
        config["search_paths"]["custom_bench_paths"].append("/a/b/")
        config_utils.save_config(config)
        result = core_config.add_search_path("/a/b")
        assert result.data.changed is False


class TestRemoveSearchPath:
    def test_removes_on_a_normalized_match(self, cfg):
        core_config.add_search_path("/a/b")
        result = core_config.remove_search_path("/a/b/")
        assert result.data.changed is True
        assert result.data.search_paths == []

    def test_absent_path_is_a_no_op_success(self, cfg):
        result = core_config.remove_search_path("/a/none")
        assert result.status is Status.OK
        assert result.data.changed is False

    def test_can_remove_a_legacy_relative_entry(self, cfg):
        # Garbage the old unvalidated add-path stored must stay removable.
        config = config_utils.load_config()
        config["search_paths"]["custom_bench_paths"].append("not/absolute/../weird")
        config_utils.save_config(config)
        result = core_config.remove_search_path("not/absolute/../weird")
        assert result.data.changed is True
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == []


class TestSetTips:
    def test_writes_and_reports(self, cfg, capsys):
        result = core_config.set_tips(False)
        assert result.data.show_tips is False
        assert config_utils.get_show_tips() is False
        _assert_plain(result.data)
        assert capsys.readouterr() == ("", "")


class TestCachedProjects:
    def test_empty_inventory(self, cfg):
        result = core_config.cached_projects()
        assert result.status is Status.OK
        assert result.data == []

    def test_lists_projects_as_plain_dtos(self, cfg):
        cfg.cached = [SimpleNamespace(name="proj-a", last_updated="2026-07-16 10:00:00")]
        result = core_config.cached_projects()
        assert [p.name for p in result.data] == ["proj-a"]
        _assert_plain(result.data)


class TestClearCache:
    def test_project_and_all_is_a_usage_error_clearing_nothing(self, cfg):
        with pytest.raises(CwcliError) as exc:
            core_config.clear_cache("proj", all_projects=True, consent=True)
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "cache.conflicting_target"
        assert cfg.cleared_all == 0
        assert cfg.cleared_projects == []

    def test_no_target_is_a_usage_error(self, cfg):
        with pytest.raises(CwcliError) as exc:
            core_config.clear_cache()
        assert exc.value.kind is ErrorKind.USAGE
        assert exc.value.code == "cache.no_target"

    def test_all_without_consent_is_a_needs_choice_not_a_wipe(self, cfg):
        result = core_config.clear_cache(all_projects=True)
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_clear"
        assert result.choice.param == "consent"
        assert cfg.cleared_all == 0

    def test_all_with_consent_clears_everything(self, cfg):
        result = core_config.clear_cache(all_projects=True, consent=True)
        assert result.status is Status.OK
        assert result.data.scope == "all"
        assert cfg.cleared_all == 1
        _assert_plain(result.data)

    def test_single_project_found(self, cfg):
        cfg.cached = [SimpleNamespace(name="proj", last_updated="x")]
        result = core_config.clear_cache("proj")
        assert result.data.scope == "project"
        assert result.data.project == "proj"
        assert result.data.found is True
        assert cfg.cleared_projects == ["proj"]

    def test_single_project_missing_is_ok_with_found_false(self, cfg):
        result = core_config.clear_cache("ghost")
        assert result.status is Status.OK
        assert result.data.found is False
