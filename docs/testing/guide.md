# Testing Guide

## Overview

This project uses pytest for testing. All tests are located in the `tests/` directory.

## Running Tests

### Run All Tests

```bash
uv run pytest
```

### Run Specific Test File

```bash
uv run pytest tests/test_completion_utils.py
```

### Run with Verbose Output

```bash
uv run pytest -v
```

### Run with Coverage

```bash
# Coverage for all code
uv run pytest --cov=caffeinated_whale_cli --cov-report=term-missing

# Coverage for specific module
uv run pytest tests/test_completion_utils.py \
  --cov=caffeinated_whale_cli.utils.completion_utils \
  --cov-report=term-missing
```

### Run Tests Matching Pattern

```bash
# Run all tests with "cache" in the name
uv run pytest -k cache

# Run all tests in a specific class
uv run pytest -k TestCompleteProjectNames
```

### Stop on First Failure

```bash
uv run pytest -x
```

## Test Structure

### Current Test Coverage

`tests/` holds 18 `test_*.py` suites totaling 329 tests at ~42% overall coverage (measured with `uv run pytest --cov` at 0.34.0).
See the [Testing Directory Index](./README.md#current-status) for the full per-area breakdown.
`test_completion_utils.py` remains the most complete single-module suite (tab completion, ~92% coverage): project name completion, app name completion, site name completion, cache functionality, Docker client management.

### Test Organization

Tests are organized by functionality using pytest classes:

```python
class TestCompleteProjectNames:
    """Tests for complete_project_names function."""

    def test_returns_unique_sorted_project_names(self):
        """Should return unique, sorted project names from Docker containers."""
        # Test implementation
```

## Writing Tests

### Test Naming Conventions

- Test files: `test_*.py`
- Test classes: `Test*` (e.g., `TestCompleteProjectNames`)
- Test methods: `test_*` (e.g., `test_returns_sorted_names`)
- Use descriptive names that explain what is being tested

### Docstrings

Every test should have a docstring explaining what it tests:

```python
def test_caches_results(self):
    """Should cache results and not query Docker on second call."""
    # Test implementation
```

### Fixtures

Use pytest fixtures for common test setup:

```python
@pytest.fixture
def mock_docker_client():
    """Mock Docker client for testing."""
    client = Mock()
    client.ping = Mock()
    return client
```

### Autouse Fixtures

Use `autouse=True` for setup/teardown that should run for every test:

```python
@pytest.fixture(autouse=True)
def reset_cache():
    """Reset completion cache before each test."""
    completion_utils._cache.clear()
    yield
    completion_utils._cache.clear()
```

### Mocking

Use `unittest.mock` for mocking dependencies:

```python
@patch("caffeinated_whale_cli.utils.completion_utils.docker.from_env")
def test_returns_project_names(self, mock_from_env, mock_docker_client):
    mock_from_env.return_value = mock_docker_client
    # Test implementation
```

## Testing Best Practices

### 1. Test One Thing Per Test

Each test should verify one specific behavior:

```python
# Good
def test_returns_sorted_names(self):
    """Should return sorted names."""
    assert result == ["a", "b", "c"]

def test_removes_duplicates(self):
    """Should remove duplicate entries."""
    assert len(result) == len(set(result))

# Bad
def test_returns_sorted_unique_names(self):
    """Should return sorted unique names."""
    assert result == ["a", "b", "c"]
    assert len(result) == len(set(result))
```

### 2. Test Happy Path and Edge Cases

```python
# Happy path
def test_returns_project_names(self):
    """Should return project names when Docker is available."""

# Edge cases
def test_returns_empty_list_on_docker_error(self):
    """Should return empty list if Docker is unavailable."""

def test_handles_containers_without_project_label(self):
    """Should handle containers missing project label."""
```

### 3. Use Descriptive Assertions

```python
# Good
assert result == ["frappe-one", "frappe-two"]
assert len(result) == 2
assert "frappe-one" in result

# Less clear
assert result
assert len(result)
```

### 4. Clean Up After Tests

Use fixtures with yield for cleanup:

```python
@pytest.fixture
def temp_cache():
    """Create temporary cache for testing."""
    cache = {}
    yield cache
    cache.clear()  # Cleanup
```

## Coverage Goals

- **Minimum**: 80% coverage for new code
- **Target**: 90%+ coverage for critical paths
- **Current**: ~42% overall at 0.34.0; `completion_utils.py` is the highest-covered module at ~92% (see the [Testing Directory Index](./README.md#current-status) for the rest)

### Checking Coverage

```bash
# Generate HTML coverage report
uv run pytest --cov=caffeinated_whale_cli --cov-report=html

# Open report in browser
open htmlcov/index.html
```

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

## Common Testing Patterns

### Testing Functions with Context

```python
def test_uses_project_from_context(self):
    """Should extract project_name from Typer context."""
    ctx = Mock(spec=typer.Context)
    ctx.params = {"project_name": "frappe-one"}

    result = completion_utils.complete_app_names(ctx)

    assert result  # Non-empty result
```

### Testing Caching

```python
def test_caches_results(self, mock_data_source):
    """Should cache results and avoid redundant queries."""
    # First call
    result1 = function_under_test()
    # Second call
    result2 = function_under_test()

    assert result1 == result2
    assert mock_data_source.call_count == 1
```

### Testing Time-Based Behavior

```python
def test_cache_expires_after_ttl(self):
    """Should expire cache after TTL."""
    function_under_test()

    time.sleep(2.1)  # Wait for TTL

    function_under_test()
    assert mock_source.call_count == 2
```

### Testing Error Handling

```python
def test_handles_exception_gracefully(self, mock_source):
    """Should return empty list on exception."""
    mock_source.side_effect = Exception("Error")

    result = function_under_test()

    assert result == []  # Graceful fallback
```

## Continuous Integration

Tests run in CI on every push and PR via `.github/workflows/test.yml`:

```yaml
# .github/workflows/test.yml
- name: Run tests with coverage
  run: uv run pytest --cov=caffeinated_whale_cli --cov-report=term-missing
```

The `Pytest` job is the intended required gate; a second `Mypy` job runs `uv run mypy src/` as a zero-error gate (it fails on any type error). See the [CI/CD Workflows guide](../contributing/ci-cd.md) for details.

## Debugging Tests

### Run with Print Statements

```bash
uv run pytest -s  # Show print() output
```

### Drop into Debugger on Failure

```bash
uv run pytest --pdb  # Drop into pdb on failure
```

### Show Local Variables on Failure

```bash
uv run pytest -l  # Show local variables
```

## Future Test Coverage

Status as of 0.34.0 (see the [Testing Directory Index](./README.md#future-test-priorities) for the full list):

- [x] `commands/inspect.py` - Project inspection logic (`test_inspect_partial_refresh`, `test_inspect_label_recovery`)
- [x] `utils/db_utils.py` - Cache database operations (`test_db_security`, `test_config_validation`)
- [~] `commands/start.py` - Port conflict detection; `test_yes_flag` covers the non-interactive contract, but the port-scanning logic itself has no dedicated suite
- [ ] `utils/port_utils.py` - Port management
- [ ] `utils/docker_utils.py` - Docker interactions
- [ ] Integration tests for full command flows

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [Python Testing Best Practices](https://realpython.com/pytest-python-testing/)
