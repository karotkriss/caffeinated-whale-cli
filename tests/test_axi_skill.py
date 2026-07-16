"""The installable Agent Skill (`skills/cwcli/SKILL.md`) and its staleness gate.

The skill is static and generated. Its whole risk is DRIFT: a skill that teaches
a surface that has moved is worse than no skill, because an agent believes it.
The AXI standard asks for a `--check` build step in CI for exactly this; running
it as a unit test means the existing `Pytest` gate already blocks a stale skill,
with no extra workflow step to maintain.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "cwcli" / "SKILL.md"


def _build_skill():
    """Import `scripts/build_skill.py`, which is outside the package."""
    spec = importlib.util.spec_from_file_location(
        "build_skill", REPO_ROOT / "scripts" / "build_skill.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_skill"] = module
    spec.loader.exec_module(module)
    return module


build_skill = _build_skill()


class TestSkillIsCommittedAndFresh:
    def test_the_committed_skill_is_not_stale(self):
        """The `--check` gate. If this fails, run `uv run python scripts/build_skill.py`."""
        assert SKILL_PATH.exists(), f"{SKILL_PATH} is missing; run scripts/build_skill.py"
        assert (
            SKILL_PATH.read_text(encoding="utf-8") == build_skill.render()
        ), "SKILL.md is stale. Run `uv run python scripts/build_skill.py` and commit the result."

    def test_check_mode_reports_stale_and_fresh(self, monkeypatch, tmp_path):
        monkeypatch.setattr(build_skill.sys, "argv", ["build_skill.py", "--check"])

        stale = tmp_path / "SKILL.md"
        stale.write_text("out of date")
        monkeypatch.setattr(build_skill, "SKILL_PATH", stale)
        assert build_skill.main() == 1

        stale.write_text(build_skill.render())
        assert build_skill.main() == 0

    def test_check_mode_reports_a_missing_skill(self, monkeypatch, tmp_path):
        monkeypatch.setattr(build_skill.sys, "argv", ["build_skill.py", "--check"])
        monkeypatch.setattr(build_skill, "SKILL_PATH", tmp_path / "absent" / "SKILL.md")

        assert build_skill.main() == 1

    def test_build_mode_writes_the_file(self, monkeypatch, tmp_path):
        target = tmp_path / "nested" / "SKILL.md"
        monkeypatch.setattr(build_skill.sys, "argv", ["build_skill.py"])
        monkeypatch.setattr(build_skill, "SKILL_PATH", target)

        assert build_skill.main() == 0
        assert target.read_text() == build_skill.render()


class TestVerbTableTracksTheRealApp:
    """The generated half. Hand-maintaining this list is what the gate removes."""

    def test_every_registered_axi_verb_appears(self):
        from caffeinated_whale_cli.commands import axi

        names = [c.name for c in axi.app.registered_commands]
        assert "setup" in names  # sanity: the registry is what we think it is
        skill = SKILL_PATH.read_text(encoding="utf-8")
        for name in names:
            assert f"\n  {name} " in skill or f"\n  {name}  " in skill, f"{name} missing from skill"

    def test_grouped_subcommands_are_qualified(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        assert "apps list" in skill
        assert "apps update" in skill

    def test_a_new_verb_makes_the_committed_skill_stale(self):
        """The gate's actual job, driven rather than asserted in prose."""
        rendered = build_skill.render()
        assert SKILL_PATH.read_text(encoding="utf-8") == rendered

        real_rows = build_skill._verb_rows
        try:
            build_skill._verb_rows = lambda: real_rows() + [("teleport", "Teleport the bench.")]
            assert build_skill.render() != rendered
            assert "teleport" in build_skill.render()
        finally:
            build_skill._verb_rows = real_rows

    def test_rst_double_backticks_are_collapsed_for_markdown(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        verbs = skill.split("## Verbs")[1].split("```")[1]
        assert "``" not in verbs


class TestSkillContent:
    def test_frontmatter_is_trigger_shaped(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        assert skill.startswith("---\nname: cwcli\n")
        assert "description:" in skill
        # The trigger must fire on the INTENT, not on the tool's name - an agent
        # that has never heard of cwcli would never think to say "cwcli".
        assert "Frappe" in build_skill.SKILL_DESCRIPTION
        assert "Use whenever" in build_skill.SKILL_DESCRIPTION

    def test_commands_are_runnable_without_a_global_install(self):
        """A skill may be installed on a machine with no cwcli on PATH."""
        skill = SKILL_PATH.read_text(encoding="utf-8")
        assert "uvx --from caffeinated-whale-cli cwcli" in skill

    def test_carries_no_live_state(self):
        """A skill is static; live state is the hook's job alone.

        The illustrative TOON block is fine - what must never appear is a real
        project name or a path from the machine that generated the file.
        """
        skill = SKILL_PATH.read_text(encoding="utf-8")
        for leaked in ("cwe2e-", "/home/", str(Path.home())):
            assert leaked not in skill

    @pytest.mark.parametrize("absent", ["axi apps install", "axi apps uninstall", "axi restore"])
    def test_documents_the_deliberately_absent_verbs(self, absent):
        """Captain-locked deferrals; an agent must not be nudged to hunt for them."""
        assert absent in SKILL_PATH.read_text(encoding="utf-8")

    def test_description_matches_the_cli_home(self):
        """Single source of truth: the skill cannot drift from what the CLI says."""
        from caffeinated_whale_cli.commands import axi

        assert axi._DESCRIPTION in SKILL_PATH.read_text(encoding="utf-8")
