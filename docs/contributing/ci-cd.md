# CI/CD Workflows

This guide covers the automated GitHub Actions workflows for caffeinated-whale-cli.

## Overview

We use five GitHub Actions workflows:

| Workflow | Triggers | Purpose |
|----------|----------|---------|
| **Lint** | All branches, all PRs | Code quality checks (Black, Ruff) |
| **Test** | All branches, all PRs | Run the fast `unit` pytest tier (required gate) + mypy (zero-error gate), a narrow `tests/test_auto_inspect.py` leg on `windows-latest`, and a runtime-deps-only clean-install smoke test |
| **E2E** | PRs into `develop`/`master`, `e2e`-labeled PRs, manual dispatch | Run the real-Docker `e2e` tier on a v14/v15/v16 Frappe matrix, plus a runtime-deps-only full-lifecycle leg (`e2e_pkg`) that drives a `uv tool install .` binary via `CWCLI_BIN` |
| **Build** | Push to `master`, manual dispatch | Build package, verify version consistency |
| **Release** | Tags `v*.*.*` | Publish to PyPI, create GitHub release |

Lint, Build, and Release, along with Test's `Pytest`/`Mypy` jobs, run inside the `ghcr.io/astral-sh/uv:python3.12-bookworm` Docker image. E2E runs directly on the `ubuntu-latest` host runner (no `container:`) so `docker`/`docker compose` can reach the runner's own daemon; it installs `uv` via `astral-sh/setup-uv` instead. Test's other two jobs also run on host runners rather than the container: `Pytest (Windows, auto-inspect)` runs on `windows-latest` (no Linux container available there), and `Clean install smoke` runs on `ubuntu-latest` so `uv tool install` resolves a real runtime-only environment instead of the container's `--all-extras` sync. Both install `uv` via `astral-sh/setup-uv`.

`develop` is branch-protected and currently requires ten contexts: `Lint & Format Check`, `Pytest`, `Mypy`, `E2E (runtime-only install)`, and the six `E2E (frappe vNN, shared)` / `E2E (frappe vNN, standalone)` contexts.
`master` is not branch-protected.
Job names are load-bearing because renaming a required job also renames its status context; update the required-check list in the same window as any such rename.

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

Runs on every push and PR. Has four jobs:

