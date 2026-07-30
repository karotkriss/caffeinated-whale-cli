---
name: cwcli-release
description: >
  How to cut a cwcli (caffeinated-whale-cli) release - the exactly-four version-bump files
  (pyproject.toml, __init__.py, uv.lock, CHANGELOG.md), the CHANGELOG Keep-a-Changelog format, the
  semver convention this repo practices, the develop -> vX.Y.Z tag publish path, the uv publish PyPI
  flow in release.yml (the UV_PUBLISH_TOKEN prerequisite, --check-url non-reproducible-build gotcha,
  why the old pypa action was replaced), and the hand-written release-card copy the workflow requires
  before it will publish. Use this whenever you bump the cwcli version, edit CHANGELOG.md for a
  release, write a release note, publish to PyPI, or touch .github/workflows/release.yml,
  .github/scripts/release-body.sh, .github/release-notes/, or build.yml.
metadata:
  internal: true
---

# Cutting a cwcli release (sharp edges)

Keep the root-cause "why" so a later change does not silently re-break the release flow.

### Cutting a release

Current practice (the `UV_PUBLISH_TOKEN` flow), verified against `.github/workflows/release.yml`:

- A version bump touches exactly four files: `version` in `pyproject.toml`, `__version__` in `src/caffeinated_whale_cli/__init__.py`, the project's own entry in `uv.lock` (regenerate via `uv lock`, do not hand-edit), and a new `CHANGELOG.md` section.
- Both `build.yml` and `release.yml` hard-fail when `pyproject.toml` and `__init__.py` disagree, so the two version strings must always be bumped together.
- `CHANGELOG.md` is manually maintained in Keep a Changelog format: a `## [x.y.z] - YYYY-MM-DD` section with `### Added`/`### Changed`/`### Fixed` subsections, entries shaped like ``- **`cmd` command** - description`` with indented sub-bullets, user-facing changes only (no CI/typing internals), no issue numbers.
- Version choice follows semver as this repo practices it: minor for new flags or behavior changes (0.32.0, 0.33.0, 0.34.0), patch for a narrow compatibility fix (0.31.1).
- The bump lands as a normal PR to `develop` with a `chore: bump version to x.y.z` commit.
- Publishing to PyPI is done by `release.yml`, not from a dev machine.
  Merge the bump PR into the default `develop` branch, then push a `vX.Y.Z` tag pointing at the merge commit; the tag trigger fires regardless of branch.
  The workflow verifies the tag matches the package version, publishes to PyPI, and creates a GitHub release with the built artifacts.
- **The release card's copy is written by hand, in the repo, BEFORE the tag is pushed** - `.github/release-notes/v<version>.md`.
  It is Chris's cross-project release-notes template (`## [<version>](<diff url>) (<date>)`, then `> Description`, then whichever of Upgrade Steps / Breaking Changes / New Features / Bug Fixes / Performance Improvements / Other Changes actually apply - never an empty section), not a cwcli-specific "What's Changed" shape; `.github/release-notes/README.md` owns the full format and rules.
  This replaced the older cwcli-specific card format (a `### What's Changed`/`### What's New` heading, hand-written advertising copy, a `<!-- flagship -->` marker, and a generated Installation/CHANGELOG-link footer) as of the v2.1.0 release, on the standing rule that every project's release notes use the same template going forward - see the global agent instructions' "Release notes" section. `tests/test_release_body.py`'s history (`TestTheFlagshipMarker`, the old `TestTheCardSatisfiesTheStandingRules`) documents what the old format guarded; those tests were rewritten for the new one rather than kept as dead weight.
  `.github/scripts/release-body.sh` combines that note with a generated header (version, diff link, release date) and validates the section headings and the `[ACTION REQUIRED]` flag on Upgrade Steps entries; the workflow composes the result BEFORE the build and publish, so an invalid note stops the release before a version is consumed on PyPI.
  The split is load-bearing: a template cannot write the actual copy, and a generated commit list is not a release card - which is why `generate_release_notes` is off and the workflow's old hardcoded body is gone.
  That old hardcoded body once pointed the CHANGELOG link at `blob/master/`, a branch this repo does not have; the owner corrected it on the published 1.0.0 note but not in the generator, so every later release regenerated the dead link. Fix the generator, not the output.
  Render before tagging with `.github/scripts/release-body.sh <version> karotkriss/caffeinated-whale-cli`.
