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
| **[Coverage Summary](./coverage-summary.md)** | Current test coverage status | To see what's tested |
| **[Implementation Checklist](./implementation-checklist.md)** | Test implementation tracking | To see completed work |

## Current Status

### Test Coverage

- **completion_utils.py**: 92% (27 tests) ✅
- **Overall project**: 7.46%
- **Target**: Expand coverage to port utilities, Docker utils, commands

### Test Files

```
tests/
├── __init__.py
├── README.md
└── test_completion_utils.py    # 27 tests, 92% coverage
```

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

```
tests/
├── test_completion_utils.py      # Completion utilities
├── test_port_utils.py             # Port utilities (future)
├── test_docker_utils.py           # Docker utilities (future)
└── test_commands/                 # Command tests (future)
    ├── test_start.py
    ├── test_inspect.py
    └── test_update.py
```

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

Based on code complexity and criticality:

### High Priority
1. **Port Conflict Detection** (`commands/start.py`)
   - Complex multi-stage logic
   - Interactive prompts
   - Critical UX feature

2. **Project Inspection** (`commands/inspect.py`)
   - Core functionality
   - Cache integration
   - Complex data gathering

3. **Docker Utilities** (`utils/docker_utils.py`)
   - Foundation for all commands
   - Error handling critical

### Medium Priority
4. **Database Operations** (`utils/db_utils.py`)
   - Cache consistency
   - Data integrity

5. **Port Utilities** (`utils/port_utils.py`)
   - Cross-platform behavior
   - Process detection

### Lower Priority
6. **VS Code Integration** (`utils/vscode_utils.py`)
7. **Configuration Management** (`utils/config_utils.py`)
8. Other command modules

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
