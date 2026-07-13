# GitHub Actions Workflows

## Quick Reference

### Workflows

| Workflow | File | Triggers | Branch |
|----------|------|----------|--------|
| **Lint** | `lint.yml` | Push, PR | All branches |
| **Test** | `test.yml` | Push, PR | All branches (fast `unit` pytest tier + mypy) |
| **E2E** | `e2e.yml` | PR, `e2e` label, Manual | PRs into `develop`/`master` (real-Docker `e2e` tier, v14/v15/v16 matrix) |
| **Build** | `build.yml` | Push, Manual | `master` only |
| **Release** | `release.yml` | Push, Tags, Release, Manual | `master` (push); tags fire from any branch |

### Required Setup

#### 1. PyPI Trusted Publisher (one-time, on pypi.org)

Publishing uses [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) over GitHub OIDC - there is **no** `PYPI_API_TOKEN` secret. Register a trusted publisher for the project on PyPI (**Manage project > Publishing**):

```
Owner:       karotkriss
Repository:  caffeinated-whale-cli
Workflow:    release.yml
Environment: pypi
```

Until this publisher exists, `uv publish` fails with an auth error; CI cannot create it.

#### 2. Variables (Optional - Settings > Secrets and variables > Actions > Variables)

```
PYTHON_VERSION = 3.12
PYPI_REPOSITORY = pypi
```

#### 3. Environment (Required - Settings > Environments)

Create environment named `pypi` (its name must match the trusted publisher's `Environment` above):
- Protection rules: Require reviewers (optional)
- Branch restriction: `master` only

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

1. ✅ Register the PyPI trusted publisher (owner `karotkriss`, repo `caffeinated-whale-cli`, workflow `release.yml`, environment `pypi`)
2. ✅ Create the `pypi` environment (required - its name must match the trusted publisher)
3. ✅ Test on TestPyPI first (see [CI/CD guide](../../docs/contributing/ci-cd.md))
4. ✅ Ensure versions match in `__init__.py` and `pyproject.toml`

### Full Documentation

See [docs/contributing/ci-cd.md](../../docs/contributing/ci-cd.md) for complete CI/CD guide.
