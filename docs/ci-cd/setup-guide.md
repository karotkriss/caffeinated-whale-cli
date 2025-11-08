# GitHub Actions Setup Guide

This document explains how to configure GitHub Actions workflows for the caffeinated-whale-cli project.

## Workflows Overview

### 1. **Lint** (`lint.yml`)
- **Triggers:** All branches, all PRs
- **Purpose:** Code quality checks (Black, Ruff, mypy)
- **Container:** `ghcr.io/astral-sh/uv:python3.12-bookworm`

### 2. **Build** (`build.yml`)
- **Triggers:** Push to `master` branch, manual dispatch
- **Purpose:** Build wheel and sdist, verify version consistency
- **Container:** `ghcr.io/astral-sh/uv:python3.12-bookworm`
- **Artifacts:** Uploads build artifacts with 30-day retention

### 3. **Release** (`release.yml`)
- **Triggers:** Push to `master`, version tags (`v*.*.*`), GitHub releases, manual dispatch
- **Purpose:** Build and publish to PyPI, create GitHub releases
- **Container:** `ghcr.io/astral-sh/uv:python3.12-bookworm`
- **Environment:** Uses GitHub environment for PyPI publishing

---

## Initial Setup

### Step 1: Configure GitHub Secrets

Navigate to: **Repository Settings > Secrets and variables > Actions > Secrets**

#### Required Secrets:

| Secret Name | Description | How to Get |
|------------|-------------|------------|
| `PYPI_API_TOKEN` | PyPI API token for publishing | 1. Go to https://pypi.org/manage/account/token/<br>2. Create new token with scope "Entire account" or specific to this project<br>3. Copy the token (starts with `pypi-`) |

**Creating the secret:**
```bash
# In GitHub UI:
1. Click "New repository secret"
2. Name: PYPI_API_TOKEN
3. Value: pypi-AgEIcHlwaS5vcm...
4. Click "Add secret"
```

---

### Step 2: Configure GitHub Variables (Optional)

Navigate to: **Repository Settings > Secrets and variables > Actions > Variables**

#### Optional Variables:

| Variable Name | Default | Description |
|--------------|---------|-------------|
| `PYTHON_VERSION` | `3.12` | Python version for release builds |
| `PYPI_REPOSITORY` | `pypi` | Environment name (pypi/testpypi) |
| `PYPI_REPOSITORY_URL` | `https://upload.pypi.org/legacy/` | PyPI upload URL |

**Creating a variable:**
```bash
# In GitHub UI:
1. Click "New repository variable"
2. Name: PYTHON_VERSION
3. Value: 3.12
4. Click "Add variable"
```

---

### Step 3: Configure PyPI Environment (Recommended)

Using a GitHub environment adds protection rules and approval gates for releases.

Navigate to: **Repository Settings > Environments**

1. **Create environment:**
   - Name: `pypi`
   - Click "Configure environment"

2. **Add protection rules (optional):**
   - ☑ Required reviewers (add yourself or team)
   - ☑ Wait timer: 0 minutes (or add delay)
   - ☑ Deployment branches: Only `master`

3. **Add environment secrets (alternative to repo secrets):**
   - Name: `PYPI_API_TOKEN`
   - Value: Your PyPI token

**Benefits of using environments:**
- Require manual approval before publishing
- Restrict deployments to specific branches
- Separate secrets for different environments (pypi vs testpypi)
- Deployment history tracking

---

## Alternative: PyPI Trusted Publishing (Recommended)

Instead of using API tokens, you can use PyPI's trusted publishing (OIDC):

### Step 1: Configure PyPI Project

1. Go to https://pypi.org/manage/project/caffeinated-whale-cli/settings/publishing/
2. Add a new "trusted publisher":
   - **PyPI Project Name:** `caffeinated-whale-cli`
   - **Owner:** `karotkriss` (your GitHub username)
   - **Repository name:** `caffeinated-whale-cli`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi` (optional but recommended)

### Step 2: Update release.yml

Replace the `pypa/gh-action-pypi-publish@release/v1` step:

```yaml
- name: Publish to PyPI
  uses: pypa/gh-action-pypi-publish@release/v1
  # No password needed with trusted publishing!
