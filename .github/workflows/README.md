# GitHub Actions Workflows

## Quick Reference

### Workflows

| Workflow | File | Triggers | Branch |
|----------|------|----------|--------|
| **Lint** | `lint.yml` | Push, PR | All branches |
| **Test** | `test.yml` | Push, PR | All branches |
| **Build** | `build.yml` | Push, Manual | `master` only |
| **Release** | `release.yml` | Push, Tags, Release, Manual | `master` (push); tags fire from any branch |

### Required Setup

#### 1. Secrets (Settings > Secrets and variables > Actions > Secrets)

```
PYPI_API_TOKEN = pypi-AgEIcHlwaS5vcm...
```

Get token from: https://pypi.org/manage/account/token/

#### 2. Variables (Optional - Settings > Secrets and variables > Actions > Variables)

```
PYTHON_VERSION = 3.12
PYPI_REPOSITORY = pypi
PYPI_REPOSITORY_URL = https://upload.pypi.org/legacy/
```

#### 3. Environment (Recommended - Settings > Environments)

Create environment named `pypi` with:
- Protection rules: Require reviewers (optional)
- Branch restriction: `master` only
- Secret: `PYPI_API_TOKEN`

### Triggering Releases

First land the version bump as a PR to `develop`: bump `pyproject.toml` and `src/caffeinated_whale_cli/__init__.py` together (the workflows hard-fail if they disagree), regenerate `uv.lock` with `uv lock`, and add a `CHANGELOG.md` section. Then trigger the publish:

**Method 1: Version Tag**
```bash
# tag the bump's merge commit on develop; the tag trigger fires regardless of branch
git tag v0.9.2
git push origin v0.9.2
```

**Method 2: GitHub Release**
```
Releases > Draft new release > Create tag v0.9.2 > Publish
```

**Method 3: Manual Dispatch**
```
Actions > Release > Run workflow > Run workflow
```

### Before First Release

1. ✅ Add `PYPI_API_TOKEN` secret
2. ✅ Create `pypi` environment (optional)
3. ✅ Test on TestPyPI first (see [CI/CD guide](../../docs/contributing/ci-cd.md))
4. ✅ Ensure versions match in `__init__.py` and `pyproject.toml`

### Full Documentation

See [docs/contributing/ci-cd.md](../../docs/contributing/ci-cd.md) for complete CI/CD guide.
