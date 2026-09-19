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


# --------------------------------------------------------------------------- #
# prune_search_paths (issue #237): drop search paths no live instance references
# --------------------------------------------------------------------------- #

from caffeinated_whale_cli.core.envelope import Result  # noqa: E402
from caffeinated_whale_cli.core.list import InstanceDTO  # noqa: E402


class _PruneFake:
    """A frappe container that reports which of the probed paths exist as dirs."""

    def __init__(self, *, running=True, existing=(), exec_fails=False):
        self.status = "running" if running else "exited"
        self.labels = {"com.docker.compose.service": "frappe"}
        self._existing = set(existing)
        self._exec_fails = exec_fails

    def exec_run(self, cmd, workdir=None):
        if self._exec_fails:
            raise RuntimeError("exec broke")
        # cmd is ["sh", "-c", <script>, "sh", *paths]; echo back the existing ones
        # plus the completion sentinel _existing_dirs looks for.
        paths = cmd[4:]
        lines = [p for p in paths if p in self._existing]
        lines.append("__CWCLI_PRUNE_END__")
        return (0, ("\n".join(lines) + "\n").encode())


def _wire_instances(monkeypatch, instances, containers):
    """instances: [(name, running)]; containers: {name: _PruneFake or None}."""
    dtos = [
        InstanceDTO(project_name=n, status=("running" if r else "exited"), ports=[])
        for n, r in instances
    ]
    monkeypatch.setattr(
        core_config.core_list, "list_instances", lambda **k: Result(status=Status.OK, data=dtos)
    )
    monkeypatch.setattr(
        core_config,
        "get_project_containers",
        lambda name: ([containers[name]] if containers.get(name) else containers.get(name, [])),
    )


class TestPruneSearchPaths:
    def test_no_custom_paths_is_a_noop(self, cfg, monkeypatch):
        _wire_instances(monkeypatch, [], {})
        result = core_config.prune_search_paths(apply=True)
        assert result.status is Status.OK
        assert result.data.statuses == []
        assert result.data.pruned == []

    def test_dry_run_lists_dead_paths_without_writing(self, cfg, monkeypatch):
        config_utils.add_custom_path("/live/root")
        config_utils.add_custom_path("/dead/root")
        _wire_instances(monkeypatch, [("a", True)], {"a": _PruneFake(existing={"/live/root"})})

        result = core_config.prune_search_paths(apply=False)
        plan = result.data
        assert plan.applied is False
        assert plan.pruned == []
        prunable = {s.path for s in plan.statuses if s.prunable}
        assert prunable == {"/dead/root"}
        assert {s.path for s in plan.statuses if s.present} == {"/live/root"}
        # Nothing written on a dry-run.
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == [
            "/live/root",
            "/dead/root",
        ]

    def test_apply_removes_only_dead_paths(self, cfg, monkeypatch):
        config_utils.add_custom_path("/live/root")
        config_utils.add_custom_path("/dead/root")
        _wire_instances(monkeypatch, [("a", True)], {"a": _PruneFake(existing={"/live/root"})})

        result = core_config.prune_search_paths(apply=True)
        assert result.data.applied is True
        assert result.data.pruned == ["/dead/root"]
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == ["/live/root"]

    def test_path_present_in_any_instance_is_kept(self, cfg, monkeypatch):
        config_utils.add_custom_path("/shared/root")
        _wire_instances(
            monkeypatch,
            [("a", True), ("b", True)],
            {"a": _PruneFake(existing=set()), "b": _PruneFake(existing={"/shared/root"})},
        )
        result = core_config.prune_search_paths(apply=True)
        assert result.data.pruned == []
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == ["/shared/root"]

    def test_stopped_instance_blocks_all_pruning(self, cfg, monkeypatch):
        config_utils.add_custom_path("/dead/root")
        _wire_instances(
            monkeypatch,
            [("a", True), ("b", False)],
            {"a": _PruneFake(existing=set()), "b": None},
        )
        result = core_config.prune_search_paths(apply=True)
        # A stopped instance cannot be checked, so nothing is pruned even though
        # the path is absent from the running one.
        assert result.data.pruned == []
        assert result.data.unchecked_instances == ["b"]
        assert all(not s.prunable for s in result.data.statuses)
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == ["/dead/root"]

    def test_exec_failure_marks_instance_unchecked(self, cfg, monkeypatch):
        config_utils.add_custom_path("/dead/root")
        _wire_instances(monkeypatch, [("a", True)], {"a": _PruneFake(exec_fails=True)})
        result = core_config.prune_search_paths(apply=True)
        assert result.data.unchecked_instances == ["a"]
        assert result.data.pruned == []

    def test_zero_instances_prunes_the_whole_graveyard(self, cfg, monkeypatch):
        config_utils.add_custom_path("/dead/one")
        config_utils.add_custom_path("/dead/two")
        _wire_instances(monkeypatch, [], {})
        result = core_config.prune_search_paths(apply=True)
        assert sorted(result.data.pruned) == ["/dead/one", "/dead/two"]
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == []

    def test_daemon_unreachable_propagates(self, cfg, monkeypatch):
        config_utils.add_custom_path("/dead/root")

        def _raise(**k):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "no daemon")

        monkeypatch.setattr(core_config.core_list, "list_instances", _raise)
        with pytest.raises(CwcliError) as exc:
            core_config.prune_search_paths(apply=True)
        assert exc.value.kind is ErrorKind.DOCKER
        # Config untouched on a failed verification.
        assert config_utils.load_config()["search_paths"]["custom_bench_paths"] == ["/dead/root"]
