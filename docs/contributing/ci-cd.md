# CI/CD Workflows

This guide covers the automated GitHub Actions workflows for caffeinated-whale-cli.

## Overview

We use five GitHub Actions workflows:

| Workflow | Triggers | Purpose |
|----------|----------|---------|
| **Lint** | All branches, all PRs | Code quality checks (Black, Ruff) |
| **Test** | All branches, all PRs | Run the fast `unit` pytest tier (required gate) + mypy (zero-error gate) |
| **E2E** | PRs into `develop`/`master`, `e2e`-labeled PRs, manual dispatch | Run the real-Docker `e2e` tier on a v14/v15/v16 Frappe matrix |
| **Build** | Push to `master`, manual dispatch | Build package, verify version consistency |
| **Release** | Tags `v*.*.*`, push to `master`, published releases, manual dispatch | Publish to PyPI, create GitHub release |

Lint, Test, Build, and Release run inside the `ghcr.io/astral-sh/uv:python3.12-bookworm` Docker image. E2E runs directly on the `ubuntu-latest` host runner (no `container:`) so `docker`/`docker compose` can reach the runner's own daemon; it installs `uv` via `astral-sh/setup-uv` instead.

---

## Workflows

### Lint (`.github/workflows/lint.yml`)

Runs on every push and PR to ensure code quality.

**Checks:**
- Black formatting (`uv run black --check src/`)
- Ruff linting (`uv run ruff check src/`)

**Run locally:**
```bash
uv run black --check src/
uv run ruff check src/
```

See [Code Quality Guide](./code-quality.md) for details.

---

### Test (`.github/workflows/test.yml`)

Runs on every push and PR. Has two jobs:

- **Pytest** - runs the fast `unit` tier with coverage (`uv run pytest -m unit --cov=caffeinated_whale_cli --cov-report=term-missing`). This is the always-required gate; it needs no Docker daemon and runs inside the uv container. Because `develop` has no branch protection, an admin must tick `Pytest` as a required status check in the `develop` branch-protection settings for it to actually block merges.
- **Mypy** - runs `uv run mypy src/` as a zero-error gate. The historical ~50 errors across ~14 files were burned down to zero and `continue-on-error` was dropped from the step, so any new type error fails the job's status check. To make it *required to merge*, an admin must also tick `Mypy` as a required status check in the `develop` branch-protection settings (same outstanding step as `Pytest`).

**Run locally:**
```bash
uv run pytest -m unit --cov=caffeinated_whale_cli --cov-report=term-missing
uv run mypy src/
```

---

### E2E (`.github/workflows/e2e.yml`)

