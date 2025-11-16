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

### `test_completion_utils.py`
Tests for tab completion functionality.

**Coverage:** 92%

**What it tests:**
- Project name completion from Docker
- App name completion from cache
- Site name completion from cache
- Bench name completion from cache
- Cache TTL behavior
- Docker client reuse
- Error handling

**Test classes:**
- `TestCompleteProjectNames` - 7 tests
- `TestCompleteAppNames` - 6 tests
- `TestCompleteSiteNames` - 4 tests
- `TestCompleteBenchNames` - 3 tests
- `TestCacheHelpers` - 4 tests
- `TestGetDockerClient` - 3 tests

## Test Coverage

Current overall coverage: **7.46%** (only completion_utils tested)

### Covered Modules
- ✅ `utils/completion_utils.py` - 92% (9 missing lines)
- ⚠️ `utils/db_utils.py` - 48% (partial coverage from mocking)

### Modules Needing Tests
- ❌ All command modules (0% coverage)
- ❌ `utils/port_utils.py` (0% coverage)
- ❌ `utils/docker_utils.py` (0% coverage)
- ❌ `utils/vscode_utils.py` (0% coverage)
- ❌ `utils/config_utils.py` (0% coverage)

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

Tests should be run in CI/CD pipeline before merging:

```yaml
- name: Run tests with coverage
  run: uv run pytest --cov --cov-report=xml

- name: Upload coverage
  uses: codecov/codecov-action@v3
```

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

1. **Port conflict detection** (`commands/start.py`)
   - Most complex logic in codebase
   - Critical for user experience
   - High risk of regression

2. **Project inspection** (`commands/inspect.py`)
   - Core functionality
   - Cache system testing

3. **Docker utilities** (`utils/docker_utils.py`)
   - Foundation for all commands
   - Error handling critical

4. **Database operations** (`utils/db_utils.py`)
   - Data integrity
   - Cache consistency

5. **Integration tests**
   - Full command workflows
   - End-to-end scenarios

## Resources

- [Testing Guide](../docs/testing/guide.md) - Comprehensive testing documentation
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Plugin](https://pytest-cov.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
