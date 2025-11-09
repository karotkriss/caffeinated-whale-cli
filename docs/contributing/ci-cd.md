# CI/CD Workflows

This guide covers the automated GitHub Actions workflows for caffeinated-whale-cli.

## Overview

We use three GitHub Actions workflows:

| Workflow | Triggers | Purpose |
|----------|----------|---------|
| **Lint** | All branches, all PRs | Code quality checks (Black, Ruff, mypy) |
| **Build** | Push to `master` | Build package, verify version consistency |
| **Release** | Tags `v*.*.*` | Publish to PyPI, create GitHub release |

All workflows use the `ghcr.io/astral-sh/uv:python3.12-bookworm` Docker image.

---

## Workflows

### Lint (`.github/workflows/lint.yml`)

Runs on every push and PR to ensure code quality.

**Checks:**
- Black formatting (`uv run black --check src/`)
- Ruff linting (`uv run ruff check src/`)
- mypy type checking (`uv run mypy src/`)

**Run locally:**
```bash
uv run black --check src/
uv run ruff check src/
uv run mypy src/
```

See [Code Quality Guide](./code-quality.md) for details.

---

### Build (`.github/workflows/build.yml`)

Runs on push to `master` branch to verify the package builds correctly.

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

**Steps:**
1. Extract and verify version from `__init__.py`, `pyproject.toml`, and git tag
2. Build package (`uv build`)
3. Publish to PyPI
4. Create GitHub release (if tag-triggered)

**Trigger a release:**
```bash
# 1. Update versions
vim src/caffeinated_whale_cli/__init__.py  # __version__ = "0.10.0"
vim pyproject.toml                          # version = "0.10.0"

# 2. Commit and tag
git add .
git commit -m "chore: bump version to 0.10.0"
git tag v0.10.0
git push origin master --tags
```

See [Chores Guide](./chores.md) for the full release process.

---

## Setup (First Time)

### Required: PyPI API Token

1. Create token at https://pypi.org/manage/account/token/
2. Add to GitHub: **Settings > Secrets and variables > Actions > Secrets**
3. Name: `PYPI_API_TOKEN`
4. Value: `pypi-AgEI...` (your token)

### Optional: GitHub Environment

Create a `pypi` environment for additional protection:

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

1. Create TestPyPI account at https://test.pypi.org
2. Create token and add as `TEST_PYPI_API_TOKEN` secret
3. Create `testpypi` environment in GitHub
4. Manually trigger release workflow with testpypi environment
5. Verify: `pip install --index-url https://test.pypi.org/simple/ caffeinated-whale-cli`

---

## Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [uv Documentation](https://docs.astral.sh/uv/)
- [PyPI Publishing Guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [Workflows README](../../.github/workflows/README.md)
