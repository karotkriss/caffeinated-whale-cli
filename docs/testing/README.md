# Testing Documentation

This directory contains all testing-related documentation for caffeinated-whale-cli.

## Quick Start

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov

# Run specific test file
uv run pytest tests/test_completion_utils.py
```

## Documentation

| Guide | Purpose | When to Read |
|-------|---------|--------------|
| **[Testing Guide](./guide.md)** | Complete testing documentation | When writing tests |

## Current Status

### Test Coverage

Measured with `uv run pytest --cov` at 0.34.0: 335 tests across 18 test files, ~42% overall coverage.
Per-area breakdown (highest-coverage module in each area; see the module list in each test file for what else it exercises):

- **rm safety** (`test_rm_safety`, `test_rm_truth`, `test_rm_stopped`) - `commands/rm.py` ~78%
- **restore safety** (`test_restore_safety`, `test_restore_inspect_fixes`) - `commands/restore.py` ~43%
- **inspect freshness** (`test_inspect_partial_refresh`, `test_inspect_label_recovery`) - `commands/inspect.py` ~62%
- **bench labels/selectors** (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`) - `utils/bench_labels.py` ~94%
- **yes-flag contract** (`test_yes_flag`) - covers the `confirm_or_exit`/`ensure_containers_running` non-interactive contract across `start`, `config`, `logs`
- **db security** (`test_db_security`) - `utils/db_utils.py` ~68%
- **init reuse** (`test_init_reuse_bench`, `test_init_mariadb_flag`) - `commands/init.py` ~31% (the reuse-bench resolver, the container-readiness poll, and the MariaDB-flag branches are covered)
- **completion** (`test_completion_utils`) - `utils/completion_utils.py` ~92%
- **tips** (`test_tips`) - `utils/tips.py` ~91%
- **config validation** (`test_config_validation`) - config validation helpers in `utils/db_utils.py`
- **exit codes** (`test_exit_codes`) - cross-command honest-exit-code contract

**No dedicated suite** (only incidental coverage from other tests' mocking): `utils/port_utils.py` (~9%), `utils/docker_utils.py` (~37%), `utils/sendme_utils.py` (~9%), `utils/vscode_utils.py` (~16%).
**Target**: add dedicated suites for those four modules next.

### Test Files

Run `ls tests/` for the authoritative, current list; as of 0.34.0 it holds 18 `test_*.py` suites plus `bench_fakes.py` and `bench_fakes_mb.py` (shared fakes) and `README.md`.

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
addopts = ["-v", "--strict-markers", "--tb=short", "--cov-report=term-missing"]
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

### All Tests

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

Use pytest markers:

```python
@pytest.mark.unit
def test_function():
    """Unit test."""
    pass

@pytest.mark.integration
def test_workflow():
    """Integration test."""
    pass

@pytest.mark.slow
def test_performance():
    """Slow test."""
    pass
```

Run by type:
```bash
pytest -m unit           # Only unit tests
pytest -m "not slow"     # Skip slow tests
pytest -m integration    # Only integration tests
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

Tests run in CI on every push and PR via `.github/workflows/test.yml`:

```yaml
- name: Run tests with coverage
  run: uv run pytest --cov=caffeinated_whale_cli --cov-report=term-missing
```

The `Pytest` job is the intended required gate. See the [CI/CD Workflows guide](../contributing/ci-cd.md) for the full setup.

## Future Test Priorities

Status as of 0.34.0 (based on `ls tests/` and the coverage run above):

### Done
1. **Project Inspection** (`commands/inspect.py`) - covered by `test_inspect_partial_refresh`, `test_inspect_label_recovery` (~62%).
2. **Database Operations** (`utils/db_utils.py`) - covered by `test_db_security`, `test_config_validation` (~68%).

### Partial
3. **Port Conflict Detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract, but the port-scanning and interactive-resolution logic itself has no dedicated suite (~59%).

### Still needed
4. **Docker Utilities** (`utils/docker_utils.py`) - foundation for all commands; error handling untested by a dedicated suite (~37%, all incidental).
5. **Port Utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
6. **VS Code Integration** (`utils/vscode_utils.py`) - container attachment fallback logic (~16%).
7. **Configuration Management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation, which `test_config_validation` already covers (~37%).
8. Other command modules at or near 0%: `backup.py`, `list.py`, `run.py`, `status.py`, `unlock.py`, `where.py`.

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
