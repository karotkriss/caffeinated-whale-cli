# Tests

This directory contains all tests for the caffeinated-whale-cli project.

## Quick Reference

```bash
# Run all tests
uv run pytest

# Run all tests with coverage
uv run pytest --cov

# Run specific test file
uv run pytest tests/test_completion_utils.py

# Run tests matching a pattern
uv run pytest -k "cache"

# Run with verbose output
uv run pytest -v

# Stop on first failure
uv run pytest -x

# Show local variables on failure
uv run pytest -l

# Drop into debugger on failure
uv run pytest --pdb
```

## Test Files

As of 0.34.0, `tests/` holds 18 `test_*.py` suites totaling 329 tests at ~42%
overall coverage (measured with `uv run pytest --cov`). Run `ls tests/` for the
authoritative current list; see [../docs/testing/README.md](../docs/testing/README.md)
for the per-area breakdown.

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

Current overall coverage: **~42%** at 0.34.0 (329 tests across 18 test files).

### Covered Modules
- ✅ `utils/completion_utils.py` - 92% (7 missing lines)
- ✅ `utils/bench_labels.py` - ~94% (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`)
- ✅ `utils/db_utils.py` - ~68% (`test_db_security`, `test_config_validation`)
- ✅ `commands/inspect.py` - ~62% (`test_inspect_partial_refresh`, `test_inspect_label_recovery`)
- ✅ `commands/restore.py` - ~43% (`test_restore_safety`, `test_restore_inspect_fixes`)
- ✅ `commands/rm.py` - ~76% (`test_rm_safety`, `test_rm_truth`, `test_rm_stopped`)

### Modules Needing Dedicated Suites
- ⚠️ `utils/port_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/docker_utils.py` (~37%, only incidental coverage)
- ⚠️ `utils/sendme_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/vscode_utils.py` (~16%, only incidental coverage)
- ⚠️ `utils/config_utils.py` (~37%, distinct from `db_utils`'s config validation)
- ⚠️ Command modules at or near 0% dedicated coverage: `backup.py`, `list.py`, `run.py`, `status.py`, `unlock.py`, `where.py`

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
addopts = ["-v", "--strict-markers", "--tb=short", "--cov-report=term-missing"]
```

Coverage configuration:

```toml
[tool.coverage.run]
source = ["src/caffeinated_whale_cli"]
omit = ["*/tests/*", "*/__init__.py"]
```

## CI/CD

Tests run in CI on every push and PR via `.github/workflows/test.yml`:

```yaml
- name: Run tests with coverage
  run: uv run pytest --cov=caffeinated_whale_cli --cov-report=term-missing
```

The `Pytest` job is the intended required gate. See the [CI/CD Workflows guide](../docs/contributing/ci-cd.md) for the full setup.

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

### Partial
3. **Port conflict detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract, but the port-scanning and interactive-resolution logic has no dedicated suite (~59%).

### Still needed
4. **Docker utilities** (`utils/docker_utils.py`) - foundation for all commands; error handling only incidentally covered (~37%).
5. **Port utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
6. **VS Code integration** (`utils/vscode_utils.py`) - container attachment fallback (~16%).
7. **Configuration management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation (~37%).
8. **Integration tests** - full command workflows, end-to-end scenarios.
9. Other command modules at or near 0%: `backup.py`, `list.py`, `run.py`, `status.py`, `unlock.py`, `where.py`.

## Resources

- [Testing Guide](../docs/testing/guide.md) - Comprehensive testing documentation
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Plugin](https://pytest-cov.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
