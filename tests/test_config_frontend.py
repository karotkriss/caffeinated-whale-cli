"""Frontend tests for the reworked `cwcli config` surface (rework-config-dx §3).

The NEW verbs (`show`, `paths`, `edit`, the fused `auto-inspect enable`/
`disable`, the `--json` reads) and the Decision-4 alias contract: hidden from
--help, deprecation warning on stderr ONLY, stdout byte-identical. The
characterization net (`test_config_characterization.py`) pins the surviving
behavior; this file covers what the rework added.
"""

import json

import click
from typer.testing import CliRunner

from caffeinated_whale_cli.main import app

# The shared storage/process-layer fixture; importing it registers it here.
from tests.test_config_characterization import cfg  # noqa: F401

runner = CliRunner()


class TestShow:
    def test_human_show_renders_every_store(self, cfg):
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        for expected in (
            "Config file:",
            "Cache DB:",
            "Search paths",
            "Enabled: No",
            "Interval: 3600 seconds",
            "Daemon: Stopped",
            "Start on boot: Disabled",
            "Tips: Enabled",
        ):
            assert expected in result.output

    def test_json_show_is_one_parseable_object(self, cfg):
        result = runner.invoke(app, ["config", "show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["search_paths"] == []
        assert data["show_tips"] is True
        assert "config_file" in data and "cache_db" in data
        ai = data["auto_inspect"]
        for key in (
            "enabled",
            "interval",
            "startup_enabled",
            "daemon_running",
            "daemon_pid",
            "boot_installed",
        ):
            assert key in ai
        assert "[" not in result.output.split("{")[0]  # no rich markup before the JSON

    def test_show_reflects_live_state(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        cfg.installed = True
        result = runner.invoke(app, ["config", "show"])
        assert "Running" in result.output
        assert "4242" in result.output
        assert "Start on boot: Enabled" in result.output


class TestBarePaths:
    def test_config_path_is_the_bare_path(self, cfg, monkeypatch):
        from caffeinated_whale_cli.utils import config_utils

        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert result.output == f"{config_utils.CONFIG_FILE}\n"

    def test_cache_path_is_the_bare_path(self, cfg):
        from caffeinated_whale_cli.utils import db_utils

        result = runner.invoke(app, ["config", "cache", "path"])
        assert result.exit_code == 0
        assert result.output == f"{db_utils.DB_PATH}\n"


class TestPathsGroup:
    def test_list_empty_is_explicit(self, cfg):
        result = runner.invoke(app, ["config", "paths"])
        assert result.exit_code == 0
        assert "No custom search paths configured." in result.output

    def test_list_prints_one_path_per_line(self, cfg):
        runner.invoke(app, ["config", "paths", "add", "/opt/a"])
        runner.invoke(app, ["config", "paths", "add", "/opt/b"])
        result = runner.invoke(app, ["config", "paths"])
        assert result.output == "/opt/a\n/opt/b\n"

    def test_list_json_empty_is_an_empty_array(self, cfg):
        result = runner.invoke(app, ["config", "paths", "--json"])
        assert json.loads(result.output) == []

    def test_add_refuses_relative_with_exit_two(self, cfg):
        result = runner.invoke(app, ["config", "paths", "add", "rel/path"])
        assert result.exit_code == 2
        assert "not an absolute path" in result.stderr

    def test_add_normalizes_and_dedups(self, cfg):
        runner.invoke(app, ["config", "paths", "add", "/a/b"])
        result = runner.invoke(app, ["config", "paths", "add", "/a/b/"])
        assert result.exit_code == 0
        assert "already exists" in result.output
        assert json.loads(runner.invoke(app, ["config", "paths", "--json"]).output) == ["/a/b"]

    def test_remove_matches_normalized(self, cfg):
        runner.invoke(app, ["config", "paths", "add", "/a/b"])
        result = runner.invoke(app, ["config", "paths", "remove", "/a/b/"])
        assert result.exit_code == 0
        assert "Removed '/a/b'" in result.output

    def test_remove_absent_is_exit_zero(self, cfg):
        result = runner.invoke(app, ["config", "paths", "remove", "/a/none"])
        assert result.exit_code == 0
        assert "not found" in result.output


class TestCacheJson:
    def test_cache_list_json_empty_is_an_empty_array(self, cfg):
        result = runner.invoke(app, ["config", "cache", "list", "--json"])
        assert json.loads(result.output) == []

    def test_cache_list_json_rows(self, cfg):
        from types import SimpleNamespace

        cfg.cached = [SimpleNamespace(name="proj", last_updated="2026-07-16 10:00:00")]
        rows = json.loads(runner.invoke(app, ["config", "cache", "list", "--json"]).output)
        assert rows == [{"name": "proj", "last_updated": "2026-07-16 10:00:00"}]

    def test_status_json_carries_all_three_stores(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        data = json.loads(runner.invoke(app, ["config", "auto-inspect", "status", "--json"]).output)
        assert data["daemon_running"] is True
        assert data["daemon_pid"] == 4242
        assert data["enabled"] is False
        assert data["boot_installed"] is False


class TestFusedEnableDisable:
    def test_enable_reaches_running_in_one_verb(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "enable"])
        assert result.exit_code == 0
        assert "Auto-inspect enabled." in result.output
        assert "background process started" in result.output
        assert cfg.calls["start"] == 1
        # No second command is required or suggested (the old two-step hint).
        assert "auto-inspect start" not in result.output

    def test_enable_is_idempotent(self, cfg):
        runner.invoke(app, ["config", "auto-inspect", "enable"])
        result = runner.invoke(app, ["config", "auto-inspect", "enable"])
        assert result.exit_code == 0
        assert "already running" in result.output
        assert cfg.calls["start"] == 1

    def test_enable_interval_change_restarts(self, cfg):
        runner.invoke(app, ["config", "auto-inspect", "enable"])
        result = runner.invoke(app, ["config", "auto-inspect", "enable", "-i", "900"])
        assert "Inspection interval set to 900 seconds." in result.output
        assert "restarted" in result.output
        assert cfg.calls["stop"] == 1
        assert cfg.calls["start"] == 2

    def test_enable_startup_installs_the_hook(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "enable", "--startup"])
        assert "Startup enabled." in result.output
        assert cfg.calls["install"] == 1

    def test_enable_no_startup_drops_the_hook_but_keeps_running(self, cfg):
        cfg.installed = True
        result = runner.invoke(app, ["config", "auto-inspect", "enable", "--no-startup"])
        assert "Startup configuration removed." in result.output
        assert cfg.calls["uninstall"] == 1
        assert cfg.running is True

    def test_disable_tears_down_all_three_stores(self, cfg):
        runner.invoke(app, ["config", "auto-inspect", "enable", "--startup"])
        result = runner.invoke(app, ["config", "auto-inspect", "disable"])
        assert result.exit_code == 0
        assert "background process stopped" in result.output
        assert "Auto-inspect disabled." in result.output
        assert "Startup configuration removed." in result.output
        assert cfg.running is False
        assert cfg.installed is False

    def test_disable_when_nothing_is_up_is_a_clean_success(self, cfg):
        result = runner.invoke(app, ["config", "auto-inspect", "disable"])
        assert result.exit_code == 0
        assert "Auto-inspect disabled." in result.output


class TestEdit:
    def test_edit_opens_the_config_file_in_the_editor(self, cfg, monkeypatch):
        opened = []
        monkeypatch.setattr(click, "edit", lambda filename=None, **kw: opened.append(filename))
        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 0
        from caffeinated_whale_cli.utils import config_utils

        assert opened == [str(config_utils.CONFIG_FILE)]
        assert config_utils.CONFIG_FILE.exists()  # created before the editor opened


class TestAliasContract:
    """Decision 4: hidden, stderr-only warning, stdout byte-identical."""

    def test_aliases_are_hidden_and_new_verbs_visible_in_the_registry(self, cfg):
        from caffeinated_whale_cli.commands import config as config_mod

        def split(typer_app):
            visible = {c.name for c in typer_app.registered_commands if not c.hidden}
            hidden = {c.name for c in typer_app.registered_commands if c.hidden}
            return visible, hidden

        top_visible, top_hidden = split(config_mod.app)
        assert top_hidden == {"add-path", "remove-path"}
        assert top_visible == {"show", "path", "edit"}

        ai_visible, ai_hidden = split(config_mod.auto_inspect_app)
        assert ai_hidden == {
            "start",
            "restart",
            "set-interval",
            "install-startup",
            "uninstall-startup",
        }
        assert ai_visible == {"enable", "disable", "stop", "status", "logs"}

        tips_visible, tips_hidden = split(config_mod.tips_app)
        assert tips_hidden == {"status"}
        assert tips_visible == {"enable", "disable"}

        # And --help genuinely drops them (the registry flag reaches the render).
        top_help = runner.invoke(app, ["config", "--help"]).output
        assert "add-path" not in top_help
        assert "remove-path" not in top_help

    def test_alias_warning_is_stderr_only_stdout_unchanged(self, cfg):
        result = runner.invoke(app, ["config", "add-path", "/opt/benches"])
        assert result.exit_code == 0
        assert "deprecated" in result.stderr
        assert "deprecated" not in result.stdout
        assert "Added '/opt/benches' to custom search paths." in result.stdout

    def test_every_alias_warns_on_stderr(self, cfg):
        from caffeinated_whale_cli.utils import config_utils

        config = config_utils.load_config()
        config["auto_inspect"]["enabled"] = True
        config_utils.save_config(config)
        for argv in (
            ["config", "add-path", "/opt/x"],
            ["config", "remove-path", "/opt/x"],
            ["config", "auto-inspect", "start"],
            ["config", "auto-inspect", "restart"],
            ["config", "auto-inspect", "set-interval", "900"],
            ["config", "auto-inspect", "install-startup"],
            ["config", "auto-inspect", "uninstall-startup"],
            ["config", "tips", "status"],
        ):
            result = runner.invoke(app, argv)
            assert "deprecated" in result.stderr, argv
            assert "deprecated" not in result.stdout, argv

    def test_alias_add_path_gains_the_validation_exception(self, cfg):
        result = runner.invoke(app, ["config", "add-path", "rel/path"])
        assert result.exit_code == 2
        assert "not an absolute path" in result.stderr
