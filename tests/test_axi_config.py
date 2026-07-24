"""``cwcli axi config`` tests: one TOON document, the exit mapping, and the
registry assertion that no config-mutating verb exists (rework-config-dx §4)."""

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests.test_axi import assert_is_one_toon_document

pytest_plugins = ("tests.test_config_characterization",)

runner = CliRunner()


class TestAxiConfig:
    def test_emits_one_toon_document_and_exits_zero(self, cfg):
        result = runner.invoke(axi_mod.app, ["config"])
        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)

    def test_document_carries_every_store(self, cfg):
        cfg.running = True
        cfg.pid = 4242
        cfg.installed = True
        result = runner.invoke(axi_mod.app, ["config"])
        out = result.stdout
        assert "config_file:" in out
        assert "cache_db:" in out
        assert "search_paths[0]:" in out
        assert "auto_inspect:" in out
        assert "  enabled: false" in out
        assert "  daemon_running: true" in out
        assert "  daemon_pid: 4242" in out
        assert "  boot_installed: true" in out
        assert "show_tips: true" in out

    def test_search_paths_render_inline(self, cfg):
        from caffeinated_whale_cli.core import config as core_config

        core_config.add_search_path("/opt/benches")
        result = runner.invoke(axi_mod.app, ["config"])
        assert "search_paths[1]: /opt/benches" in result.stdout
        assert_is_one_toon_document(result.stdout)

    def test_internal_failure_is_a_structured_error_exit_one(self, cfg, monkeypatch):
        def _boom():
            raise CwcliError(ErrorKind.INTERNAL, "config.read_failed", "config unreadable")

        monkeypatch.setattr(axi_mod.core_config, "show_config", _boom)
        result = runner.invoke(axi_mod.app, ["config"])
        assert result.exit_code == 1
        assert result.stdout.startswith("error: ")

    def test_verb_takes_no_mutating_flags(self, cfg):
        command = next(c for c in axi_mod.app.registered_commands if c.name == "config")
        import inspect

        params = [p for p in inspect.signature(command.callback).parameters]
        assert params == []  # a pure read: no arguments, no options


class TestNoMutatingAxiConfigVerbs:
    """Design Decision 6: `axi config` is the ONE config verb, read-only.
    Mutations (paths add/remove, cache clear, auto-inspect enable/disable) are
    deliberately NOT built - the `axi apps uninstall` deferral
    discipline. This test keeps that a decision, not an oversight."""

    def test_registry_carries_config_and_no_config_mutations(self):
        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "config" in registered

        # No config-adjacent mutation appears as a top-level verb.
        forbidden = {
            "add-path",
            "remove-path",
            "paths",
            "cache",
            "auto-inspect",
            "tips",
            "edit",
        }
        assert registered & forbidden == set()

        # And no `axi config <sub>` group exists at all (a group is where
        # mutations would live; `config` is a plain command).
        group_names = {g.name for g in axi_mod.app.registered_groups}
        assert "config" not in group_names

    def test_no_subapp_smuggles_a_config_mutation(self):
        for group in axi_mod.app.registered_groups:
            sub = {c.name for c in group.typer_instance.registered_commands}
            assert not {"add-path", "remove-path", "clear", "enable", "disable"} & sub, group.name