Runs the real-Docker `e2e` tier (`tests/e2e/`) against genuine throwaway Frappe instances, driving the real `cwcli` binary. See [tests/README.md](../../tests/README.md#e2e-harness-real-docker) for the harness itself.

- **Trigger gate:** a manual dispatch, a PR carrying the `e2e` label, or a PR whose base branch is `develop`/`master`. `develop`/`master` have no branch protection today, so this is not yet a *required* merge gate - an admin must enable branch protection and tick the `E2E (frappe vNN)` checks as required for that to happen. Until then it runs informationally (or on-demand via the label) and the unit tier is the only gate that blocks a merge.
- **Runner:** `ubuntu-latest` host runner (no `container:`), one job per `strategy.matrix.frappe: [14, 15, 16]` leg, `fail-fast: false` so a leaky instance on one leg can't cancel another.
- **Steps:** authenticate to Docker Hub when creds are configured (dodges the anonymous-pull rate limit on the multi-GB `frappe/bench` image), a preflight upstream-reachability check (annotates an upstream/infra break distinctly from a real assertion failure), `uv sync --frozen --all-extras`, `uv run pytest tests/e2e -m e2e -o addopts=""`, then an unconditional `always()` teardown step that sweeps any leaked `cwe2e-` resources and prunes the runner's Docker state.
- **Per-job `timeout-minutes`:** ~45 (a full `cwcli init` - image pull + bench init + new-site - is the dominant cost).

**Run locally** (needs a reachable Docker daemon; installs `pexpect` via the `e2e` extra):
```bash
uv sync --all-extras
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m e2e
```

---

### Build (`.github/workflows/build.yml`)

Runs on push to the `master` branch (and on manual dispatch) to verify the package builds correctly.

**Steps:**
1. Verify version consistency (`__init__.py` ↔ `pyproject.toml`)
2. Build wheel and sdist (`uv build`)
3. Upload artifacts (30-day retention)

**Run locally:**
```bash
uv build
ls -lh dist/
```

---

### Release (`.github/workflows/release.yml`)

Publishes package to PyPI when a version tag is pushed.
It also triggers on pushes to `master`, on published GitHub releases, and on manual dispatch.

**Steps:**
1. Extract and verify version from `__init__.py`, `pyproject.toml`, and git tag
2. Build package (`uv build`)
3. Publish to PyPI (`uv publish --trusted-publishing automatic --check-url https://pypi.org/simple/`, authenticated over GitHub OIDC - no token secret)
4. Create GitHub release (if tag-triggered)

**Trigger a release:**
```bash
# 1. Bump the version (four files move together)
vim pyproject.toml                          # version = "0.10.0"
vim src/caffeinated_whale_cli/__init__.py   # __version__ = "0.10.0"
uv lock                                     # refresh the project's own entry in uv.lock
vim CHANGELOG.md                            # add the "## [0.10.0] - YYYY-MM-DD" section

# 2. Land the bump as a normal PR to develop
git add pyproject.toml src/caffeinated_whale_cli/__init__.py uv.lock CHANGELOG.md
git commit -m "chore: bump version to 0.10.0"
# push the branch, open a PR, merge into develop

# 3. Tag the merge commit (the tag trigger fires regardless of branch)
git tag v0.10.0
git push origin v0.10.0
```

See [Chores Guide](./chores.md) for the full release process.

---

## Setup (First Time)

### Required: PyPI Trusted Publisher

The release workflow publishes with `uv publish --trusted-publishing automatic`, authenticating over GitHub OIDC - there is **no** `PYPI_API_TOKEN` secret to manage. Register a [trusted publisher](https://docs.pypi.org/trusted-publishers/) for the project on PyPI (**Manage project > Publishing**):

- Owner: `karotkriss`
- Repository: `caffeinated-whale-cli`
- Workflow: `release.yml`
- Environment: `pypi`

Until this publisher is registered, `uv publish` fails with an auth error; CI cannot create it (captain-only, one-time step).

### Required: GitHub Environment

Trusted publishing binds the OIDC token to a deployment environment, so the workflow runs in a `pypi` environment whose name must match the publisher's `Environment` above:

**Settings > Environments > New environment: `pypi`**

**Protection rules:**
- ☑ Required reviewers (optional)
- ☑ Deployment branches: `master` only

**Benefits:**
- Manual approval before publishing
- Deployment history tracking
- Branch restrictions

---

## Common Issues

### "lockfile needs to be updated"

**Cause:** Added dependency to `pyproject.toml` but didn't update `uv.lock`

**Fix:**
```bash
uv sync --all-extras
git add uv.lock
git commit -m "build: update lock file"
```

---

### "Version mismatch"

**Cause:** `__init__.py` and `pyproject.toml` have different versions

**Fix:**
```bash
# Check versions
grep '__version__' src/caffeinated_whale_cli/__init__.py
grep '^version' pyproject.toml

# Update both to match
```

---

### "command not found: ruff"

**Cause:** Workflow missing `--all-extras` flag

**Fix:** Ensure workflow uses:
```yaml
run: uv sync --frozen --all-extras
```

---

## Testing CI

### Test Lint Workflow

```bash
git checkout -b test-ci
echo "# test" >> README.md
git add . && git commit -m "test: ci workflow"
git push origin test-ci
# Create PR → Lint runs automatically
```

### Test Build Workflow

```bash
git checkout master
git merge test-ci
git push origin master
# Check Actions tab for build
```

### Test Release (Use TestPyPI First)

The release workflow hardcodes the PyPI publish/check URLs, so a real TestPyPI dry run means temporarily pointing `uv publish` at TestPyPI on a branch:

1. Create a TestPyPI account at https://test.pypi.org
2. Register a TestPyPI trusted publisher for the project (owner `karotkriss`, repo `caffeinated-whale-cli`, workflow `release.yml`, environment `testpypi`) - trusted publishing, no token secret
3. On a branch, point the publish step at TestPyPI (`uv publish --trusted-publishing automatic --publish-url https://test.pypi.org/legacy/ --check-url https://test.pypi.org/simple/`) and create a matching `testpypi` environment
4. Manually dispatch the release workflow
5. Verify: `pip install --index-url https://test.pypi.org/simple/ caffeinated-whale-cli`

---

## Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [uv Documentation](https://docs.astral.sh/uv/)
- [PyPI Publishing Guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [Workflows README](../../.github/workflows/README.md)
