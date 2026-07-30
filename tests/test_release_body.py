"""The release card the tag push publishes (`.github/scripts/release-body.sh`).

The card is published once, by a workflow that runs only on a version tag, so
nothing exercises it until the moment it is too late to fix cheaply - which is
how it shipped a CHANGELOG link to `blob/master/`, a branch this repository does
not have, on every release from 1.0.0 onward. The link was corrected on one
published note and never in the generator, so the generator reintroduced it each
time. These tests run the generator in the ordinary `Pytest` gate instead, so a
regression is caught on the pull request rather than on the release.

The card format itself changed for v2.1.0: from a cwcli-specific "What's
Changed" advertising-copy shape with a `<!-- flagship -->` marker and a
generated Installation/CHANGELOG-link footer, to Chris's cross-project
release-notes template (a generated `## [version](diff url) (date)` header
over hand-written Upgrade Steps / Breaking Changes / New Features / Bug Fixes
/ Performance Improvements / Other Changes sections, each omitted entirely
when unused). These tests cover the new contract only; the old flagship/footer
behavior is gone, not preserved alongside it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "release-body.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
NOTES_DIR = REPO_ROOT / ".github" / "release-notes"

REPO = "karotkriss/caffeinated-whale-cli"

DATE_RE = r"\d{4}-\d{2}-\d{2}"


def _default_notes() -> str:
    return """> A short description of the release.

### Bug Fixes

