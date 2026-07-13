---
name: cwcli-release
description: >
  How to cut a cwcli (caffeinated-whale-cli) release - the exactly-four version-bump files
  (pyproject.toml, __init__.py, uv.lock, CHANGELOG.md), the CHANGELOG Keep-a-Changelog format, the
  semver convention this repo practices, the develop -> vX.Y.Z tag publish path, the uv
  trusted-publishing PyPI flow in release.yml (OIDC, --check-url non-reproducible-build gotcha, why
  the old pypa action was replaced), and the one-time PyPI trusted-publisher prerequisite. Use this
  whenever you bump the cwcli version, edit CHANGELOG.md for a release, publish to PyPI, or touch
  .github/workflows/release.yml or build.yml.
---

# Cutting a cwcli release (sharp edges)

Keep the root-cause "why" so a later change does not silently re-break the release flow.

### Cutting a release

Current practice at 0.34.0 (the `uv --trusted-publishing` flow), verified against `.github/workflows/release.yml`:

- A version bump touches exactly four files: `version` in `pyproject.toml`, `__version__` in `src/caffeinated_whale_cli/__init__.py`, the project's own entry in `uv.lock` (regenerate via `uv lock`, do not hand-edit), and a new `CHANGELOG.md` section.
- Both `build.yml` and `release.yml` hard-fail when `pyproject.toml` and `__init__.py` disagree, so the two version strings must always be bumped together.
- `CHANGELOG.md` is manually maintained in Keep a Changelog format: a `## [x.y.z] - YYYY-MM-DD` section with `### Added`/`### Changed`/`### Fixed` subsections, entries shaped like ``- **`cmd` command** - description`` with indented sub-bullets, user-facing changes only (no CI/typing internals), no issue numbers.
- Version choice follows semver as this repo practices it: minor for new flags or behavior changes (0.32.0, 0.33.0, 0.34.0), patch for a narrow compatibility fix (0.31.1).
- The bump lands as a normal PR to `develop` with a `chore: bump version to x.y.z` commit.
- Publishing to PyPI is done by `release.yml`, not from a dev machine.
  Since the default branch is `develop` but the workflow's branch trigger is `master`, the working path is: merge the bump PR into `develop`, then push a `vX.Y.Z` tag pointing at the merge commit (the tag trigger fires regardless of branch).
  The workflow verifies the tag matches the package version, publishes to PyPI, and creates a GitHub release with the built artifacts.
- The publish step runs `uv publish --trusted-publishing automatic --check-url https://pypi.org/simple/` inside the job's `ghcr.io/astral-sh/uv` container.
  It authenticates via GitHub OIDC trusted publishing (the `id-token: write` permission plus the `pypi` `environment:` block in the workflow), so there is NO `PYPI_API_TOKEN` secret to manage.
  `--check-url` is NOT twine's `skip-existing`: it skips ONLY a byte-identical re-upload (content-hash dedup, for retry/parallel-upload safety within the SAME build), and a same-name but different-content upload ERRORS with `Local file and index file do not match` (exit 2).
  Because Python builds are non-reproducible, re-tagging an already-published version rebuilds a byte-different artifact and FAILS rather than skipping - so each release must be a genuinely NEW version; do not expect re-tagging an existing version to skip gracefully.
  This replaced the old `pypa/gh-action-pypi-publish@release/v1` step, which is a Docker container action and cannot run from inside a job `container:` (GitHub tries to bootstrap it via `create-docker-action.py`, which is absent in the nested-container filesystem, and dies with `[Errno 2] No such file or directory`, exit 2 - the v0.33.0 tag never reached PyPI for this reason).
- One-time prerequisite (captain-only, not automatable in CI): a PyPI **trusted publisher** must be registered for this project on pypi.org - owner `karotkriss`, repo `caffeinated-whale-cli`, workflow `release.yml`, environment `pypi`.
  Trusted publishing fails with an auth error until that publisher exists; CI cannot create it.
