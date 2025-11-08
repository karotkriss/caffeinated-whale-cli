# GitHub Actions Workflows Implementation

## Overview

Implemented three GitHub Actions workflows using official `astral-sh/uv` Docker images for optimal performance and reproducibility.

## Workflows Created

### 1. **Lint Workflow** (`.github/workflows/lint.yml`)

**Triggers:**
- ✅ All branches (push)
- ✅ All PRs

**Purpose:**
- Code formatting check with Black
- Linting with Ruff
- Type checking with mypy

**Container:** `ghcr.io/astral-sh/uv:python3.12-bookworm`

**Key Features:**
- Runs on every push and PR (including drafts)
- Fast feedback (~30-60s)
- Continues on type check errors (non-blocking)

---

### 2. **Build Workflow** (`.github/workflows/build.yml`)

**Triggers:**
- ✅ Push to `master` branch only
- ✅ Manual dispatch

**Purpose:**
- Build wheel and sdist
- Verify version consistency between `__init__.py` and `pyproject.toml`
- Upload build artifacts

**Container:** `ghcr.io/astral-sh/uv:python3.12-bookworm`

**Key Features:**
- Artifacts retained for 30 days
- Version mismatch detection
- Build summary in GitHub Actions UI
- Artifact naming includes version number

---

### 3. **Release Workflow** (`.github/workflows/release.yml`)

**Triggers:**
- ✅ Push to `master` branch
- ✅ Version tags (`v*.*.*` like `v0.9.1`)
- ✅ GitHub releases (published)
- ✅ Manual dispatch

**Purpose:**
- Build package
- Verify version consistency across files and tags
- Publish to PyPI
- Create GitHub release (if triggered by tag)

**Container:** `ghcr.io/astral-sh/uv:python3.12-bookworm`

**Environment:** `pypi` (configurable via variables)

**Key Features:**
- Version verification across 3 sources
- Tag version matching
- PyPI trusted publishing support
- Automatic GitHub release creation
- Release summary with install command

---

## Project Variables

All workflows support GitHub project variables for customization:

### Environment Variables

Set in workflow `env:` section with fallback defaults:

| Variable | Default | Usage |
|----------|---------|-------|
| `PYTHON_VERSION` | `3.12` | Python version for release builds |
| `PYPI_REPOSITORY` | `pypi` | Environment name (pypi/testpypi) |

### Configurable Variables

Set in **Settings > Secrets and variables > Actions > Variables**:

| Variable | Default | Description |
|----------|---------|-------------|
| `PYTHON_VERSION` | `3.12` | Override Python version |
| `PYPI_REPOSITORY` | `pypi` | Target environment name |
| `PYPI_REPOSITORY_URL` | `https://upload.pypi.org/legacy/` | PyPI upload endpoint |

### Required Secrets

Set in **Settings > Secrets and variables > Actions > Secrets**:

| Secret | Required | Description |
|--------|----------|-------------|
| `PYPI_API_TOKEN` | ✅ Yes | PyPI API token for publishing |

---

## Branch Strategy

