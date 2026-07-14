# Testing Documentation

This directory contains all testing-related documentation for caffeinated-whale-cli.

The authoritative team norms for validating a change - gate scope, both-modes testing, per-change test-writing discipline, real-instance discipline, and the no-broad-prune rule - are in the [Testing Guide's Gate Policy](./guide.md#gate-policy).

## Two-tier model: fast `unit` vs real-Docker `e2e`

The suite is split into two tiers by pytest marker (registered in `pyproject.toml`; `tests/conftest.py` auto-applies `unit` to anything not marked `e2e`/`e2e_p2p`).

- **`unit`** - fast, needs no Docker daemon, and is the default tier a bare `pytest` runs.
  It verifies pure logic and command wiring against fakes and runs inside the uv container in CI (`test.yml`, `-m unit`, the always-required gate).
  It stays green even with a dead Docker endpoint - that is the proof it is mock-free (`DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest -m unit`).
- **`e2e` / `e2e_p2p`** - real Docker, under `tests/e2e/`.
  These drive the real `cwcli` binary against genuine throwaway Frappe instances (real `cwcli init` up, real side-effect assertions, `cwcli rm` down), are excluded by default, and run on GitHub-hosted `ubuntu-latest` in a v14/v15/v16 matrix (`e2e.yml`).

The migration off the legacy container-mock suite is parallel-run: those tests are carried in the `unit` tier and retired per command as each command's real E2E lands (see [`../../openspec/changes/rebuild-e2e-test-suite`](../../openspec/changes/rebuild-e2e-test-suite)); the mock-free pure-logic tests are kept permanently.
See [`../../tests/README.md`](../../tests/README.md) for the E2E harness (isolation rails, `cwe2e-` backstop, `CWCLI_HOME` seam, `pexpect`/`ESC[?2004h`) and for how to read the run's output (per-test time + description on each `-v` line, and the single two-faced end-of-run summary - Cockpit in colour, Ledger in plain - with its highlighted E2E init-pole callout).

## Quick Start

```bash
# Fast tier only (the default; no Docker needed)
uv run pytest

# Fast tier, explicit + coverage (what CI's unit job runs)
uv run pytest -m unit --cov=caffeinated_whale_cli

# Real-Docker E2E tier (needs a daemon), one Frappe version leg
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m e2e

# Run specific test file
uv run pytest tests/test_completion_utils.py
```

## Documentation

| Guide | Purpose | When to Read |
|-------|---------|--------------|
| **[Testing Guide](./guide.md)** | Complete testing documentation | When writing tests |

## Current Status

### Test Coverage

Measured with `uv run pytest --cov` at 0.37.0 (unreleased): 582 tests across 38 test files, ~58% overall coverage.
Per-area breakdown (highest-coverage module in each area; see the module list in each test file for what else it exercises):

- **rm safety** (`test_rm_safety`, `test_rm_truth`, `test_rm_stopped`) - `commands/rm.py` ~78%
- **restore safety** (`test_restore_safety`, `test_restore_inspect_fixes`) - `commands/restore.py` ~43%
- **inspect freshness** (`test_inspect_partial_refresh`, `test_inspect_label_recovery`, `test_inspect_apps_error`) - `commands/inspect.py` ~63% (includes the `_get_installed_apps` failure path: a non-zero `list-apps` exit warns to stderr and caches `[]`, never the old poisoned sentinel string)
- **bench labels/selectors** (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`) - `utils/bench_labels.py` ~94%
- **yes-flag contract** (`test_yes_flag`) - covers the `confirm_or_exit`/`ensure_containers_running` non-interactive contract across `start`, `config`, `logs`
- **db security** (`test_db_security`) - `utils/db_utils.py` ~68%
- **init reuse + secrets** (`test_init_reuse_bench`, `test_init_mariadb_flag`, `test_init_admin_password`) - `commands/init.py` ~49% (the reuse-bench resolver, the container-readiness poll, the MariaDB-flag branches, and the admin-password/env-secret handling are covered)
- **completion** (`test_completion_utils`) - `utils/completion_utils.py` ~92%
- **tips** (`test_tips`) - `utils/tips.py` ~91%
- **config validation** (`test_config_validation`) - config validation helpers in `utils/db_utils.py`
- **exit codes** (`test_exit_codes`) - cross-command honest-exit-code contract
- **app management** (`test_apps`) - `commands/apps.py` ~91%, `commands/update.py` ~69% (multi-site fan-out, frappe reset, `update` deprecation)
- **logs** (`test_logs`) - `commands/logs.py` ~73% (the `--follow` default flip to `False`, and gating `docker exec -it` on `sys.stdin.isatty()` so a non-TTY/piped invocation never requests a TTY)
- **unlock** (`test_unlock`) - `commands/unlock.py` ~44% (the `test -d` probes and `rm -rfv` locks removal run as argv lists, not `sh -c` string interpolation)
- **`CWCLI_HOME` override** (`test_cwcli_home`) - `utils/config_utils.py`'s `cwcli_home()` ~41% file-wide; mock-free, sets a real `CWCLI_HOME` env var and resolves the import-time footprint constants in a fresh subprocess
- **logic core purity + contract** (`test_core_envelope`) - AST-scans `core/*.py` for the `rich`/`questionary`/`typer` import ban and `typer.Exit`/`confirm_or_exit` references, and pins the `Result`/`CwcliError` DTO shapes - `core/envelope.py`, `core/errors.py` 100%
- **core resolvers + CLI wrappers** (`test_core_resolvers`) - `core/resolvers.py`, `core/docker.py` 100%; also dedicated-tests the thin `commands/utils.py`/`docker_utils.py` wrappers that translate a core choice/error into today's prompts, messages, and exit codes
- **core backup slice** (`test_core_backup`) - `core/backup.py` ~95% (success, both `NEEDS_CHOICE` forks, every `CwcliError` kind, on a fake container)
- **`cwcli axi` surface** (`test_axi`) - `commands/axi.py` ~97% (TOON serializer round-trip, verb exit-mapping 0/1/2, the content-first home, `axi ls`/`axi where`)
- **core read-only slices** (`test_core_list`, `test_core_where`) - `core/list.py`, `core/where.py` 100% (fake docker client / throwaway sqlite - empty/aggregate/DOCKER-raise, dedup/scoping/installed-only/USAGE)
- **`ls`/`where` human frontends** (`test_list`, `test_where`) - `commands/list.py` ~80%, `commands/where.py` 100% (the `--json` empty-`[]` fix, quiet/table rendering, the `--apps`/`--sites` conflict)
- **supervision substrate** (`test_core_supervision`) - `core/supervision.py` ~81% (discovery + label-mapping + bench-keying against faked `ps`/Procfile/exec I/O, the supervisor marker present/absent, the web probe, the launch command shape, the honcho-prefix per-process parse)
- **core start/status slices** (`test_core_start`, `test_core_status`) - `core/start.py` ~98%, `core/status.py` ~91% (the launch outcome, the idempotent no-op, multi-bench `NEEDS_CHOICE`, an explicit `bench_path` used verbatim, every `overall` branch, the stopped-is-offline-not-raised contract vs the nonexistent-project `NOT_FOUND` raise, docker-unreachable errors)
- **`status` human frontend** (`test_status_frontend`) - `commands/status.py` ~91% (the stdout-token-only contract the PR-1 E2E net pins, per-process detail on stderr, exit 0 for a stopped project vs non-zero for a nonexistent one)
- **`axi start`/`axi status` verbs** (`test_axi_start_status`) - TOON rendering, exit-code mapping, needs-choice/`CONFLICT` flag-naming, the never-prompt port-conflict pre-step (core stubbed)

**No dedicated suite** (only incidental coverage from other tests' mocking): `utils/port_utils.py` (~9%), `utils/sendme_utils.py` (~9%), `utils/vscode_utils.py` (~16%).
`utils/docker_utils.py` is now partially covered (~51%, up from ~37%) since `test_core_resolvers` dedicated-tests its `get_frappe_container` CLI wrapper; the rest of the module remains incidental.
**Target**: add dedicated suites for the remaining three modules next.

### Test Files

Run `ls tests/` for the authoritative, current list; as of 0.37.0 (unreleased) it holds 38 `test_*.py` suites plus `bench_fakes.py` and `bench_fakes_mb.py` (shared fakes) and `README.md`.

## Testing Framework

### Stack

- **pytest** - Testing framework
- **pytest-cov** - Coverage reporting
- **unittest.mock** - Mocking library

### Configuration

Test configuration is in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
# The default `-m` deselects the real-Docker tiers, so a bare `pytest` is the
# fast unit tier; `-m e2e` on the CLI overrides it (the last `-m` wins).
addopts = ["-v", "--strict-markers", "--tb=short", "--cov-report=term-missing", "--durations=15", "-m", "not e2e and not e2e_p2p"]
markers = [
    "unit: fast tests that need no Docker daemon (the default tier)",
    "e2e: real-Docker end-to-end tests driving the real cwcli binary",
    "e2e_p2p: real-Docker P2P (sendme loopback) end-to-end tests",
]
```

## Writing Tests

### Test Structure

```python
"""
Tests for module_name.

Brief description of what's being tested.
"""

import pytest
from unittest.mock import Mock, patch

from caffeinated_whale_cli.module import function_to_test


@pytest.fixture
def mock_dependency():
    """Mock external dependency."""
    return Mock()


class TestFeature:
    """Tests for specific feature."""

    def test_happy_path(self, mock_dependency):
        """Should handle normal case correctly."""
        result = function_to_test(mock_dependency)
        assert result == expected_value

    def test_error_handling(self, mock_dependency):
        """Should handle errors gracefully."""
        mock_dependency.method.side_effect = Exception("Error")
        result = function_to_test(mock_dependency)
        assert result is None
```

### Best Practices

1. **One test per behavior**
2. **Descriptive test names** (explain what's being tested)
3. **Comprehensive docstrings**
4. **Test happy path and edge cases**
5. **Mock external dependencies**
6. **Clean up with fixtures**

See [Testing Guide](./guide.md) for detailed documentation.

## Running Tests

### Unit Tier (default; no Docker needed)

```bash
uv run pytest
```

### With Coverage

```bash
uv run pytest --cov
```

### Specific Test File

```bash
uv run pytest tests/test_completion_utils.py
```

### With Verbose Output

```bash
uv run pytest -v
```

### Stop on First Failure

```bash
uv run pytest -x
```

### Show Local Variables on Failure

```bash
uv run pytest -l
```

### Run Tests Matching Pattern

```bash
# Run all tests with "cache" in name
uv run pytest -k cache

# Run specific test class
uv run pytest -k TestCompleteProjectNames
```

## Coverage Reports

### Terminal Report

```bash
uv run pytest --cov --cov-report=term-missing
```

### HTML Report

```bash
uv run pytest --cov --cov-report=html
open htmlcov/index.html
```

### XML Report (for CI/CD)

```bash
uv run pytest --cov --cov-report=xml
```

## Coverage Goals

- **Minimum**: 80% for new code
- **Target**: 90%+ for critical paths
- **Current**: 92% for completion_utils.py

### What to Cover

✅ **Must test:**
- Happy path scenarios
- Error handling
- Edge cases (empty lists, None values, etc.)
- Cache behavior
- Side effects

⚠️ **Nice to have:**
- Performance characteristics
- Integration scenarios
- Multiple code paths

❌ **Can skip:**
- Simple getters/setters
- Framework boilerplate
- External library code

## Test Organization

### By Module

Tests are flat files in `tests/` named after the area they cover (e.g. `test_rm_safety.py`, `test_bench_labels.py`), not a mirrored `test_commands/`/`test_utils/` package tree.
Run `ls tests/` for the current, authoritative list.

### By Type

Three markers are registered in `pyproject.toml`: `unit`, `e2e`, `e2e_p2p`. `tests/conftest.py` auto-applies `unit` to any collected test not already marked `e2e`/`e2e_p2p`, so unit tests need no hand-added marker; only the real-Docker tests under `tests/e2e/` mark themselves explicitly:

```python
import pytest

pytestmark = pytest.mark.e2e

def test_real_docker_behavior(session_instance):
    """Drives the real cwcli binary against a genuine throwaway instance."""
    ...
```

Run by type:
```bash
pytest                # Default -m "not e2e and not e2e_p2p": only the fast unit tier
pytest -m unit         # Explicitly the unit tier
pytest tests/e2e -m e2e  # The real-Docker tier (needs a Docker daemon)
```

## Debugging Tests

### Show Print Output

```bash
uv run pytest -s
```

### Drop into Debugger on Failure

```bash
uv run pytest --pdb
```

### Show Full Traceback

```bash
uv run pytest --tb=long
```

## CI/CD Integration

CI is two-tiered. The fast `unit` tier runs on every push and PR via `.github/workflows/test.yml`:

```yaml
- name: Run unit tests with coverage
  run: uv run pytest -m unit --cov=caffeinated_whale_cli --cov-report=term-missing
```

The real-Docker `e2e` tier runs via `.github/workflows/e2e.yml` on a v14/v15/v16 Frappe matrix, on PRs into `develop`/`master` and on-demand via the `e2e` PR label.

The `Pytest` (unit) job is the always-required gate. See the [CI/CD Workflows guide](../contributing/ci-cd.md) for the full setup.

## Future Test Priorities

Status as of 0.37.0 (based on `ls tests/` and the coverage run above):

### Done
1. **Project Inspection** (`commands/inspect.py`) - covered by `test_inspect_partial_refresh`, `test_inspect_label_recovery` (~62%).
2. **Database Operations** (`utils/db_utils.py`) - covered by `test_db_security`, `test_config_validation` (~68%).
3. **App Management** (`commands/apps.py`, `commands/update.py`) - covered by `test_apps` (~91% / ~69%).
4. **`CWCLI_HOME` override** (`utils/config_utils.py`'s `cwcli_home()`) - covered by `test_cwcli_home`, mock-free (real env var, real filesystem, real subprocess).
5. **Real-Docker E2E for `init` and `backup`** (`tests/e2e/test_init_e2e.py`, `tests/e2e/test_backup_e2e.py`) - genuine `bench init`/`bench backup` against throwaway Frappe instances on the v14/v15/v16 matrix, both interactive and non-interactive. The remaining commands (`rm`, `restore`, `update`/`apps`, `unlock`, `inspect`) and the P2P loopback are deferred to follow-up PRs (`openspec/changes/rebuild-e2e-test-suite`).
5a. **Real-Docker E2E for the lifecycle commands `start`/`status`/`logs`/`restart`** (`tests/e2e/test_start_status_e2e.py`, both modes, v14/v15/v16 matrix) - a structure-agnostic outcome net (a started instance genuinely serves, `status` discriminates the real lifecycle states, `logs` shows the bench stream, `restart` recovers the instance, honest exit codes) that pinned the invariants the start/status core migration had to preserve and abstained from the mechanics it replaced, so it survived that migration unchanged (`openspec/changes/add-start-status-e2e-net`). `tests/e2e/test_start_status_new_behavior_e2e.py` is that migration's own net for the behavior it ADDED (genuine idempotency, `degraded`, real per-process health, the relocated bounded log, the multi-bench refuse) - see item 6a and `openspec/changes/migrate-start-status-core`.
6. **Logic core + `cwcli axi`** (`core/`, `commands/axi.py`) - covered by `test_core_envelope`, `test_core_resolvers`, `test_core_backup`, `test_axi` (envelope/resolvers/docker wrapper at 100%, `core/backup.py` ~95%, `commands/axi.py` ~97%). The read-only `ls`/`list` and `where` slices followed the same pattern onto `core/list.py`/`core/where.py` (both 100%) and thin frontends `commands/list.py` (~80%)/`commands/where.py` (100%), covered by `test_core_list`, `test_core_where`, `test_list`, `test_where`, plus `axi ls`/`axi where` in `test_axi`.
6a. **`start`+`status` onto the logic core** (`core/start.py` ~98%, `core/status.py` ~91%, sharing `core/supervision.py` ~81%) - covered by `test_core_start`, `test_core_status`, `test_core_supervision` (every branch against faked `ps`/Procfile/exec I/O: idempotent no-op, multi-bench `NEEDS_CHOICE`, every `overall` state, the supervisor marker, the honcho discovery/bench-keying). The reseated `commands/status.py` (~91%) is covered by `test_status_frontend` (the stdout-token-only contract), and the new `cwcli axi start`/`axi status` verbs by `test_axi_start_status`.

### Partial
7. **Port Conflict Detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract, and the interactive port-conflict confirmation prompt is driven end to end by `tests/e2e/test_start_status_e2e.py`; the remaining port-scanning helpers still have no dedicated unit suite (~57%).

### Still needed
8. **Docker Utilities** (`utils/docker_utils.py`) - foundation for all commands; the `get_frappe_container` CLI wrapper is now covered by `test_core_resolvers`, but the rest of the module's error handling is untested by a dedicated suite (~51%, up from ~37%).
9. **Port Utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
10. **VS Code Integration** (`utils/vscode_utils.py`) - container attachment fallback logic (~16%).
11. **Configuration Management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation, which `test_config_validation` already covers; `cwcli_home()` is now covered by `test_cwcli_home`, but `load_config`/`save_config` and the custom-path/auto-inspect-config setters remain untested (~41%).
12. Other command modules at or near 0% dedicated unit coverage: `backup.py` (its logic moved to `core/backup.py`, which is covered), `run.py`. `status.py` and `unlock.py` are no longer in this bucket (see item 6a and `test_unlock`).

## Common Issues

### Import Errors

```bash
# Make sure package is installed
uv sync --all-extras
```

### Coverage Not Working

```bash
# Use correct module path
uv run pytest --cov=caffeinated_whale_cli
```

### Tests Run Twice

Use `testpaths` in `pyproject.toml` to specify test location.

## Resources

- [Testing Guide](./guide.md) - Complete testing guide
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [Python Testing Best Practices](https://realpython.com/pytest-python-testing/)

## Contributing Tests

When adding tests:

1. ✅ Follow existing patterns
2. ✅ Use descriptive names
3. ✅ Add comprehensive docstrings
4. ✅ Test happy path and edge cases
5. ✅ Mock external dependencies
6. ✅ Aim for 80%+ coverage
7. ✅ Update this documentation if needed

See [Contributing Guide](../contributing/) for general contribution guidelines.

## Questions?

- **Issues:** Report on [GitHub Issues](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- **Documentation:** Check [Testing Guide](./guide.md)
- **Examples:** See `tests/test_completion_utils.py`