* Fixed something.
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
        tags: tuple[str, ...] | None = None,
    ):
        if tags is not None:
            existing_tags = subprocess.run(
                ["git", "tag", "--list"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if existing_tags:
                _git(repo, "tag", "--delete", *existing_tags)
            for tag in tags:
                _git(repo, "tag", tag)
        if not omit_notes:
            note = _default_notes() if notes is None else notes
            (repo / ".github" / "release-notes" / f"v{version}.md").write_text(note)
        return subprocess.run(
            [".github/scripts/release-body.sh", version, REPO],
            cwd=repo,
            capture_output=True,
            text=True,
        )

    return render


class TestTheHeaderIsGenerated:
    def test_the_header_links_the_version_to_the_previous_release(self, rendered):
        body = rendered("2.0.0").stdout
        first_line = body.splitlines()[0]
        expected_prefix = f"## [2.0.0](https://github.com/{REPO}/compare/v1.1.0...v2.0.0) ("
        assert first_line.startswith(expected_prefix)
        assert re.fullmatch(rf"{re.escape(expected_prefix)}{DATE_RE}\)", first_line)

    def test_the_first_release_has_no_compare_link(self, rendered):
        body = rendered("0.35.0", tags=("v0.35.0",)).stdout
        first_line = body.splitlines()[0]
        assert re.fullmatch(rf"## \[0\.35\.0\] \({DATE_RE}\)", first_line)
        assert "compare" not in first_line

    def test_preview_before_the_current_tag_exists_uses_the_previous_release(self, rendered):
        body = rendered("2.1.0").stdout
        first_line = body.splitlines()[0]
        expected_prefix = f"## [2.1.0](https://github.com/{REPO}/compare/v2.0.0...v2.1.0) ("
        assert first_line.startswith(expected_prefix)

    def test_preview_after_the_current_tag_exists_uses_the_same_previous_release(
        self, rendered
    ):
        body = rendered(
            "2.1.0",
            tags=("v0.35.0", "v1.0.0", "v1.1.0", "v2.0.0", "v2.1.0"),
        ).stdout
        first_line = body.splitlines()[0]
        expected_prefix = f"## [2.1.0](https://github.com/{REPO}/compare/v2.0.0...v2.1.0) ("
        assert first_line.startswith(expected_prefix)

    def test_the_body_does_not_repeat_the_name_or_version(self, rendered):
        """The release title carries both; the body must not repeat them."""
        body = rendered("2.0.0").stdout
        generated = "\n".join(body.splitlines()[1:])
        assert "caffeinated-whale-cli v2.0.0" not in generated
        assert "## caffeinated-whale-cli" not in generated

    def test_the_hand_written_note_follows_the_header_verbatim(self, rendered):
        notes = _default_notes()
        body = rendered("2.0.0", notes=notes).stdout
        assert body.endswith(notes)


class TestSectionsAreOmittedWhenUnused:
    def test_a_section_not_written_does_not_appear(self, rendered):
        notes = """> Only a bug fix this time.

### Bug Fixes

* Fixed the thing.
"""
        body = rendered("1.1.0", notes=notes).stdout
        for heading in (
            "### Upgrade Steps",
            "### Breaking Changes",
            "### New Features",
            "### Performance Improvements",
            "### Other Changes",
        ):
            assert heading not in body
        assert "### Bug Fixes" in body

    def test_all_six_sections_can_appear_together(self, rendered):
        notes = """> A release with everything.

### Upgrade Steps

* [ACTION REQUIRED] Re-run the migration.

### Breaking Changes

* The old flag is gone.

### New Features

* A new command.

### Bug Fixes

* A fixed bug.

### Performance Improvements

* A faster path.

### Other Changes

* Updated docs.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode == 0
        for heading in (
            "### Upgrade Steps",
            "### Breaking Changes",
            "### New Features",
            "### Bug Fixes",
            "### Performance Improvements",
            "### Other Changes",
        ):
            assert heading in result.stdout

    def test_an_empty_section_before_a_populated_section_is_rejected(self, rendered):
        notes = """> A release with one empty section.

### Bug Fixes

### Other Changes

* Updated docs.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode != 0
        assert "empty section: ### Bug Fixes" in result.stderr

    def test_adding_a_bullet_to_the_section_allows_it_to_publish(self, rendered):
        notes = """> A release with populated sections.

### Bug Fixes

* Fixed the thing.

### Other Changes

* Updated docs.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode == 0
        assert notes in result.stdout


class TestHeadingsAreValidated:
    def test_an_unrecognised_heading_fails_the_release(self, rendered):
        notes = """> Typo'd heading.

### Bugfixes

* Something.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode != 0
        assert "unrecognised heading" in result.stderr
        assert "### Bugfixes" in result.stderr

    def test_wrong_case_is_also_rejected(self, rendered):
        notes = """> Wrong case.

### bug fixes

* Something.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode != 0
        assert "unrecognised heading" in result.stderr


class TestUpgradeStepsRequiresTheActionFlag:
    def test_an_unflagged_bullet_fails_the_release(self, rendered):
        notes = """> Needs a migration.

### Upgrade Steps

* Run the migration script.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode != 0
        assert "ACTION REQUIRED" in result.stderr

    def test_a_flagged_bullet_publishes(self, rendered):
        notes = """> Needs a migration.

### Upgrade Steps

* [ACTION REQUIRED] Run the migration script.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode == 0
        assert "[ACTION REQUIRED] Run the migration script." in result.stdout

    def test_a_later_section_is_unaffected_by_the_flag_rule(self, rendered):
        notes = """> Needs a migration.

### Upgrade Steps

* [ACTION REQUIRED] Run the migration script.

### Bug Fixes

* An ordinary bullet with no flag needed here.
"""
        result = rendered("1.1.0", notes=notes)
        assert result.returncode == 0
        assert "An ordinary bullet with no flag needed here." in result.stdout


class TestTheCardCannotBePublishedWithoutItsCopy:
    def test_a_missing_note_fails_the_release(self, rendered):
        result = rendered("3.0.0", omit_notes=True)
        assert result.returncode != 0
        assert "release-notes/v3.0.0.md" in result.stderr

    def test_an_empty_note_fails_the_release(self, rendered):
        assert rendered("3.0.0", notes="").returncode != 0


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

    def test_git_safe_directory_is_registered_before_any_step_that_could_run_git(self):
        """release-body.sh runs git tag/rev-parse in a run: step inside this
        job's container. actions/checkout only registers a safe.directory
        exception around its own git calls (and again during its post-job
        cleanup), never for later run: steps, so the first step to invoke git
        itself hits git's "detected dubious ownership" refusal (exit 128) -
        the tag push that shipped v2.1.0 failed exactly this way. The fix must
        run before the step that needs it.
        """
        workflow = WORKFLOW.read_text()
        assert "safe.directory" in workflow
        checkout_index = workflow.index("- name: Checkout code")
        safe_directory_index = workflow.index("safe.directory")
        compose_body_index = workflow.index("- name: Compose release body")
        assert checkout_index < safe_directory_index < compose_body_index
