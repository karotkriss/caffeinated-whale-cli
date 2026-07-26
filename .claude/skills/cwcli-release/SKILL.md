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
  `.github/release-notes/README.md` owns the format and all five card rules.
  `.github/scripts/release-body.sh` combines that note with the mechanical footer, and the workflow composes the result BEFORE the build and publish, so an invalid note stops the release before a version is consumed on PyPI.
  The split is load-bearing: a template cannot write advertising copy, and a generated commit list is not a release card - which is why `generate_release_notes` is off and the workflow's old hardcoded body is gone.
  That body pointed the CHANGELOG link at `blob/master/`, a branch this repo does not have; the owner corrected it on the published 1.0.0 note but not in the generator, so every later release regenerated the dead link. Fix the generator, not the output.
  Render before tagging with `.github/scripts/release-body.sh <version> karotkriss/caffeinated-whale-cli`.
- Before authoring a flagship entry, follow `.github/release-notes/README.md`'s "Naming the flagship entry" section.
  It owns the marker format and validation contract; regression coverage is `tests/test_release_body.py::TestTheFlagshipMarker`.
- `uv build` runs with `--no-create-gitignore`: uv otherwise writes a `.gitignore` into `dist/`, which the release upload glob swept up and GitHub published as an asset named `default.gitignore` (it renames leading-dot uploads). Every release from v0.35.0 to v2.0.0 carries that stray asset. Do not "fix" this by narrowing the upload glob - keep `dist/` holding only distributables.
- The publish step runs `uv publish --check-url https://pypi.org/simple/` inside the job's `ghcr.io/astral-sh/uv` container.
  It authenticates with the `UV_PUBLISH_TOKEN` secret, a PyPI API token, since commit `1fbc555` - so it uses the token and does not attempt trusted publishing, even though the `id-token: write` permission and the `pypi` `environment:` block that OIDC needed are both still present.
  `--check-url` is NOT twine's `skip-existing`: it skips ONLY a byte-identical re-upload (content-hash dedup, for retry/parallel-upload safety within the SAME build), and a same-name but different-content upload ERRORS with `Local file and index file do not match` (exit 2).
  Because Python builds are non-reproducible, re-tagging an already-published version rebuilds a byte-different artifact and FAILS rather than skipping - so each release must be a genuinely NEW version; do not expect re-tagging an existing version to skip gracefully.
  This replaced the old `pypa/gh-action-pypi-publish@release/v1` step, which is a Docker container action and cannot run from inside a job `container:` (GitHub tries to bootstrap it via `create-docker-action.py`, which is absent in the nested-container filesystem, and dies with `[Errno 2] No such file or directory`, exit 2 - the v0.33.0 tag never reached PyPI for this reason).
- One-time prerequisite (captain-only, not automatable in CI): the `UV_PUBLISH_TOKEN` repository secret must hold a valid PyPI API token for this project; publishing fails with an auth error otherwise and CI cannot create it.
  If the flow is ever moved back to OIDC, the prerequisite becomes a PyPI **trusted publisher** registered for owner `karotkriss`, repo `caffeinated-whale-cli`, workflow `release.yml`, environment `pypi`.
