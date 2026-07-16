"""The SessionStart hook installer (`cwcli axi setup`).

Every test drives a THROWAWAY home (`tmp_path`), never the real one. These writes
land in files the user owns, so the suite's job is to prove the installer is
idempotent, repairs rather than duplicates, and never clobbers what it did not
write.
"""

from __future__ import annotations

import json

import pytest
import toml
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.utils import agent_hooks

runner = CliRunner()

CMD = "/opt/bin/cwcli axi"


def _home(tmp_path, *, claude=True, codex=True, opencode=True):
    """A fake home with the requested agent config dirs already present."""
    if claude:
        (tmp_path / ".claude").mkdir(parents=True)
    if codex:
        (tmp_path / ".codex").mkdir(parents=True)
    if opencode:
        (tmp_path / ".config" / "opencode").mkdir(parents=True)
    return tmp_path


def _status(outcomes, agent):
    return next(o.status for o in outcomes if o.agent == agent)


def _session_start(path):
    return json.loads(path.read_text())["hooks"]["SessionStart"]


class TestInstall:
    def test_installs_into_all_three_detected_agents(self, tmp_path):
        outcomes = agent_hooks.install(CMD, home=_home(tmp_path))

        assert {o.agent for o in outcomes} == {"claude-code", "codex", "opencode"}
        assert all(o.status == "installed" for o in outcomes)
        assert _session_start(tmp_path / ".claude" / "settings.json") == [
            {"matcher": "", "hooks": [{"type": "command", "command": CMD, "timeout": 10}]}
        ]
        assert _session_start(tmp_path / ".codex" / "hooks.json")[0]["hooks"][0]["command"] == CMD
        assert (tmp_path / ".config" / "opencode" / "plugins" / "axi-cwcli.js").exists()

    def test_an_undetected_agent_is_skipped_not_created(self, tmp_path):
        """cwcli must not scatter config dirs for harnesses the user does not run."""
        outcomes = agent_hooks.install(CMD, home=_home(tmp_path, codex=False, opencode=False))

        assert _status(outcomes, "codex") == "skipped"
        assert _status(outcomes, "opencode") == "skipped"
        assert _status(outcomes, "claude-code") == "installed"
        assert not (tmp_path / ".codex").exists()
        assert not (tmp_path / ".config" / "opencode").exists()

    def test_no_agent_harness_at_all_creates_nothing(self, tmp_path):
        """`setup` on a machine with no agents is an honest all-skipped no-op."""
        outcomes = agent_hooks.install(
            CMD, home=_home(tmp_path, claude=False, codex=False, opencode=False)
        )

        assert all(o.status == "skipped" for o in outcomes)
        assert all(o.detail == "not detected" for o in outcomes)
        assert list(tmp_path.iterdir()) == []

    def test_reinstall_is_an_idempotent_no_op(self, tmp_path):
        home = _home(tmp_path)
        agent_hooks.install(CMD, home=home)
        settings = home / ".claude" / "settings.json"
        untouched = settings.read_text()

        outcomes = agent_hooks.install(CMD, home=home)

        assert all(o.status == "unchanged" for o in outcomes)
        assert settings.read_text() == untouched
        assert len(_session_start(settings)) == 1

    def test_a_moved_executable_is_repaired_in_place_not_duplicated(self, tmp_path):
        """The reinstall/relocate case: one entry, pointing at the new path."""
        home = _home(tmp_path)
        agent_hooks.install("/old/bin/cwcli axi", home=home)

        outcomes = agent_hooks.install(CMD, home=home)

        assert _status(outcomes, "claude-code") == "updated"
        groups = _session_start(home / ".claude" / "settings.json")
        assert len(groups) == 1
        assert groups[0]["hooks"][0]["command"] == CMD

    def test_foreign_hooks_and_unrelated_settings_survive(self, tmp_path):
        """We share these files with other tools; never clobber their entries."""
        home = _home(tmp_path)
        settings = home / ".claude" / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "SessionStart": [
                            {"matcher": "", "hooks": [{"type": "command", "command": "gh-axi"}]}
                        ],
                        "Stop": [{"matcher": "*", "hooks": [{"command": "notify"}]}],
                    },
                }
            )
        )

        agent_hooks.install(CMD, home=home)

        config = json.loads(settings.read_text())
        assert config["model"] == "opus"
        assert config["hooks"]["Stop"] == [{"matcher": "*", "hooks": [{"command": "notify"}]}]
        commands = [h["command"] for g in config["hooks"]["SessionStart"] for h in g["hooks"]]
        assert commands == ["gh-axi", CMD]

    def test_a_corrupt_config_is_refused_not_overwritten(self, tmp_path):
        """Mirrors the codex TOML path: a file we cannot parse is never replaced."""
        home = _home(tmp_path)
        settings = home / ".claude" / "settings.json"
        original = "{ not json"
        settings.write_text(original)

        outcomes = agent_hooks.install(CMD, home=home)  # must not raise

        assert _status(outcomes, "claude-code") == "manual"
        assert settings.read_text() == original
        claude = next(o for o in outcomes if o.agent == "claude-code")
        assert "not valid JSON" in claude.detail

    def test_a_non_object_json_config_is_refused_not_overwritten(self, tmp_path):
        """Valid JSON but the wrong shape (e.g. a top-level array) is just as unusable."""
        home = _home(tmp_path)
        settings = home / ".claude" / "settings.json"
        settings.write_text("[]")

        outcomes = agent_hooks.install(CMD, home=home)

        assert _status(outcomes, "claude-code") == "manual"
        assert settings.read_text() == "[]"

    def test_a_missing_config_still_installs_normally(self, tmp_path):
        """Absent is not the same failure mode as unreadable."""
        home = _home(tmp_path)

        outcomes = agent_hooks.install(CMD, home=home)

        assert _status(outcomes, "claude-code") == "installed"
        assert _session_start(home / ".claude" / "settings.json")[0]["hooks"][0]["command"] == CMD

    def test_a_spaced_executable_path_is_repaired_not_duplicated(self, tmp_path):
        """The advertised idempotent/repair-in-place contract must hold on a spaced path."""
        home = _home(tmp_path, codex=False, opencode=False)
        spaced_cmd = "/home/John Doe/.local/bin/cwcli axi"
        agent_hooks.install(spaced_cmd, home=home)

        outcomes = agent_hooks.install(spaced_cmd, home=home)

        assert _status(outcomes, "claude-code") == "unchanged"
        assert len(_session_start(home / ".claude" / "settings.json")) == 1


