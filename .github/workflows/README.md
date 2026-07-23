# GitHub Actions Workflows

## Quick Reference

### Workflows

| Workflow | File | Triggers | Branch |
|----------|------|----------|--------|
| **Lint** | `lint.yml` | Push, PR | All branches |
| **Test** | `test.yml` | Push, PR | All branches (fast `unit` pytest tier + mypy + a narrow Windows auto-inspect leg + a clean-install smoke test) |
| **E2E** | `e2e.yml` | PR, `e2e` label, Manual | PRs into `develop`/`master` (real-Docker `e2e` tier, v14/v15/v16 matrix) |
| **Build** | `build.yml` | Push, Manual | `master` only |
| **Release** | `release.yml` | Version tags | Tags fire from any branch |

### Required Setup

#### 1. PyPI API token

Publishing uses the `UV_PUBLISH_TOKEN` repository secret.
See the [CI/CD guide](../../docs/contributing/ci-cd.md#required-pypi-api-token) for setup.

#### 2. Variables (Optional - Settings > Secrets and variables > Actions > Variables)

```
PYTHON_VERSION = 3.12
PYPI_REPOSITORY = pypi
```

#### 3. Environment (Required - Settings > Environments)

Create an environment named `pypi`:
- Protection rules: Require reviewers (optional)

### Triggering Releases

The [CI/CD guide](../../docs/contributing/ci-cd.md#release-githubworkflowsreleaseyml) owns the release procedure.
The hand-written release card must be committed before the version tag is pushed.
Its format is owned by [`.github/release-notes/README.md`](../release-notes/README.md).

### Full Documentation

See [docs/contributing/ci-cd.md](../../docs/contributing/ci-cd.md) for complete CI/CD guide.