| Workflow | master | develop | feature/* | PRs |
|----------|--------|---------|-----------|-----|
| Lint | ✅ | ✅ | ✅ | ✅ |
| Build | ✅ | ❌ | ❌ | ❌ |
| Release | ✅ | ❌ | ❌ | ❌ |

**Rationale:**
- **Lint everywhere** - Catch issues early in development
- **Build on master** - Verify production branch is buildable
- **Release from master** - Only release stable, merged code

---

## Release Process

### Standard Release Flow

```bash
# 1. Update version in both files
vim src/caffeinated_whale_cli/__init__.py  # __version__ = "0.9.2"
vim pyproject.toml                          # version = "0.9.2"

# 2. Update CHANGELOG.md
vim CHANGELOG.md

# 3. Commit changes
git add .
git commit -m "chore: bump version to 0.9.2"
git push origin master

# 4. Create and push tag
git tag v0.9.2
git push origin v0.9.2

# 5. Watch GitHub Actions
# - Build workflow runs on master push
# - Release workflow runs on tag push
# - Package published to PyPI
# - GitHub release created automatically
```

### Alternative: GitHub UI Release

```
1. Go to Releases > Draft a new release
2. Choose a tag: v0.9.2 (create new tag)
3. Target: master
4. Generate release notes (auto)
5. Publish release
   → Triggers release workflow
   → Publishes to PyPI
```

### Manual Release (Testing)

```
1. Go to Actions > Release
2. Click "Run workflow"
3. Select branch: master
4. Click "Run workflow"
   → Builds and publishes to PyPI
   → Does NOT create GitHub release (tag-triggered only)
```

---

## Version Consistency Checks

The workflows enforce version consistency across multiple locations:

### Build Workflow Checks:
1. ✅ `src/caffeinated_whale_cli/__init__.py` → `__version__`
2. ✅ `pyproject.toml` → `version`

### Release Workflow Checks:
1. ✅ `__init__.py` matches `pyproject.toml`
2. ✅ Git tag matches `__init__.py` (if tag-triggered)

**Failure Example:**
```
Error: Version mismatch! __init__.py has 0.9.1 but pyproject.toml has 0.9.2
```

---

## Testing Strategy

### Before First Release

1. **Test Lint:**
   ```bash
   git checkout -b test-lint
   echo "# test" >> README.md
   git add . && git commit -m "test: lint workflow"
   git push origin test-lint
   # Create PR → Lint runs automatically
   ```

2. **Test Build:**
   ```bash
   git checkout master
   git merge test-lint
   git push origin master
   # Build workflow runs automatically
   # Download artifacts from Actions tab
   ```

3. **Test Release on TestPyPI:**
   ```bash
   # Setup TestPyPI (see GITHUB_ACTIONS_SETUP.md)
   # Create testpypi environment with test token
   # Manually trigger release workflow
   Actions > Release > Run workflow
   ```

4. **Verify TestPyPI Release:**
   ```bash
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ \
     caffeinated-whale-cli

   cwcli --version
   ```

5. **Production Release:**
   ```bash
   git tag v0.9.2
   git push origin v0.9.2
   # Release workflow publishes to production PyPI
   ```

---

## GitHub Environments

### Recommended Setup

Create a `pypi` environment for production releases:

**Settings > Environments > New environment: `pypi`**

**Protection Rules:**
- ☑ Required reviewers: Add yourself or team
- ☑ Deployment branches: Selected branches → `master`
- ☑ Wait timer: 0 minutes (or add delay for review)

**Environment Secrets:**
- `PYPI_API_TOKEN` = Your production PyPI token

**Benefits:**
- Manual approval before publishing
- Deployment history tracking
- Separate credentials for different environments
- Branch restrictions

---

## Alternative: PyPI Trusted Publishing

Instead of API tokens, use OIDC trusted publishing (more secure):

### Setup Steps:

1. **Configure on PyPI:**
   - Go to: https://pypi.org/manage/project/caffeinated-whale-cli/settings/publishing/
   - Add trusted publisher:
     - Owner: `karotkriss`
     - Repository: `caffeinated-whale-cli`
     - Workflow: `release.yml`
     - Environment: `pypi`

2. **Update `release.yml`:**
   ```yaml
   - name: Publish to PyPI
     uses: pypa/gh-action-pypi-publish@release/v1
     # Remove: password: ${{ secrets.PYPI_API_TOKEN }}
   ```

3. **Remove secret:**
   - Delete `PYPI_API_TOKEN` from GitHub secrets

**Benefits:**
- ✅ No secret management
- ✅ More secure (no long-lived tokens)
- ✅ Automatic credential rotation
- ✅ Better audit trail

---

## Monitoring

### View Workflow Status

```
Repository > Actions tab
```

### View Build Artifacts

```
Actions > Build > [workflow run] > Artifacts
```

### View Deployments

```
Repository > Environments > pypi > Deployment history
```

### Enable Notifications

```
Your Profile > Settings > Notifications > Actions
☑ Notify me of workflow runs on repositories I'm watching
```

---

## Troubleshooting

### Common Issues

**1. Lint fails with "command not found: ruff"**
```bash
# Add to pyproject.toml dependencies
ruff = ">=0.1.0"
mypy = ">=1.0.0"
```

**2. Build fails with "Version mismatch"**
```bash
# Ensure versions match
grep __version__ src/caffeinated_whale_cli/__init__.py
grep ^version pyproject.toml
```

**3. Release fails with "Invalid credentials"**
```bash
# Regenerate PyPI token
1. Visit https://pypi.org/manage/account/token/
2. Create new token
3. Update PYPI_API_TOKEN secret
4. Re-run workflow
```

**4. Release fails with "File already exists"**
```bash
# Version already published, bump version
vim src/caffeinated_whale_cli/__init__.py
vim pyproject.toml
git add . && git commit -m "chore: bump version"
git push
```

---

## Files Created

```
.github/
├── workflows/
│   ├── lint.yml          # Lint workflow (all branches)
│   ├── build.yml         # Build workflow (master only)
│   ├── release.yml       # Release workflow (master + tags)
│   └── README.md         # Quick reference
├── GITHUB_ACTIONS_SETUP.md  # Complete setup guide
```

Also created:
- `GITHUB_WORKFLOWS.md` (this file) - Implementation overview

---

## Next Steps

1. ✅ Push workflows to repository
2. ✅ Configure `PYPI_API_TOKEN` secret
3. ✅ Create `pypi` environment (optional)
4. ✅ Test lint on feature branch
5. ✅ Test build on master
6. ✅ Test release on TestPyPI
7. ✅ Do production release

---

## References

- [GitHub Actions docs](https://docs.github.com/en/actions)
- [uv Docker integration](https://docs.astral.sh/uv/guides/integration/docker/)
- [PyPI publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [Trusted publishing](https://docs.pypi.org/trusted-publishers/)