class TestCodexFeatureFlag:
    """Codex ignores hooks.json unless `[features] hooks = true`."""

    def test_missing_features_section_is_appended_preserving_comments(self, tmp_path):
        home = _home(tmp_path)
        config = home / ".codex" / "config.toml"
        config.write_text('# a comment worth keeping\nmodel = "gpt-5"\n')

        agent_hooks.install(CMD, home=home)

        text = config.read_text()
        assert "# a comment worth keeping" in text
        assert toml.loads(text)["features"]["hooks"] is True
        assert toml.loads(text)["model"] == "gpt-5"

    def test_already_enabled_is_left_alone(self, tmp_path):
        home = _home(tmp_path)
        config = home / ".codex" / "config.toml"
        config.write_text("[features]\nhooks = true\n")

        outcomes = agent_hooks.install(CMD, home=home)

        assert config.read_text() == "[features]\nhooks = true\n"
        assert _status(outcomes, "codex") == "installed"

    def test_a_features_section_without_hooks_asks_rather_than_rewriting(self, tmp_path):
        """`toml.dump` would strip the user's comments, so report instead."""
        home = _home(tmp_path)
        config = home / ".codex" / "config.toml"
        original = "[features]\n# hooks deliberately off\nweb_search = true\n"
        config.write_text(original)

        outcomes = agent_hooks.install(CMD, home=home)

        assert config.read_text() == original
        codex = next(o for o in outcomes if o.agent == "codex")
        assert codex.status == "manual"
        assert "[features]" in codex.detail

    def test_no_config_file_at_all_gets_one(self, tmp_path):
        home = _home(tmp_path)

        agent_hooks.install(CMD, home=home)

        assert (
            toml.loads((home / ".codex" / "config.toml").read_text())["features"]["hooks"] is True
        )

    def test_an_unparseable_config_asks_rather_than_overwriting(self, tmp_path):
        """We cannot reason about a TOML we cannot read; never overwrite it blind."""
        home = _home(tmp_path)
        config = home / ".codex" / "config.toml"
        config.write_text("[features\nbroken = = =\n")

        outcomes = agent_hooks.install(CMD, home=home)

        assert config.read_text() == "[features\nbroken = = =\n"
        assert _status(outcomes, "codex") == "manual"

    def test_a_config_without_a_trailing_newline_is_appended_cleanly(self, tmp_path):
        home = _home(tmp_path)
        config = home / ".codex" / "config.toml"
        config.write_text('model = "gpt-5"')  # no trailing newline

        agent_hooks.install(CMD, home=home)

        assert toml.loads(config.read_text())["features"]["hooks"] is True

    def test_a_first_time_toml_write_is_not_masked_as_unchanged(self, tmp_path):
        """hooks.json already correct but config.toml needs its first write: a
        real mutation, so the reported status must not read as a no-op."""
        home = _home(tmp_path)
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "",
                                "hooks": [{"type": "command", "command": CMD, "timeout": 10}],
                            }
                        ]
                    }
                }
            )
        )

        outcomes = agent_hooks.install(CMD, home=home)

        codex = next(o for o in outcomes if o.agent == "codex")
        assert codex.status != "unchanged"
        assert (
            toml.loads((home / ".codex" / "config.toml").read_text())["features"]["hooks"] is True
        )