- This repo never mentions the unreleased Console/`serve` command, or any GUI, in a release note - see `AGENTS.md`'s "Console" entry. It ships unreachable, and advertising an unreachable command would be worse than saying nothing.
- **A tag push triggers `release.yml`'s container job, which is the first (and so far only) job in this repo to run `git` directly inside a `run:` step** (`release-body.sh`'s `git tag --list`/`git rev-parse` calls, used to find the previous release tag). `actions/checkout` only registers the workspace as a safe directory around its own git calls and again in its post-job cleanup - never for later `run:` steps - so a container job whose checked-out files are owned by a different UID than the one `run:` steps execute as hits git's "detected dubious ownership" refusal (exit 128) the first time any step tries to run git itself. The fix is a dedicated "Configure git safe directory" step (`git config --global --add safe.directory "$GITHUB_WORKSPACE"`) placed immediately after checkout and before any git-invoking step; if a future change adds another git call to this job, or to `build.yml`/`lint.yml`/`test.yml`'s container jobs, it needs the same registration first.
  A tag that already exists on the remote when a fix like this lands cannot simply be re-pushed (the ref exists; nothing re-triggers) and re-running the OLD failed workflow run replays the exact unfixed YAML (GitHub pins a run's workflow definition to the commit that triggered it) - so recovering a tag that failed for an environment reason, with nothing actually published under it yet (verify against PyPI and the GitHub releases API, not the workflow's exit code), means deleting and recreating the tag against the commit that carries the fix.
- `uv build` runs with `--no-create-gitignore`: uv otherwise writes a `.gitignore` into `dist/`, which the release upload glob swept up and GitHub published as an asset named `default.gitignore` (it renames leading-dot uploads). Every release from v0.35.0 to v2.0.0 carries that stray asset. Do not "fix" this by narrowing the upload glob - keep `dist/` holding only distributables.
- The publish step runs `uv publish --check-url https://pypi.org/simple/` inside the job's `ghcr.io/astral-sh/uv` container.
  It authenticates with the `UV_PUBLISH_TOKEN` secret, a PyPI API token, since commit `1fbc555` - so it uses the token and does not attempt trusted publishing, even though the `id-token: write` permission and the `pypi` `environment:` block that OIDC needed are both still present.
  `--check-url` is NOT twine's `skip-existing`: it skips ONLY a byte-identical re-upload (content-hash dedup, for retry/parallel-upload safety within the SAME build), and a same-name but different-content upload ERRORS with `Local file and index file do not match` (exit 2).
  Because Python builds are non-reproducible, re-tagging an already-published version rebuilds a byte-different artifact and FAILS rather than skipping - so each release must be a genuinely NEW version; do not expect re-tagging an existing version to skip gracefully.
  This replaced the old `pypa/gh-action-pypi-publish@release/v1` step, which is a Docker container action and cannot run from inside a job `container:` (GitHub tries to bootstrap it via `create-docker-action.py`, which is absent in the nested-container filesystem, and dies with `[Errno 2] No such file or directory`, exit 2 - the v0.33.0 tag never reached PyPI for this reason).
- One-time prerequisite (captain-only, not automatable in CI): the `UV_PUBLISH_TOKEN` repository secret must hold a valid PyPI API token for this project; publishing fails with an auth error otherwise and CI cannot create it.
  If the flow is ever moved back to OIDC, the prerequisite becomes a PyPI **trusted publisher** registered for owner `karotkriss`, repo `caffeinated-whale-cli`, workflow `release.yml`, environment `pypi`.
