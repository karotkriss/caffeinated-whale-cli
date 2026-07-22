# Testing Documentation

This directory contains all testing-related documentation for caffeinated-whale-cli.

The authoritative team norms for validating a change - gate scope, both-modes testing, per-change test-writing discipline, real-instance discipline, and the no-broad-prune rule - are in the [Testing Guide's Gate Policy](./guide.md#gate-policy).

## Two-tier model: fast `unit` vs real-Docker `e2e`

The suite is split into two tiers by pytest marker (registered in `pyproject.toml`; `tests/conftest.py` auto-applies `unit` to anything not marked `e2e`/`e2e_p2p`).

- **`unit`** - fast, needs no Docker daemon, and is the default tier a bare `pytest` runs.
  It verifies pure logic and command wiring against fakes and runs inside the uv container in CI (`test.yml`, `-m unit`, the always-required gate).
  It stays green even with a dead Docker endpoint - that is the proof it is mock-free (`DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest -m unit`).
- **`e2e` / `e2e_p2p` / `e2e_pkg`** - real Docker, under `tests/e2e/`.
  These drive the real `cwcli` binary against genuine throwaway Frappe instances (real `cwcli init` up, real side-effect assertions, `cwcli rm` down), are excluded by default, and run on GitHub-hosted `ubuntu-latest` in a v14/v15/v16 matrix (`e2e.yml`).

The migration off the legacy container-mock suite is parallel-run: those tests are carried in the `unit` tier and retired per command as each command's real E2E lands (see [`../../openspec/changes/rebuild-e2e-test-suite`](../../openspec/changes/rebuild-e2e-test-suite)); the mock-free pure-logic tests are kept permanently.
See [`../../tests/README.md`](../../tests/README.md) for the E2E harness (isolation rails, `cwe2e-` backstop, `CWCLI_HOME` seam, `pexpect`/`ESC[?2004h`) and for how to read the run's output (one line per test file plus one total coverage % when green; the failing test's name and traceback when red).

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

[tests/README.md](../../tests/README.md#test-coverage) is the single source for commands that report current test and coverage totals, plus the point-in-time per-module breakdown.
That is also where the list of modules with no dedicated suite yet is kept.

### Test Files

Run `ls tests/` for the authoritative, current list (`bench_fakes.py` and `bench_fakes_mb.py` hold the shared fakes); see [tests/README.md](../../tests/README.md#test-files) for the current suite count.

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
# No `[ 42%]` progress indicator: the fast tier prints its own per-file progress.
console_output_style = "classic"
# The default `-m` deselects the real-Docker tiers, so a bare `pytest` is the
# fast unit tier; `-m e2e` on the CLI overrides it (the last `-m` wins).
addopts = ["-q", "--strict-markers", "--tb=short", "--cov-report=", "-m", "not e2e and not e2e_p2p and not e2e_pkg"]
markers = [
    "unit: fast tests that need no Docker daemon (the default tier)",
    "e2e: real-Docker end-to-end tests driving the real cwcli binary",
    "e2e_p2p: real-Docker P2P (sendme loopback) end-to-end tests (also carries e2e, version-gated)",
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

A run prints one total coverage %. To opt back into the per-file table and the
uncovered line numbers, pass `--cov-report` on the CLI (it overrides the addopts
default):

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
- **Current**: see [tests/README.md](../../tests/README.md#test-coverage) for the current overall and per-module numbers

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

Four markers are registered in `pyproject.toml`: `unit`, `e2e`, `e2e_p2p`, `e2e_pkg`. `tests/conftest.py` auto-applies `unit` to any collected test not already marked `e2e`/`e2e_p2p`/`e2e_pkg`, so unit tests need no hand-added marker; only the real-Docker tests under `tests/e2e/` mark themselves explicitly (`e2e_pkg` is the runtime-deps-only packaging leg, off the `-m e2e` matrix):

```python
import pytest

pytestmark = pytest.mark.e2e

def test_real_docker_behavior(session_instance):
    """Drives the real cwcli binary against a genuine throwaway instance."""
    ...
```

Run by type:
```bash
pytest                # Default -m "not e2e and not e2e_p2p and not e2e_pkg": only the fast unit tier
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
  run: uv run pytest -m unit --cov=caffeinated_whale_cli
```

The real-Docker `e2e` tier runs via `.github/workflows/e2e.yml` on a v14/v15/v16 Frappe matrix, on PRs into `develop`/`master` and on-demand via the `e2e` PR label.

The `Pytest` (unit) job is the always-required gate. See the [CI/CD Workflows guide](../contributing/ci-cd.md) for the full setup.

## Future Test Priorities

The prioritized checklist of what still needs dedicated suites, what is partial, and what is done lives in [tests/README.md](../../tests/README.md#future-test-priorities) - the single source.

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