```

Remove the `password: ${{ secrets.PYPI_API_TOKEN }}` line.

**Benefits:**
- ✅ No secrets to manage
- ✅ More secure (no long-lived tokens)
- ✅ Automatic rotation
- ✅ Better audit trail

---

## Testing Workflows

### Test Lint Workflow

Create a PR or push to any branch:
```bash
git checkout -b test-workflows
git add .github/
git commit -m "test: add GitHub workflows"
git push origin test-workflows
# Create PR on GitHub
```

### Test Build Workflow

Push to master:
```bash
git checkout master
git merge test-workflows
git push origin master
```

View artifacts: **Actions > Build > Artifacts**

### Test Release Workflow

#### Option 1: Manual Trigger
```bash
# In GitHub UI:
Actions > Release > Run workflow > Run workflow
```

#### Option 2: Create Version Tag
```bash
git tag v0.9.1
git push origin v0.9.1
```

#### Option 3: Create GitHub Release
```bash
# In GitHub UI:
Releases > Draft a new release > Choose tag (v0.9.1) > Publish release
```

---

## Testing on TestPyPI First

Before publishing to production PyPI, test on TestPyPI:

### Step 1: Get TestPyPI Token
1. Go to https://test.pypi.org/manage/account/token/
2. Create token
3. Add as secret: `TEST_PYPI_API_TOKEN`

### Step 2: Create testpypi Environment
1. **Settings > Environments > New environment**
2. Name: `testpypi`
3. Add secret: `PYPI_API_TOKEN` = `<test-pypi-token>`

### Step 3: Add Variable
```
Name: PYPI_REPOSITORY_URL
Value: https://test.pypi.org/legacy/
```

### Step 4: Manually Trigger Release
```bash
# In GitHub UI:
Actions > Release > Run workflow
# It will use testpypi environment
```

### Step 5: Verify Upload
```bash
pip install --index-url https://test.pypi.org/simple/ caffeinated-whale-cli
```

---

## Workflow Triggers Summary

| Workflow | Branches | PRs | Tags | Manual | Release |
|----------|----------|-----|------|--------|---------|
| Lint | ✅ All | ✅ All | ❌ | ❌ | ❌ |
| Build | ✅ master only | ❌ | ❌ | ✅ | ❌ |
| Release | ✅ master only | ❌ | ✅ v*.*.* | ✅ | ✅ |

---

## Monitoring Workflows

### View Workflow Runs
```
Repository > Actions tab
```

### View Build Artifacts
```
Actions > Build workflow run > Artifacts section
```

### View Release Status
```
Actions > Release workflow run > Environment deployments
```

### Enable Notifications
```
Your GitHub Settings > Notifications > Actions
- ☑ Notify me of workflow runs on repositories I'm watching
```

---

## Troubleshooting

### Lint Fails with "No module named X"

**Problem:** Missing dependency in `pyproject.toml`

**Solution:**
```toml
# Add to pyproject.toml [project.dependencies]
dependencies = [
    # ... existing deps
    "ruff>=0.1.0",  # Add if missing
    "mypy>=1.0.0",  # Add if missing
]
```

### Build Fails with "Version mismatch"

**Problem:** `__init__.py` and `pyproject.toml` versions don't match

**Solution:**
```bash
# Update both files to match
vim src/caffeinated_whale_cli/__init__.py  # __version__ = "0.9.1"
vim pyproject.toml                          # version = "0.9.1"
```

### Release Fails with "Invalid credentials"

**Problem:** PyPI token is incorrect or expired

**Solution:**
1. Generate new token at https://pypi.org/manage/account/token/
2. Update `PYPI_API_TOKEN` secret in GitHub
3. Re-run workflow

### Release Fails with "Filename already exists"

**Problem:** Version was already published to PyPI

**Solution:**
```bash
# Bump version in both files
vim src/caffeinated_whale_cli/__init__.py
vim pyproject.toml

# Commit and push
git add .
git commit -m "chore: bump version to 0.9.2"
git push
```

### Container Pull Rate Limit

**Problem:** "Too Many Requests" from `ghcr.io`

**Solution:**
```yaml
# Add authentication in workflow (rare, usually not needed)
- name: Login to GitHub Container Registry
  uses: docker/login-action@v3
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}
```

---

## Security Best Practices

1. **Never commit secrets** to the repository
2. **Use environment protection rules** for production releases
3. **Enable branch protection** on `master`:
   - Require PR reviews
   - Require status checks (lint) to pass
4. **Use trusted publishing** instead of API tokens when possible
5. **Rotate PyPI tokens** periodically
6. **Limit token scope** to specific projects

---

## Maintenance

### Update Workflow Dependencies

Periodically update action versions:
```yaml
# Check for updates at:
# - https://github.com/actions/checkout/releases
# - https://github.com/actions/upload-artifact/releases
# - https://github.com/pypa/gh-action-pypi-publish/releases

uses: actions/checkout@v4  # Check for v5
uses: actions/upload-artifact@v4  # Check for newer
uses: pypa/gh-action-pypi-publish@release/v1  # Check changelog
```

### Monitor uv Updates

Check for new uv releases:
```bash
# View available tags at:
# https://github.com/astral-sh/uv/pkgs/container/uv
```

---

## Next Steps

After setup:

1. ✅ Verify lint runs on PR creation
2. ✅ Test build on master push
3. ✅ Do test release to TestPyPI
4. ✅ Do production release to PyPI
5. ✅ Enable branch protection rules
6. ✅ Configure environment protection

For questions, see:
- [GitHub Actions docs](https://docs.github.com/en/actions)
- [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/)
- [PyPI publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
