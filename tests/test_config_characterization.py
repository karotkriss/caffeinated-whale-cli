"""Characterization net for the `cwcli config` surface (openspec `rework-config-dx`, tasks §1).

Green against the UNMIGRATED monolith and committed separately (the batch 4
discipline), so the rework's refactor-under-green is auditable. Everything the
proposal table marks *unchanged* or *frozen alias* is pinned here through the
one seam that survives the migration: Typer's own parser via ``CliRunner``
against the real ``main.app`` (argv in, stdout + exit code out). The fakes
patch the STORAGE/PROCESS layer modules (``config_utils``/``auto_inspect``/
``startup``/``db_utils``) - the layer both the monolith and the future core
call - never ``commands.config`` internals, so no test names an attribute that
moves.

The behavior DELTAS the proposal disclosed (F3 partial mutation, F4 conflicting
clear, F9 garbage add-path, F10 exit codes) ride at the bottom asserting the
NEW behavior. They were committed as ``xfail(strict=True)`` against the
unmigrated monolith (each XFAILed, recording the driven evidence) and flipped
XPASS when the rework landed, so the markers came off in the rework commit -
the auditable flip.
"""

import time
from types import SimpleNamespace

import pytest
import toml
from typer.testing import CliRunner

from caffeinated_whale_cli.main import app
from caffeinated_whale_cli.utils import auto_inspect, config_utils, cred_daemon, db_utils, startup

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated config file + inert, call-recording daemon/startup/db fakes.

    No test here may touch the real ``~/.cwcli``, spawn a real daemon, or write
    a real boot unit.
    """
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config_utils, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_utils, "CONFIG_FILE", cfg_dir / "config.toml")
    run_dir = tmp_path / "run"
    monkeypatch.setattr(auto_inspect, "PID_DIR", run_dir)
    monkeypatch.setattr(auto_inspect, "PID_FILE", run_dir / "auto-inspect.pid")
    monkeypatch.setattr(auto_inspect, "LOG_FILE", run_dir / "auto-inspect.log")
    # `config show` also reads the credential-bridge state; keep it off the real
    # ~/.cwcli/run too (read-only here - these tests never enable the bridge).
    monkeypatch.setattr(cred_daemon, "PID_DIR", run_dir)
    monkeypatch.setattr(cred_daemon, "PID_FILE", run_dir / "credbridge.pid")
    monkeypatch.setattr(cred_daemon, "LOG_FILE", run_dir / "credbridge.log")
    monkeypatch.setattr(cred_daemon, "AUDIT_FILE", run_dir / "credbridge-audit.log")
    monkeypatch.setattr(cred_daemon, "REGISTRY_FILE", run_dir / "credbridge-registry.json")

    state = SimpleNamespace(
        running=False,
        pid=None,
        installed=False,
        cached=[],
        calls={"start": 0, "stop": 0, "install": 0, "uninstall": 0, "clear_all": 0},
        cleared_projects=[],
    )

    def _start():
        state.calls["start"] += 1
        state.running = True
        state.pid = 4242

    def _stop():
        state.calls["stop"] += 1
        state.running = False
        state.pid = None

    def _install():
        state.calls["install"] += 1
        state.installed = True
        return True

    def _uninstall():
        state.calls["uninstall"] += 1
        state.installed = False
        return True

    def _clear_all():
        state.calls["clear_all"] += 1

    def _clear_project(name):
        state.cleared_projects.append(name)
        return any(p.name == name for p in state.cached)

    monkeypatch.setattr(auto_inspect, "is_running", lambda: state.running)
    monkeypatch.setattr(auto_inspect, "get_pid", lambda: state.pid)
    monkeypatch.setattr(auto_inspect, "start_daemon", _start)
    monkeypatch.setattr(auto_inspect, "stop_daemon", _stop)
    monkeypatch.setattr(startup, "is_startup_installed", lambda: state.installed)
    monkeypatch.setattr(startup, "install_startup", _install)
    monkeypatch.setattr(startup, "uninstall_startup", _uninstall)
    monkeypatch.setattr(startup, "get_platform", lambda: "linux")
    monkeypatch.setattr(db_utils, "clear_all_cache", _clear_all)
    monkeypatch.setattr(db_utils, "clear_cache_for_project", _clear_project)
    monkeypatch.setattr(db_utils, "get_all_cached_projects", lambda: list(state.cached))
    # `restart`'s deliberate settle pause; keep the suite fast.
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return state


def _saved_config():
    return toml.load(config_utils.CONFIG_FILE)


def _enable_in_config(enabled: bool):
    config = config_utils.load_config()
    config["auto_inspect"]["enabled"] = enabled
    config_utils.save_config(config)


# --------------------------------------------------------- cache clear (kept rows)


class TestCacheClearSingleProject:
    def test_clears_a_cached_project(self, cfg):
        cfg.cached = [SimpleNamespace(name="proj", last_updated="2026-07-16")]
        result = runner.invoke(app, ["config", "cache", "clear", "proj"])
        assert result.exit_code == 0
        assert "Cache for project 'proj' cleared." in result.output
        assert cfg.cleared_projects == ["proj"]

    def test_missing_project_is_reported_not_an_error(self, cfg):
        result = runner.invoke(app, ["config", "cache", "clear", "ghost"])
        assert result.exit_code == 0
        assert "No cache found for project 'ghost'." in result.output

    def test_all_with_yes_clears_everything(self, cfg):
        result = runner.invoke(app, ["config", "cache", "clear", "--all", "--yes"])
        assert result.exit_code == 0
        assert "Entire cache has been cleared." in result.output
        assert cfg.calls["clear_all"] == 1

    def test_all_without_yes_on_a_non_tty_refuses_and_preserves_the_cache(self, cfg):
        result = runner.invoke(app, ["config", "cache", "clear", "--all"])
        assert result.exit_code == 1
        assert cfg.calls["clear_all"] == 0


# ------------------------------------------------------------- cache list (kept)


class TestCacheList:
    def test_empty_cache_message(self, cfg):
        result = runner.invoke(app, ["config", "cache", "list"])
        assert result.exit_code == 0
        assert "No projects found in the cache." in result.output

    def test_lists_cached_projects(self, cfg):
        cfg.cached = [
            SimpleNamespace(name="proj-a", last_updated="2026-07-15 10:00:00"),
            SimpleNamespace(name="proj-b", last_updated="2026-07-16 11:00:00"),
        ]
        result = runner.invoke(app, ["config", "cache", "list"])
        assert result.exit_code == 0
        assert "proj-a" in result.output
        assert "proj-b" in result.output


# ------------------------------------------------- auto-inspect status/logs/stop


class TestAutoInspectStatus:
    def test_stopped_disabled_status_fields(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "status"])
        assert result.exit_code == 0
        assert "Enabled" in result.output
        assert "No" in result.output
        assert "3600 seconds" in result.output
        assert "Stopped" in result.output
        assert "Disabled" in result.output  # Start on Boot

    def test_running_status_shows_pid_and_log_hint(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        result = runner.invoke(app, ["config", "auto-inspect", "status"])
        assert result.exit_code == 0
        assert "Running" in result.output
        assert "4242" in result.output
        assert "cwcli config auto-inspect logs" in result.output

    def test_startup_configured_but_not_installed_is_distinguished(self, cfg):
        config = config_utils.load_config()
        config["auto_inspect"]["startup_enabled"] = True
        config_utils.save_config(config)
        result = runner.invoke(app, ["config", "auto-inspect", "status"])
        assert result.exit_code == 0
        assert "Enabled (not installed)" in result.output


class TestAutoInspectLogs:
    def test_tails_the_requested_lines(self, cfg):
        auto_inspect.PID_DIR.mkdir(parents=True, exist_ok=True)
        auto_inspect.LOG_FILE.write_text("".join(f"line {i}\n" for i in range(10)))
        result = runner.invoke(app, ["config", "auto-inspect", "logs", "-n", "3"])
        assert result.exit_code == 0
        assert "Last 3 log lines:" in result.output
        assert "line 9" in result.output
        assert "line 6" not in result.output

    def test_no_log_file_is_reported(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "logs"])
        assert result.exit_code == 0
        assert "No log file found" in result.output


class TestAutoInspectStop:
    def test_stops_a_running_daemon(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        result = runner.invoke(app, ["config", "auto-inspect", "stop"])
        assert result.exit_code == 0
        assert "stopped" in result.output
        assert cfg.calls["stop"] == 1

    def test_stop_when_not_running_is_an_idempotent_success(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "stop"])
        assert result.exit_code == 0
        assert "not running" in result.output
        assert cfg.calls["stop"] == 0


# ------------------------------------------------------------ tips enable/disable


class TestTipsToggle:
    def test_enable_tips(self, cfg):
        result = runner.invoke(app, ["config", "tips", "enable"])
        assert result.exit_code == 0
        assert "Contextual tips enabled." in result.output
        assert _saved_config()["ui"]["show_tips"] is True

    def test_disable_tips(self, cfg):
        result = runner.invoke(app, ["config", "tips", "disable"])
        assert result.exit_code == 0
        assert "Contextual tips disabled." in result.output
        assert _saved_config()["ui"]["show_tips"] is False


# ---------------------------------------------------- frozen aliases, byte-pinned
# Every branch of every alias row, because these must ship byte-identical
# (Decision 4). `start`'s refuse-when-disabled guard above all: installed boot
# units exec `cwcli config auto-inspect start` verbatim, and that guard is what
# keeps a stale hook inert after a disable.


class TestStartAliasFrozen:
    def test_refuses_when_disabled(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "start"])
        assert result.exit_code == 1
        assert "Auto-inspect is not enabled." in result.output
        assert "cwcli config auto-inspect enable" in result.output
        assert cfg.calls["start"] == 0

    def test_already_running_is_a_no_op_success(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        result = runner.invoke(app, ["config", "auto-inspect", "start"])
        assert result.exit_code == 0
        assert "already running" in result.output
        assert "4242" in result.output
        assert cfg.calls["start"] == 0

    def test_starts_when_enabled(self, cfg):
        _enable_in_config(True)
        result = runner.invoke(app, ["config", "auto-inspect", "start"])
        assert result.exit_code == 0
        assert "background process started" in result.output
        assert "3600 seconds" in result.output
        assert cfg.calls["start"] == 1

    def test_startup_flag_installs_the_boot_hook_after_starting(self, cfg):
        _enable_in_config(True)
        result = runner.invoke(app, ["config", "auto-inspect", "start", "--startup"])
        assert result.exit_code == 0
        assert cfg.calls["start"] == 1
        assert cfg.calls["install"] == 1
        assert _saved_config()["auto_inspect"]["startup_enabled"] is True


class TestRestartAliasFrozen:
    def test_refuses_when_disabled(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "restart"])
        assert result.exit_code == 1
        assert "Auto-inspect is not enabled." in result.output

    def test_restarts_a_running_daemon(self, cfg):
        _enable_in_config(True)
        cfg.running = True
        cfg.pid = 4242
        result = runner.invoke(app, ["config", "auto-inspect", "restart"])
        assert result.exit_code == 0
        assert "restarted" in result.output
        assert cfg.calls["stop"] == 1
        assert cfg.calls["start"] == 1


class TestSetIntervalAliasFrozen:
    def test_sets_a_valid_interval(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "set-interval", "900"])
        assert result.exit_code == 0
        assert "Inspection interval set to 900 seconds." in result.output
        assert _saved_config()["auto_inspect"]["interval"] == 900

    def test_rejects_a_sub_minimum_interval_without_writing(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "set-interval", "30"])
        assert result.exit_code == 1
        assert "at least 60 seconds" in result.output
        assert not config_utils.CONFIG_FILE.exists() or (
            _saved_config()["auto_inspect"]["interval"] == 3600
        )

    def test_notes_the_needed_restart_when_running(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        result = runner.invoke(app, ["config", "auto-inspect", "set-interval", "900"])
        assert result.exit_code == 0
        assert "Restart the background process" in result.output


class TestInstallStartupAliasFrozen:
    def test_installs_and_records_the_flag(self, cfg):
        _enable_in_config(True)
        result = runner.invoke(app, ["config", "auto-inspect", "install-startup"])
        assert result.exit_code == 0
        assert "installed successfully" in result.output
        assert cfg.calls["install"] == 1
        assert _saved_config()["auto_inspect"]["startup_enabled"] is True

    def test_warns_but_proceeds_when_auto_inspect_is_disabled(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "install-startup"])
        assert result.exit_code == 0
        assert "Auto-inspect is not enabled" in result.output
        assert cfg.calls["install"] == 1

    def test_already_installed_is_a_no_op_success(self, cfg):
        cfg.installed = True
        result = runner.invoke(app, ["config", "auto-inspect", "install-startup"])
        assert result.exit_code == 0
        assert "already installed" in result.output
        assert cfg.calls["install"] == 0


class TestUninstallStartupAliasFrozen:
    def test_uninstalls_and_records_the_flag(self, cfg):
        cfg.installed = True
        result = runner.invoke(app, ["config", "auto-inspect", "uninstall-startup"])
        assert result.exit_code == 0
        assert "removed successfully" in result.output
        assert cfg.calls["uninstall"] == 1
        assert _saved_config()["auto_inspect"]["startup_enabled"] is False

    def test_not_installed_is_a_no_op_success(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "uninstall-startup"])
        assert result.exit_code == 0
        assert "not installed" in result.output
        assert cfg.calls["uninstall"] == 0


class TestTipsStatusAliasFrozen:
    def test_enabled_state(self, cfg):
        result = runner.invoke(app, ["config", "tips", "status"])
        assert result.exit_code == 0
        assert "Tips Display" in result.output
        assert "cwcli config tips disable" in result.output

    def test_disabled_state(self, cfg):
        runner.invoke(app, ["config", "tips", "disable"])
        result = runner.invoke(app, ["config", "tips", "status"])
        assert result.exit_code == 0
        assert "Tips are currently disabled." in result.output
        assert "cwcli config tips enable" in result.output


class TestPathAliasesFrozen:
    """`add-path`/`remove-path` with WELL-FORMED absolute paths: the surviving
    behavior. The garbage-input delta (F9) rides in the xfail block below."""

    def test_add_path_stores_and_reports(self, cfg):
        result = runner.invoke(app, ["config", "add-path", "/opt/benches"])
        assert result.exit_code == 0
        assert "Added '/opt/benches' to custom search paths." in result.output
        assert _saved_config()["search_paths"]["custom_bench_paths"] == ["/opt/benches"]

    def test_add_path_duplicate_is_reported_not_duplicated(self, cfg):
        runner.invoke(app, ["config", "add-path", "/opt/benches"])
        result = runner.invoke(app, ["config", "add-path", "/opt/benches"])
        assert result.exit_code == 0
        assert "already exists" in result.output
        assert _saved_config()["search_paths"]["custom_bench_paths"] == ["/opt/benches"]

    def test_remove_path_removes_and_reports(self, cfg):
        runner.invoke(app, ["config", "add-path", "/opt/benches"])
        result = runner.invoke(app, ["config", "remove-path", "/opt/benches"])
        assert result.exit_code == 0
        assert "Removed '/opt/benches' from custom search paths." in result.output
        assert _saved_config()["search_paths"]["custom_bench_paths"] == []

    def test_remove_path_absent_is_a_no_op_success(self, cfg):
        result = runner.invoke(app, ["config", "remove-path", "/opt/none"])
        assert result.exit_code == 0
        assert "not found" in result.output


# --------------------------------------------------------- the disclosed deltas
# Driven evidence for the proposal's behavior deltas (F3/F4/F9/F10), asserting
# the NEW behavior. These were committed as xfail(strict=True) against the
# unmigrated monolith (each XFAILed, recording the driven evidence) and flipped
# XPASS when the rework landed, so the markers came off in the rework commit -
# the auditable flip tasks §1.2 asked for.


class TestDisclosedDeltas:
    def test_f3_failed_enable_does_not_mutate_the_config(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "enable", "--interval", "30"])
        assert result.exit_code != 0
        # Unmutated: either never created, or created with enabled still false.
        assert not config_utils.CONFIG_FILE.exists() or (
            _saved_config()["auto_inspect"]["enabled"] is False
        )
        assert cfg.calls["start"] == 0

    def test_f4_project_plus_all_is_a_usage_error_clearing_nothing(self, cfg):
        result = runner.invoke(app, ["config", "cache", "clear", "proj", "--all", "--yes"])
        assert result.exit_code == 2
        assert cfg.calls["clear_all"] == 0
        assert cfg.cleared_projects == []

    def test_f9_relative_path_is_refused(self, cfg):
        result = runner.invoke(app, ["config", "add-path", "not/absolute/../weird"])
        assert result.exit_code == 2
        assert not config_utils.CONFIG_FILE.exists() or (
            _saved_config()["search_paths"]["custom_bench_paths"] == []
        )

    def test_f9_trailing_slash_dedupes_to_one_entry(self, cfg):
        runner.invoke(app, ["config", "add-path", "/a/b"])
        runner.invoke(app, ["config", "add-path", "/a/b/"])
        assert _saved_config()["search_paths"]["custom_bench_paths"] == ["/a/b"]

    def test_f10_no_clear_target_is_a_usage_error(self, cfg):
        result = runner.invoke(app, ["config", "cache", "clear"])
        assert result.exit_code == 2

    def test_f10_logs_read_failure_exits_nonzero(self, cfg, monkeypatch):
        def _boom(lines):
            raise OSError("permission denied")

        monkeypatch.setattr(auto_inspect, "get_log_tail", _boom)
        result = runner.invoke(app, ["config", "auto-inspect", "logs"])
        assert result.exit_code == 1
