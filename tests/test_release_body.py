"""The release card the tag push publishes (`.github/scripts/release-body.sh`).

The card is published once, by a workflow that runs only on a version tag, so
nothing exercises it until the moment it is too late to fix cheaply - which is
how it shipped a CHANGELOG link to `blob/master/`, a branch this repository does
not have, on every release from 1.0.0 onward. The link was corrected on one
published note and never in the generator, so the generator reintroduced it each
time. These tests run the generator in the ordinary `Pytest` gate instead, so a
regression is caught on the pull request rather than on the release.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "release-body.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
NOTES_DIR = REPO_ROOT / ".github" / "release-notes"

REPO = "karotkriss/caffeinated-whale-cli"


def _notes_for(version: str) -> str:
    _, minor, patch = (int(part) for part in version.split("."))
    heading = "### What's New" if minor == patch == 0 else "### What's Changed"
    return f"""{heading}

**One benefit, stated for the reader.**
One short sentence of context.
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def rendered(tmp_path):
    """Render the card in a throwaway repo with a known tag history.

    The tags are fabricated rather than read from this checkout: CI clones
    shallow and without tags, and a test that quietly stops asserting the
    compare link there would guard nothing.
    """
    repo = tmp_path / "repo"
    (repo / ".github" / "scripts").mkdir(parents=True)
    (repo / ".github" / "release-notes").mkdir(parents=True)
    (repo / ".github" / "scripts" / "release-body.sh").write_bytes(SCRIPT.read_bytes())
    (repo / ".github" / "scripts" / "release-body.sh").chmod(0o755)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "CHANGELOG.md").write_text("# Changelog\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c")
    for tag in ("v0.35.0", "v1.0.0", "v1.1.0", "v2.0.0"):
        _git(repo, "tag", tag)

    def render(
        version: str,
        notes: str | None = None,
        *,
        omit_notes: bool = False,
    ):
        if not omit_notes:
            note = _notes_for(version) if notes is None else notes
            (repo / ".github" / "release-notes" / f"v{version}.md").write_text(note)
        return subprocess.run(
            [".github/scripts/release-body.sh", version, REPO],
            cwd=repo,
            capture_output=True,
            text=True,
        )

    return render


class TestTheCardSatisfiesTheStandingRules:
    def test_whats_new_leads_a_major_release_and_installation_follows_it(self, rendered):
        body = rendered("2.0.0").stdout
        assert body.startswith("### What's New")
        assert body.index("### What's New") < body.index("### Installation")

    def test_the_body_does_not_repeat_the_name_or_version(self, rendered):
        """The release title carries both; the body heading used to repeat them."""
        generated = rendered("2.0.0").stdout.split("### Installation", 1)[1]
        assert "caffeinated-whale-cli v2.0.0" not in generated
        assert "## caffeinated-whale-cli" not in generated

    def test_installation_offers_every_route_with_the_recommended_one_first(self, rendered):
        body = rendered("2.0.0").stdout
        install = body.split("### Installation", 1)[1]
        assert install.index("uv tool install") < install.index("pip install")
        assert "uvx --from caffeinated-whale-cli" in install

    def test_the_changelog_link_is_pinned_to_the_tag_not_a_branch(self, rendered):
        """`blob/master/` was dead: this repository's default branch is `develop`."""
        body = rendered("2.0.0").stdout
        assert f"https://github.com/{REPO}/blob/v2.0.0/CHANGELOG.md" in body
        assert "blob/master" not in body
        assert "blob/develop" not in body

    def test_the_comparison_runs_from_the_previous_tag_to_this_one(self, rendered):
        """It used to read `compare/v<new>...HEAD`, which is backwards."""
        body = rendered("2.0.0").stdout
        assert body.rstrip().endswith(f"https://github.com/{REPO}/compare/v1.1.0...v2.0.0")
        assert "...HEAD" not in body

    def test_the_first_release_has_nothing_to_compare_against(self, rendered):
        body = rendered("0.35.0").stdout
        assert body.startswith("### What's Changed")
        assert "/compare/" not in body
        assert "### Installation" in body


class TestTheCardCannotBePublishedWithoutItsCopy:
    """Rule 3 asks for advertising copy, which no template can write.

    Requiring the file is what makes that rule enforceable rather than aspirational.
    """

    def test_a_missing_note_fails_the_release(self, rendered):
        result = rendered("3.0.0", omit_notes=True)
        assert result.returncode != 0
        assert "release-notes/v3.0.0.md" in result.stderr

    def test_an_empty_note_fails_the_release(self, rendered):
        assert rendered("3.0.0", notes="").returncode != 0

    def test_a_note_without_the_expected_heading_fails_the_release(self, rendered):
        result = rendered("3.0.0", notes="Some prose with no heading.\n")
        assert result.returncode != 0
        assert "What's New" in result.stderr

    def test_a_major_release_rejects_whats_changed(self, rendered):
        result = rendered("3.0.0", notes=_notes_for("3.1.0"))
        assert result.returncode != 0
        assert '"### What\'s New"' in result.stderr
        assert "major release" in result.stderr

    @pytest.mark.parametrize("version", ["1.2.0", "1.1.1"])
    def test_a_smaller_release_rejects_whats_new(self, rendered, version):
        result = rendered(version, notes=_notes_for("3.0.0"))
        assert result.returncode != 0
        assert '"### What\'s Changed"' in result.stderr
        assert "smaller release" in result.stderr

    def test_a_preface_above_the_expected_heading_fails_the_release(self, rendered):
        notes = f"Preface that must not lead the card.\n{_notes_for('3.0.0')}"
        result = rendered("3.0.0", notes=notes)
        assert result.returncode != 0
        assert '"### What\'s New"' in result.stderr


class TestTheWorkflowUsesTheGenerator:
    def test_the_workflow_writes_no_body_of_its_own(self):
        workflow = WORKFLOW.read_text()
        assert "body_path:" in workflow
        assert "blob/master" not in workflow
        assert "...HEAD" not in workflow

    def test_generated_commit_lists_stay_off(self):
        """A generated commit list is not a release card."""
        assert "generate_release_notes: true" not in WORKFLOW.read_text()

    def test_the_release_title_carries_the_project_name(self):
        expected = 'name: "caffeinated-whale-cli: v${{ steps.version.outputs.version }}"'
        assert expected in WORKFLOW.read_text()

    def test_the_body_is_composed_before_anything_is_published(self):
        """A missing note must stop the release while stopping it is still free."""
        workflow = WORKFLOW.read_text()
        assert workflow.index("- name: Compose release body") < workflow.index(
            "- name: Publish to PyPI"
        )

    def test_the_build_does_not_leave_a_gitignore_in_dist(self):
        """`dist/.gitignore` was uploaded and published as `default.gitignore`."""
        assert "uv build --no-create-gitignore" in WORKFLOW.read_text()

    def test_the_upload_still_takes_everything_in_dist(self):
        """The stray asset is fixed at its source, not hidden by a narrower glob."""
        assert "files: dist/*" in WORKFLOW.read_text()

    def test_the_notes_format_is_documented_where_a_release_author_will_look(self):
        assert (NOTES_DIR / "README.md").exists()