class TestOpenCodePlugin:
    def test_plugin_spawns_the_binary_with_the_axi_subcommand(self, tmp_path):
        home = _home(tmp_path)

        agent_hooks.install(CMD, home=home)

        source = (home / ".config" / "opencode" / "plugins" / "axi-cwcli.js").read_text()
        assert 'const BIN = "/opt/bin/cwcli";' in source
        assert 'const ARGS = ["axi"];' in source
        assert "experimental.chat.system.transform" in source

    def test_a_stale_plugin_is_rewritten(self, tmp_path):
        home = _home(tmp_path)
        agent_hooks.install("/old/bin/cwcli axi", home=home)

        outcomes = agent_hooks.install(CMD, home=home)

        assert _status(outcomes, "opencode") == "updated"
        source = (home / ".config" / "opencode" / "plugins" / "axi-cwcli.js").read_text()
        assert "/old/bin/cwcli" not in source


class TestHookCommand:
    def test_prefers_the_bare_name_when_path_resolves_to_this_executable(
        self, tmp_path, monkeypatch
    ):
        """A global install must stay portable across machines."""
        binary = tmp_path / "cwcli"
        binary.write_text("#!/bin/sh\n")
        monkeypatch.setattr(agent_hooks.sys, "argv", [str(binary)])
        monkeypatch.setattr(agent_hooks.shutil, "which", lambda _: str(binary))

        assert agent_hooks.hook_command() == "cwcli axi"

    def test_falls_back_to_the_absolute_path_when_path_resolves_elsewhere(
        self, tmp_path, monkeypatch
    ):
        """Otherwise the hook would silently run a DIFFERENT cwcli."""
        mine = tmp_path / "mine" / "cwcli"
        mine.parent.mkdir()
        mine.write_text("#!/bin/sh\n")
        other = tmp_path / "other" / "cwcli"
        other.parent.mkdir()
        other.write_text("#!/bin/sh\n")
        monkeypatch.setattr(agent_hooks.sys, "argv", [str(mine)])
        monkeypatch.setattr(agent_hooks.shutil, "which", lambda _: str(other))

        assert agent_hooks.hook_command() == f"{mine} axi"

    def test_falls_back_when_cwcli_is_not_on_path_at_all(self, tmp_path, monkeypatch):
        binary = tmp_path / "cwcli"
        binary.write_text("#!/bin/sh\n")
        monkeypatch.setattr(agent_hooks.sys, "argv", [str(binary)])
        monkeypatch.setattr(agent_hooks.shutil, "which", lambda _: None)

        assert agent_hooks.hook_command() == f"{binary} axi"

    def test_an_empty_argv_still_yields_a_usable_command(self, monkeypatch):
        """Never write a hook command of `" axi"`; fall back to the bare name."""
        monkeypatch.setattr(agent_hooks.sys, "argv", [""])
        monkeypatch.setattr(agent_hooks.shutil, "which", lambda _: None)

        assert agent_hooks.hook_command() == "cwcli axi"

    def test_a_vanished_executable_does_not_crash_the_samefile_probe(self, tmp_path, monkeypatch):
        """`which` can name a path that is gone by the time we stat it."""
        monkeypatch.setattr(agent_hooks.sys, "argv", [str(tmp_path / "gone")])
        monkeypatch.setattr(agent_hooks.shutil, "which", lambda _: str(tmp_path / "also-gone"))

        assert agent_hooks.hook_command() == f"{tmp_path / 'gone'} axi"


class TestIsCwcliHook:
    @pytest.mark.parametrize(
        "command",
        [
            "cwcli axi",
            "/opt/bin/cwcli axi",
            "caffeinated-whale-cli axi",
            "/home/John Doe/.local/bin/cwcli axi",
            "cwcli.exe axi",
            "cwcli.EXE axi",
        ],
    )
    def test_recognises_our_own_entry_at_any_path(self, command):
        assert agent_hooks._is_cwcli_hook(command)

    @pytest.mark.parametrize(
        "command",
        [
            "gh-axi",
            "cwcli",
            "cwcli ls",
            "cwcli axi ls",
            "bash script.sh",
            "",
            "cwcli.backup axi",
        ],
    )
    def test_rejects_everything_else(self, command):
        """A false positive here would silently rewrite another tool's hook."""
        assert not agent_hooks._is_cwcli_hook(command)


class TestAxiSetupVerb:
    def test_emits_toon_and_exits_zero(self, tmp_path, monkeypatch):
        outcomes = [
            agent_hooks.HookOutcome("claude-code", "/h/.claude/settings.json", "installed"),
            agent_hooks.HookOutcome("codex", "/h/.codex", "skipped", "not detected"),
        ]
        monkeypatch.setattr(axi_mod.agent_hooks, "install", lambda: outcomes)

        result = runner.invoke(axi_mod.app, ["setup"])

        assert result.exit_code == 0
        assert "agents[2]{agent,status,path}:" in result.stdout
        assert "claude-code,installed,/h/.claude/settings.json" in result.stdout
        assert "notes[1]:" in result.stdout
        assert "codex: not detected" in result.stdout
        assert "help[2]:" in result.stdout

    def test_omits_the_notes_block_when_there_is_nothing_to_note(self, tmp_path, monkeypatch):
        outcomes = [agent_hooks.HookOutcome("claude-code", "/h/.claude", "unchanged")]
        monkeypatch.setattr(axi_mod.agent_hooks, "install", lambda: outcomes)

        result = runner.invoke(axi_mod.app, ["setup"])

        assert result.exit_code == 0
        assert "notes[" not in result.stdout