- **Pytest** - runs the fast `unit` tier with coverage (`uv run pytest -m unit --cov=caffeinated_whale_cli`). It needs no Docker daemon and runs inside the uv container.
- **Mypy** - runs `uv run mypy src/` as a zero-error gate. The historical ~50 errors across ~14 files were burned down to zero and `continue-on-error` was dropped from the step, so any new type error fails the job.
- **Pytest (Windows, auto-inspect)** - runs only `tests/test_auto_inspect.py` on `windows-latest`, the one non-Linux runner in this repo. It exists because every other job runs `ubuntu-latest`, and that is exactly how a Windows-only defect in `utils/auto_inspect.py` went unnoticed (`os.kill(pid, 0)` is not a genuine liveness probe on Windows). Deliberately narrow rather than `-m unit`: the other jobs run inside a Linux uv container this runner can't use, and the rest of the unit tier has never been exercised on Windows. See [Testing Guide](../testing/guide.md#the-windows-job-why-it-exists-and-why-it-is-narrow).
- **Clean install smoke** - does a runtime-deps-only `uv tool install .` into an isolated tool dir (`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`), asserts the resulting env is runtime-only (`click` present, `pytest` absent), then runs `cwcli --help`, `config --help`, `config edit` (`EDITOR=true`), and `config path`. Every other job and the local dev loop use `uv sync --all-extras`, where transitive/dev deps mask an undeclared runtime import; this job (and E2E's `e2e-runtime-only` job below) are the only two that exercise a runtime-deps-only install - this one over `--help`/`config` reads only, the other over a full real-Docker lifecycle. This is exactly how cwcli once shipped broken: `commands/config.py` imported `click` directly, but only `typer` was declared, and typer 0.27 stopped supplying `click` transitively, so a real `uv tool install` had no `click` and every command died at import with `ModuleNotFoundError`.

**Run locally:**
```bash
uv run pytest -m unit --cov=caffeinated_whale_cli
uv run mypy src/
```

Run the clean-install smoke leg locally:
```bash
export UV_TOOL_DIR=$(mktemp -d) UV_TOOL_BIN_DIR=$(mktemp -d)
uv tool install .
"$UV_TOOL_BIN_DIR/cwcli" --help
```

---

### E2E (`.github/workflows/e2e.yml`)

Runs the real-Docker `e2e` tier (`tests/e2e/`) against genuine throwaway Frappe instances, driving the real `cwcli` binary. See [tests/README.md](../../tests/README.md#e2e-harness-real-docker) for the harness itself.

- **Trigger gate:** a manual dispatch, a PR carrying the `e2e` label, or a PR whose base branch is `develop`/`master`. See the branch-protection contract above for which job contexts are required.
- **Scope gate (logic/dep filter):** the matrix runs the real Frappe legs only when the diff touches application logic or dependencies; a docs-only / CI-config-only / CHANGELOG-only / unit-fixture-only / pure-rebase push short-circuits every leg to an immediate success. A cheap `changes` job (gated on the same trigger as the two heavy jobs, so it never runs on a PR that could not trigger the matrix anyway) decides once (`run=true`/`false`) by diffing the PR's merge-base against its head and matching `src/**`, `pyproject.toml`, `uv.lock`, or `tests/e2e/**` (the last because those files *are* the E2E); a manual dispatch or the `e2e` label always forces `run=true`. It fails **open** toward `run=true` on any uncertainty - a failed checkout (`continue-on-error`) or an undiffable merge-base both default to running the full matrix, since the E2E contexts are required and skipping on uncertainty is worse than an unnecessary run. Each heavy job always enters when the trigger gate is true, then gates every Frappe step on `needs.changes.outputs.run == 'true'`. This is deliberate: a top-level `paths:` filter would stop the workflow entirely, leaving a *required* context stuck `pending` forever (blocking merge); short-circuiting the steps instead reports the context **green in seconds** on a no-logic/no-dep change while a real `src/`/dep change still runs the full multi-minute matrix.
- **Runner:** `ubuntu-latest` host runner (no `container:`), `fail-fast: false` so a leaky instance on one job can't cancel another. The matrix is `strategy.matrix.frappe: [14, 15, 16]` crossed with `strategy.matrix.group: [shared, standalone]`, so each version leg is two jobs.
- **The `shared` / `standalone` split:** the `standalone` pytest marker names every `e2e` test that never touches the session-scoped shared instance - it either builds its own uniquely named, port-disjoint instance or needs no instance at all. The `standalone` job selects `-m "e2e and standalone"`, the `shared` job selects `-m "e2e and not standalone"`, and the two partition the tier exactly: every test that ran before still runs, in one job or the other. The point is wall clock. The two halves of a leg are close to balanced, and the `standalone` job never pays the session `cwcli init` at all, so splitting them roughly halves the workflow's total time. It is safe without touching any test because the groups land on separate runners with separate Docker daemons, disks and `$CWCLI_HOME` - there is no shared state to leak between them. `tests/e2e/conftest.py` fails collection if the marker ever disagrees with the fixtures a test requests, in either direction, so the split cannot silently rot.
- **Steps:** authenticate to Docker Hub when creds are configured (dodges the anonymous-pull rate limit on the multi-GB `frappe/bench` image), a preflight upstream-reachability check (annotates an upstream/infra break distinctly from a real assertion failure), `uv sync --frozen --all-extras`, the group's `uv run pytest tests/e2e -m "..." -o addopts=""`, then an unconditional `always()` teardown step that sweeps any leaked `cwe2e-` resources and prunes the runner's Docker state.
- **Per-job `timeout-minutes`:** ~45 (a full `cwcli init` - image pull + bench init + new-site - is the dominant cost).
- **`latency_benches` dispatch input (opt-in, off by default):** sets `CWE2E_LATENCY_BENCHES` for the run, which selects `tests/e2e/test_multibench_latency_e2e.py`.
  Set it to `6` to grow a real six-bench serving instance on the v16 `standalone` leg and record the instance-wide `cwcli status` cost curve.
  It is off on every pull request because each data point requires another genuine bench build.
  Setting it raises the job's `timeout-minutes` from 45 to 90.
  The measured runtime, disk headroom, and latency numbers live in [`docs/e2e/multibench-serving-status.md`](../e2e/multibench-serving-status.md).

The workflow also runs a separate **`E2E (runtime-only install)`** job (same trigger gate) that exercises **packaging realism** - a different axis from the version matrix:

- Every leg above (and the whole local dev loop) drives the `cwcli` console script out of a `uv sync --all-extras` dev venv, where dev/transitive deps are present. That masks a runtime `import` of a package not declared in `[project.dependencies]`: it succeeds in the dev venv and ships broken to a real `uv tool install` - exactly how the missing `click` runtime dependency reached 0.37.0 while every test passed.
- The `Clean install smoke` job (Test workflow) guards *import-time* deps, but only over `--help`/`config` reads; it never runs a real Docker command path, so a dep imported lazily inside a command body stays invisible to it.
- This job installs cwcli with `uv tool install .` (runtime deps only - no dev, no extras), asserts the install is runtime-only, then points the harness at that binary via the **`CWCLI_BIN`** env override (honored by `tests/e2e/harness.py`) and runs one genuine full lifecycle - init -> apps list -> inspect -> backup -> rm - marked `e2e_pkg`. Any undeclared runtime dependency surfaces here as a `ModuleNotFoundError` on a real command, which no other gate catches. The pytest harness itself still runs from the dev venv (it needs `pexpect`/`pytest`); only the binary it *drives* is the runtime-only tool.
- It is a single **v16** leg (packaging realism is version-agnostic) and deliberately **off** the `-m e2e` matrix (`e2e_pkg` is excluded from `-m e2e`), so the dominant init cost is paid once rather than added to all three version legs.

**Run locally** (needs a reachable Docker daemon; installs `pexpect` via the `e2e` extra):
```bash
uv sync --all-extras
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m e2e

# Or one CI group at a time (what each matrix job actually runs):
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m "e2e and standalone"
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m "e2e and not standalone"

# The runtime-only packaging leg (drives whatever CWCLI_BIN points at; omit
# CWCLI_BIN to run the same lifecycle against the dev binary):
CWCLI_BIN=/path/to/runtime-only/cwcli uv run pytest tests/e2e -m e2e_pkg -o addopts=""
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

**Steps:**
1. Extract and verify version from `__init__.py`, `pyproject.toml`, and git tag
2. Compose the release card from the generated header and required hand-written note
3. Build package (`uv build --no-create-gitignore`)
4. Publish to PyPI (`uv publish --check-url https://pypi.org/simple/`, authenticated with `UV_PUBLISH_TOKEN`)
5. Create the GitHub release with the composed card and build artifacts

The release note must exist before the tag is pushed.
See [`.github/release-notes/README.md`](../../.github/release-notes/README.md) for its authoritative format and rendering instructions.

**Trigger a release:**
```bash
# 1. Bump the version (four files move together)
vim pyproject.toml                          # version = "0.10.0"
vim src/caffeinated_whale_cli/__init__.py   # __version__ = "0.10.0"
uv lock                                     # refresh the project's own entry in uv.lock
vim CHANGELOG.md                            # add the "## [0.10.0] - YYYY-MM-DD" section
vim .github/release-notes/v0.10.0.md        # write the GitHub release-card copy

# 2. Land the bump as a normal PR to develop
git add pyproject.toml src/caffeinated_whale_cli/__init__.py uv.lock CHANGELOG.md .github/release-notes/v0.10.0.md
git commit -m "chore: bump version to 0.10.0"
# push the branch, open a PR, merge into develop

# 3. Tag the merge commit (the tag trigger fires regardless of branch)
git tag v0.10.0
git push origin v0.10.0
```

---

## Setup (First Time)

### Required: PyPI API Token

The release workflow authenticates with the `UV_PUBLISH_TOKEN` repository secret.
Store a valid PyPI API token for this project at:

**Settings > Secrets and variables > Actions > New repository secret**

Publishing fails with an authentication error if that secret is absent, expired, or invalid.
CI cannot create or rotate it.

### Required: GitHub Environment

The workflow deploys through a GitHub environment named `pypi` by default:

**Settings > Environments > New environment: `pypi`**

**Protection rules:**
- ☑ Required reviewers (optional)

**Benefits:**
- Manual approval before publishing
- Deployment history tracking

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

## Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [uv Documentation](https://docs.astral.sh/uv/)
- [PyPI Publishing Guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [Workflows README](../../.github/workflows/README.md)
