# Tests

This directory contains all tests for the caffeinated-whale-cli project.

## Two tiers: fast `unit` vs real-Docker `e2e`

The suite is split into two tiers by pytest marker.

- **`unit`** - fast, needs no Docker daemon.
  It is the default tier: a bare `pytest` runs only this tier.
  It is the mock-free/mock-based suite that verifies pure logic and command wiring against fakes, and it runs inside the uv container in CI (`test.yml`, `-m unit`).
  During the migration off the legacy mock suite (see [`../openspec/changes/rebuild-e2e-test-suite`](../openspec/changes/rebuild-e2e-test-suite)) this tier also carries the container-mock behavior tests; they are retired per command as each command's real E2E lands, and the mock-free pure-logic tests are kept permanently.
- **`e2e`** / **`e2e_p2p`** - real Docker.
  These live under [`tests/e2e/`](e2e/) and drive the real `cwcli` console script against genuine throwaway Frappe instances (real `cwcli init` up, real side-effect assertions, `cwcli rm` down).
  They require a reachable Docker daemon and are excluded by default; run them explicitly with `-m e2e`.
  See [E2E harness](#e2e-harness-real-docker) below.

The `unit`, `e2e`, and `e2e_p2p` markers are registered in `pyproject.toml`; `tests/conftest.py` auto-applies `unit` to any test not marked `e2e`/`e2e_p2p`, so there is nothing to hand-mark.

## Quick Reference

```bash
# Fast tier only (the default; no Docker needed)
uv run pytest

# Fast tier, explicit + coverage (what CI's unit job runs)
uv run pytest -m unit --cov=caffeinated_whale_cli

# Prove the fast tier is mock-free: still green with a dead Docker endpoint
DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest -m unit

# Real-Docker E2E tier (needs a Docker daemon), one Frappe version leg
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m e2e

# Run specific test file
uv run pytest tests/test_completion_utils.py

# Run tests matching a pattern
uv run pytest -k "cache"

# Stop on first failure / show locals / debugger
uv run pytest -x
uv run pytest -l
uv run pytest --pdb
```

## E2E harness (real Docker)

The E2E harness ([`tests/e2e/harness.py`](e2e/harness.py) + [`tests/e2e/conftest.py`](e2e/conftest.py)) automates the manual `docs/e2e/` recipe so the destructive-path guarantees are enforced by machine.
It drives the real `cwcli` binary (subprocess for non-interactive, `pexpect` for interactive - awaiting the prompt_toolkit `ESC[?2004h` raw-mode marker before each keystroke), waits on real readiness (never fixed sleeps), and asserts real side effects (e.g. a non-empty DB dump copied out to the host), in both modes.

Isolation and safety are non-negotiable and layered:

- Each session gets a temporary `HOME` **and** a `CWCLI_HOME` override (the precise seam that relocates only cwcli's own footprint), plus unique `cwe2e-<runid>-<n>` project/site names and a port allocator (bases ≥1006 apart).
- A **hard rail** (`enforce_isolation`) fails closed before any Docker work if `HOME` is (or nests under) the operator's real home, or if `CWCLI_HOME` is unset or does not resolve to a location inside that isolated `HOME` (so a `CWCLI_HOME` pointing at the real home can never slip through), and a name rail refuses any project name lacking the `cwe2e-` prefix.
- An **unconditional teardown backstop** (`sweep_cwe2e`) removes every `cwe2e-`-labelled compose project's containers, volumes, and networks on session teardown, so a crashed test never leaks.

`CWE2E_FRAPPE_MAJOR` selects the Frappe version leg (default 16); version-agnostic E2E tests run only on the v16 leg, version-sensitive ones on every leg.
The real-Docker E2E is Linux-only; Windows/macOS-specific code stays in the unit tier.

## Test Files

As of 0.37.0 (unreleased), `tests/` holds 30 `test_*.py` suites totaling 518 tests in the
`unit` tier (measured with `uv run pytest --cov`). Run `ls tests/` for the
authoritative current list; see [../docs/testing/README.md](../docs/testing/README.md)
for the per-area breakdown.

`tests/e2e/` adds more `test_*.py` suites in the real-Docker `e2e` tier
(`test_init_e2e.py`, `test_backup_e2e.py`, `test_start_status_e2e.py`,
`test_harness_safety.py`); run `ls tests/e2e/` for the current list. They are not
part of the 518/30 count above since they need a Docker daemon and are excluded
from a bare `pytest`. `test_start_status_e2e.py` is the lifecycle-command net
(`start`/`status`/`logs`/`restart`, both modes) that pins the outcome-level
invariants the start/status core migration must preserve; it is structure-agnostic
(it asserts observable states, not `/tmp` log paths, `pkill` patterns, or the exact
status tokens) so it survives that migration unchanged.

### `test_completion_utils.py`
Tests for tab completion functionality.

**Coverage:** 92%

**What it tests:**
- Project name completion from Docker
- App name completion from cache
- Site name completion from cache
- Cache TTL behavior
- Docker client reuse
- Error handling

**Test classes:**
- `TestCompleteProjectNames` - 7 tests
- `TestCompleteAppNames` - 6 tests
- `TestCompleteSiteNames` - 4 tests
- `TestCacheHelpers` - 4 tests
- `TestGetDockerClient` - 3 tests

## Test Coverage

Current overall coverage at 0.37.0, unreleased (518 tests across 30 test files, ~55% overall).

### Covered Modules
- ✅ `utils/completion_utils.py` - 92% (7 missing lines)
- ✅ `utils/bench_labels.py` - ~94% (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`)
- ✅ `utils/db_utils.py` - ~68% (`test_db_security`, `test_config_validation`)
- ✅ `commands/inspect.py` - ~62% (`test_inspect_partial_refresh`, `test_inspect_label_recovery`)
- ✅ `commands/restore.py` - ~43% (`test_restore_safety`, `test_restore_inspect_fixes`)
- ✅ `commands/rm.py` - ~78% (`test_rm_safety`, `test_rm_truth`, `test_rm_stopped`)
- ✅ `commands/init.py` - ~49% (`test_init_reuse_bench`, `test_init_mariadb_flag`, `test_init_admin_password`)
- ✅ `commands/apps.py` + `commands/update.py` app-update path - (`test_apps`: both modes, multi-site fan-out, frappe reset, `update` deprecation)
- ✅ `utils/config_utils.py`'s `cwcli_home()` - (`test_cwcli_home`: mock-free, sets a real `CWCLI_HOME` env var and checks real filesystem/subprocess results)
- ✅ `core/envelope.py`, `core/errors.py`, `core/resolvers.py`, `core/docker.py` - 100% (`test_core_envelope`: DTO/error contract + the AST-scan purity ban; `test_core_resolvers`: split resolvers plus the `commands/utils.py`/`docker_utils.py` CLI-wrapper exit-code preservation)
- ✅ `core/backup.py` - ~95% (`test_core_backup`: every `core.backup` branch - success, both `NEEDS_CHOICE` forks, each `CwcliError` kind - on a fake container)
- ✅ `commands/axi.py` - ~97% (`test_axi`: verb exit-mapping (0/1/2), the content-first home, `axi ls`/`axi where`, the TOON encoder)
- ✅ `core/list.py`, `core/where.py` - 100% (`test_core_list`: fake docker client, empty/aggregate/DOCKER-raise; `test_core_where`: throwaway sqlite, dedup/scoping/installed-only/USAGE)
- ✅ `commands/list.py` - ~80% (`test_list`: the `ls --json` empty-`[]` fix, quiet/table rendering, port-range condensing)
- ✅ `commands/where.py` - 100% (`test_where`: table/JSON rendering, the `--apps`/`--sites` conflict, the definitive `[]` empty state)

### Modules Needing Dedicated Suites
- ⚠️ `utils/port_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/docker_utils.py` (~51%, up from ~37% now that `test_core_resolvers` dedicated-tests the `get_frappe_container` CLI wrapper; the rest of the module is still only incidentally covered)
- ⚠️ `utils/sendme_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/vscode_utils.py` (~16%, only incidental coverage)
- ⚠️ `utils/config_utils.py` (~41%; `cwcli_home()` is covered by `test_cwcli_home`, but `load_config`/`save_config`/the custom-path and auto-inspect-config setters remain untested)
- ⚠️ Command modules at or near 0% dedicated coverage: `backup.py` (0%; its logic moved to `core/backup.py`, which is covered - see above), `run.py`, `status.py` (0% dedicated unit suite, but now covered end to end by `tests/e2e/test_start_status_e2e.py`), `unlock.py`

## Writing New Tests

See [../docs/testing/guide.md](../docs/testing/guide.md) for detailed testing guidelines.

### Quick Start

1. Create new test file: `test_<module>.py`
2. Import what you need to test
3. Create test class: `class Test<Feature>:`
4. Write test methods: `def test_<behavior>(self):`
5. Run tests: `uv run pytest tests/test_<module>.py`

### Example Test Structure

```python
"""
Tests for my_module functionality.
"""

import pytest
from unittest.mock import Mock, patch

from caffeinated_whale_cli.utils import my_module


@pytest.fixture
def mock_dependency():
    """Mock external dependency."""
    return Mock()


class TestMyFunction:
    """Tests for my_function."""

    def test_happy_path(self, mock_dependency):
        """Should return expected result for normal input."""
        result = my_module.my_function(mock_dependency)

        assert result == expected_value

    def test_error_handling(self, mock_dependency):
        """Should handle errors gracefully."""
        mock_dependency.method.side_effect = Exception("Error")

        result = my_module.my_function(mock_dependency)

        assert result is None  # or whatever the fallback is
```

## Configuration

Test configuration is in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
# The default `-m` deselects the real-Docker tiers, so a bare `pytest` runs only
# the fast unit tier; override with `-m e2e` on the CLI (the last `-m` wins).
addopts = ["-v", "--strict-markers", "--tb=short", "--cov-report=term-missing", "-m", "not e2e and not e2e_p2p"]
markers = [
    "unit: fast tests that need no Docker daemon (the default tier)",
    "e2e: real-Docker end-to-end tests driving the real cwcli binary",
    "e2e_p2p: real-Docker P2P (sendme loopback) end-to-end tests",
]
```

Coverage configuration:

```toml
[tool.coverage.run]
source = ["src/caffeinated_whale_cli"]
omit = ["*/tests/*", "*/__init__.py"]
```

## CI/CD

CI is two-tiered.

- **`.github/workflows/test.yml`** runs the fast `unit` tier on every push and PR, inside the uv container:

  ```yaml
  - name: Run unit tests with coverage
    run: uv run pytest -m unit --cov=caffeinated_whale_cli --cov-report=term-missing
  ```

  The `Pytest` (unit) job is the always-required gate.

- **`.github/workflows/e2e.yml`** runs the real-Docker `e2e` tier as a `strategy.matrix.frappe: [14, 15, 16]` of GitHub-hosted `ubuntu-latest` jobs (no `container:`, so `docker`/`docker compose` reach the daemon).
  It is triggered on PRs into `develop`/`master` and on-demand via the `e2e` PR label, with per-job `timeout-minutes` and an `always()` `cwe2e-` teardown backstop.
  Note: `develop`/`master` have no branch protection today, so a repo admin must enable it and tick these checks before the E2E matrix is a *required* gate; until then the unit tier is the only gate that blocks a merge.

See the [CI/CD Workflows guide](../docs/contributing/ci-cd.md) for the full setup.

## Common Issues

### Import Errors

If you get import errors, make sure the package is installed:

```bash
uv sync --all-extras
```

### Coverage Not Working

Make sure to use the correct module path:

```bash
# Good
uv run pytest --cov=caffeinated_whale_cli

# Bad (won't find source)
uv run pytest --cov=src/caffeinated_whale_cli
```

### Tests Run Twice

This can happen if pytest finds tests in multiple locations. Use `testpaths` in `pyproject.toml` to specify where tests are.

## Future Test Priorities

Status as of 0.34.0 (see [../docs/testing/README.md](../docs/testing/README.md) for the full list):

### Done
1. **Project inspection** (`commands/inspect.py`) - covered by `test_inspect_partial_refresh`, `test_inspect_label_recovery` (~62%).
2. **Database operations** (`utils/db_utils.py`) - covered by `test_db_security`, `test_config_validation` (~68%).
3. **Real-Docker E2E for `init` and `backup`** - covered by `tests/e2e/test_init_e2e.py`, `tests/e2e/test_backup_e2e.py` (both interactive and non-interactive, on the v14/v15/v16 matrix).
3a. **Real-Docker E2E for the lifecycle commands** (`start`, `status`, `logs`, `restart`) - covered by `tests/e2e/test_start_status_e2e.py` (both modes; structure-agnostic outcome invariants that survive the pending start/status core migration - see `openspec/changes/add-start-status-e2e-net`).
4. **Logic core + `cwcli axi`** (`core/`, `commands/axi.py`) - covered by `test_core_envelope`, `test_core_resolvers`, `test_core_backup`, `test_axi` (envelope/resolvers/docker wrapper at 100%, `core/backup.py` ~95%, `commands/axi.py` ~97%).

### Partial
5. **Port conflict detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract; the interactive port-conflict confirmation prompt is now driven end to end in `tests/e2e/test_start_status_e2e.py`, and the remaining port-scanning helpers still have no dedicated unit suite (~59%).

### Still needed
6. **Docker utilities** (`utils/docker_utils.py`) - foundation for all commands; the `get_frappe_container` CLI wrapper is now covered by `test_core_resolvers`, but the rest of the module is still only incidentally covered (~51%).
7. **Port utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
8. **VS Code integration** (`utils/vscode_utils.py`) - container attachment fallback (~16%).
9. **Configuration management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation (~37%).
10. **Real-Docker E2E for the remaining commands** (`rm`, `restore`, `update`/`apps`, `unlock`, `inspect`) and the P2P (`sendme`) loopback - tracked in `openspec/changes/rebuild-e2e-test-suite`.
11. Other command modules at or near 0% dedicated unit coverage: `backup.py` (its logic moved to `core/backup.py`, which is covered), `run.py`, `status.py` (dedicated unit suite still ~0%, but now covered end to end by `tests/e2e/test_start_status_e2e.py`, see item 3a), `unlock.py`.

## Resources

- [Testing Guide](../docs/testing/guide.md) - Comprehensive testing documentation
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Plugin](https://pytest-cov.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
