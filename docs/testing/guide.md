# Testing Guide

## Overview

This project uses pytest for testing. All tests are located in the `tests/` directory.

The suite is split into two tiers by pytest marker (see [Testing Directory Index](./README.md#two-tier-model-fast-unit-vs-real-docker-e2e) for the full model): a fast `unit` tier (no Docker daemon needed) and a real-Docker `e2e`/`e2e_p2p` tier under `tests/e2e/`. This guide covers writing `unit`-tier tests; see [tests/README.md](../../tests/README.md#e2e-harness-real-docker) for the E2E harness.

## Gate Policy

These are the authoritative team norms for validating a change - the policy behind the mechanics documented in the rest of this guide.
Where a norm refers to mechanics, this section points at where they already live (the [Continuous Integration](#continuous-integration) section, [tests/README.md](../../tests/README.md), and the [E2E testing skill](../../.claude/skills/cwcli-e2e-testing/SKILL.md)) rather than restating them.

### Gate scope: fast tests only; CI owns E2E

The validation gate runs the complete fast `unit` tier.
A bare `pytest` also deselects the real-Docker tiers for ordinary local development: `addopts` in `pyproject.toml` ends in `-m "not e2e and not e2e_p2p and not e2e_pkg"`, and `tests/conftest.py` auto-marks any unmarked test `unit`.
Do not spin up or run the E2E suite as part of local validation: the v14/v15/v16 real-Docker matrix in [`.github/workflows/e2e.yml`](../../.github/workflows/e2e.yml) is CI's job, and CI catches the rest.
When a change needs E2E coverage, write or adjust the relevant test and let CI run it.
`commands.test` in [`.no-mistakes.yaml`](../../.no-mistakes.yaml) pins the gate to that fast tier in configuration, so this norm holds without depending on whoever runs the gate having read it.

### Both interactive and non-interactive modes are required

Every prompting command must SUPPORT and be TESTED in BOTH interactive and non-interactive modes.
Interactive means a real human at a TTY sees each prompt and it genuinely collects input.
Non-interactive means every prompt has a flag (`--yes`/`-y` for confirmations, credential flags, the `--site`/backup selectors, ...) so an agent or any non-TTY runs to completion with no prompt, and a non-TTY missing a needed flag refuses with a non-zero exit rather than hanging or silently defaulting.
Drive the interactive path through a real pty; the [E2E testing skill](../../.claude/skills/cwcli-e2e-testing/SKILL.md) documents the pty mechanics (awaiting prompt_toolkit's `ESC[?2004h` raw-mode marker before each keystroke).

### Test-writing discipline (per change)

Scope test changes to what the change actually did:

- A function's behavior changed -> update its test(s) and E2E.
- Genuinely new behavior -> add a new test or E2E.
- Logic refactored but behavior unchanged -> leave its tests untouched.

### Real-instance discipline

When a change genuinely needs a live Frappe instance to validate, spin up EXACTLY ONE throwaway instance (default Frappe v16), reuse that single instance for all real testing through development, and tear it down when done.
Never spin up multiple instances - each `cwcli init` is a slow full bench build.
Test only what relates to the change, never unrelated behavior and never the whole suite.
The [E2E testing skill](../../.claude/skills/cwcli-e2e-testing/SKILL.md) holds the isolation mechanics (the `CWCLI_HOME`/temp-`HOME` seam and the `cwe2e-` project prefix).

### Never broad-prune the shared Docker daemon

Never run system-wide Docker cleanup when testing locally.
No `docker system prune`, no bare `docker container/volume/image prune`, and no `docker rm` or `docker compose down -v` outside a run-scoped sweep - a developer's own Frappe instances share the local daemon, and a broad prune destroys them.
Scope every cleanup to the run's own `cwe2e-`-prefixed / uniquely-labelled resources (the E2E harness already does this on purpose).
If disk is tight, stop and reclaim narrowly rather than broad-pruning the shared daemon.

## Running Tests

### Run the Fast Unit Tier (default)

```bash
uv run pytest
```

### Run the Real-Docker E2E Tier

```bash
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m e2e
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
uv run pytest --cov=caffeinated_whale_cli

# Coverage for specific module, with the per-file table and uncovered line
# numbers. A `--cov-report` on the CLI overrides the addopts default (one total
# %), so this is how you opt back into the detail when you actually want it.
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

For the commands that report current test and coverage totals, plus the point-in-time per-module breakdown, see [tests/README.md](../../tests/README.md#test-coverage), the single source.
`test_completion_utils.py` (tab completion) is a good single-module suite to model a new one on: project name completion, app name completion, site name completion, cache functionality, Docker client management.

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

The docstring documents the test for whoever reads the file next; the run itself stays quiet about it. See [tests/README.md](../../tests/README.md#reading-the-output) for what a run actually prints.

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
- **Current**: see [tests/README.md](../../tests/README.md#test-coverage) for the commands that report current coverage and the point-in-time per-module breakdown

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

CI is two-tiered. The fast `unit` tier runs on every push and PR via `.github/workflows/test.yml`:

```yaml
# .github/workflows/test.yml
- name: Run unit tests with coverage
  run: uv run pytest -m unit --cov=caffeinated_whale_cli
```

The real-Docker `e2e` tier runs via `.github/workflows/e2e.yml` on a v14/v15/v16 Frappe matrix, on PRs into `develop`/`master` and on-demand via the `e2e` PR label.
Each version leg is split into a `shared` and a `standalone` job by the `standalone` marker, which roughly halves the workflow's wall clock without dropping a single test; see the [CI/CD Workflows guide](../contributing/ci-cd.md#e2e-githubworkflowse2eyml).

See the [CI/CD Workflows guide](../contributing/ci-cd.md) for the full job list and current required checks.

### The Windows job: why it exists and why it is narrow

Another job, `Pytest (Windows, native)`, runs `tests/test_auto_inspect.py` and `tests/test_core_credbridge.py` on `windows-latest`.

It exists because every other job runs `ubuntu-latest`, and that is exactly how Windows-only defects survive.
Two are in this repo's history.
First, `utils/auto_inspect.py`: `os.kill(pid, 0)` is an inert liveness probe on POSIX, but on Windows `signal.CTRL_C_EVENT == 0` routes it to `GenerateConsoleCtrlEvent`, which *succeeds for an already-dead pid*, so the daemon reported itself running off a stale PID file and `auto-inspect start` refused with "already running" from then on.
Second, `core/credbridge.py`: the git credential bridge bound an `AF_UNIX` socket, but Windows CPython has no `socket.AF_UNIX`, so `cwcli init --frappe-url <private fork>` crashed with `AttributeError` the instant it entered the bridge; the fix routes the Windows host onto a loopback-TCP transport, and the credbridge tests drive that transport on a real Windows kernel.
No amount of mocking `sys.platform` finds either; only a real Windows kernel does.
The repo is public, so `windows-latest` minutes are free - there is no cost argument for leaving this class of bug uncovered.

The job is **deliberately narrow**, and the honest reason is that widening it is unproven work rather than a line of YAML.
The Linux jobs run inside a `ghcr.io/astral-sh/uv` container that a Windows runner cannot use, so this job stands alone with `astral-sh/setup-uv`; and the rest of the unit tier has never been exercised on Windows, so pointing `-m unit` at this runner would be a guess.
Running the full unit tier on Windows is worth doing and is tracked as its own task.

When you touch platform-dependent process, path, or signal handling, add the test here rather than assuming the Linux jobs cover it.

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

The prioritized checklist of what still needs dedicated suites and what has already been done lives in [tests/README.md](../../tests/README.md#future-test-priorities) - the single source.

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [Python Testing Best Practices](https://realpython.com/pytest-python-testing/)
