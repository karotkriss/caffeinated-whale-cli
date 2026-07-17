# Testing Guide

## Overview

This project uses pytest for testing. All tests are located in the `tests/` directory.

The suite is split into two tiers by pytest marker (see [Testing Directory Index](./README.md#two-tier-model-fast-unit-vs-real-docker-e2e) for the full model): a fast `unit` tier (no Docker daemon needed) and a real-Docker `e2e`/`e2e_p2p` tier under `tests/e2e/`. This guide covers writing `unit`-tier tests; see [tests/README.md](../../tests/README.md#e2e-harness-real-docker) for the E2E harness.

## Gate Policy

These are the authoritative team norms for validating a change - the policy behind the mechanics documented in the rest of this guide.
Where a norm refers to mechanics, this section points at where they already live (the [Continuous Integration](#continuous-integration) section, [tests/README.md](../../tests/README.md), and the [E2E testing skill](../../.claude/skills/cwcli-e2e-testing/SKILL.md)) rather than restating them.

### Gate scope: fast tests only; CI owns E2E

The validation gate runs only the tests relevant to your change, on the fast `unit` tier.
A bare `pytest` already deselects the real-Docker tiers - `addopts` in `pyproject.toml` ends in `-m "not e2e and not e2e_p2p"`, and `tests/conftest.py` auto-marks any unmarked test `unit` - so the gate runs the fast tier by design.
Do not spin up or run the E2E suite as part of local validation: the v14/v15/v16 real-Docker matrix in [`.github/workflows/e2e.yml`](../../.github/workflows/e2e.yml) is CI's job, and CI catches the rest.
When a change needs E2E coverage, write or adjust the relevant test and let CI run it.

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

`tests/` measures at ~70% overall coverage (via `uv run pytest --cov` at 0.37.0, unreleased); see [tests/README.md](../../tests/README.md#test-coverage) for the current test/file counts and the [Testing Directory Index](./README.md#current-status) for the full per-area breakdown.
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
- **Current**: ~63% overall at 0.37.0 (unreleased); `completion_utils.py` is the highest-covered module at ~92% (see the [Testing Directory Index](./README.md#current-status) for the rest)

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

The `Pytest` (unit) job is the always-required gate; a second `Mypy` job runs `uv run mypy src/` as a zero-error gate (it fails on any type error). See the [CI/CD Workflows guide](../contributing/ci-cd.md) for details.

### The Windows job: why it exists and why it is narrow

A third job, `Pytest (Windows, auto-inspect)`, runs `tests/test_auto_inspect.py` on `windows-latest`.

It exists because every other job runs `ubuntu-latest`, and that is exactly how a Windows-only defect survived in `utils/auto_inspect.py`: `os.kill(pid, 0)` is an inert liveness probe on POSIX, but on Windows `signal.CTRL_C_EVENT == 0` routes it to `GenerateConsoleCtrlEvent`, which *succeeds for an already-dead pid*.
The daemon therefore reported itself running off a stale PID file and `auto-inspect start` refused with "already running" from then on.
No amount of mocking `sys.platform` finds that; only a real Windows kernel does.
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

Status as of 0.37.0 (see the [Testing Directory Index](./README.md#future-test-priorities) for the full list):

- [x] `core/inspect.py` - the `inspect` logic core: the T1/T2/T3 freshness tier machine, the discovery/gather fan-out, AND the cache write, one contract (`test_core_inspect`); `commands/inspect.py` is now a renderer over it (`test_inspect_partial_refresh`, `test_inspect_label_recovery`); `test_inspect_characterization` is the byte-identical green-before net pinned against the unmigrated command; `cwcli axi inspect` (`test_axi_inspect`) (`openspec/changes/migrate-inspect-core`)
- [x] `utils/db_utils.py` - Cache database operations (`test_db_security`, `test_config_validation`)
- [x] `commands/apps.py`, `commands/update.py` - App management and update-migration logic (`test_apps`); `list`/`install`/`uninstall` moved onto `core/apps.py` and `update` onto `core/update.py` (`test_apps_characterization`, `test_core_apps`, `test_axi_apps_list`, `test_core_update`, `test_axi_apps_update`, `test_update_characterization`), see the Testing Directory Index for the full breakdown
- [x] `utils/config_utils.py`'s `cwcli_home()` - `CWCLI_HOME` override (`test_cwcli_home`, mock-free)
- [~] `commands/start.py` - Port conflict detection; `test_yes_flag` covers the non-interactive contract, and the interactive port-conflict confirmation prompt is driven end to end by `tests/e2e/test_start_status_e2e.py`, but the remaining port-scanning helpers still have no dedicated unit suite
- [ ] `utils/port_utils.py` - Port management
- [~] `utils/docker_utils.py` - Docker interactions; `utf8_stream_decoder` (the exec-stream decode helper) is covered by `test_exec_stream_decode`, but `handle_docker_errors`'s error branches and `exec_into_container` still have no dedicated unit suite. `decode_exec_stream` is gone, superseded by `core/exec_stream.py`
- [x] `core/start.py`, `core/status.py`, `core/supervision.py` - the start/status logic core (`test_core_start`, `test_core_status`, `test_core_supervision`); `commands/status.py` (`test_status_frontend`, `test_status_watch` for the `--watch` live view); `cwcli axi start`/`status` (`test_axi_start_status`)
- [x] Real-Docker E2E for `init` and `backup` (`tests/e2e/test_init_e2e.py`, `tests/e2e/test_backup_e2e.py`) - genuine `bench init`/`bench backup` against throwaway Frappe instances, both interactive and non-interactive
- [x] Real-Docker E2E for the lifecycle commands `start`/`status`/`logs`/`restart` (`tests/e2e/test_start_status_e2e.py`) - a structure-agnostic outcome net that pinned the invariants the start/status core migration had to preserve, both interactive and non-interactive (`openspec/changes/add-start-status-e2e-net`); `tests/e2e/test_start_status_new_behavior_e2e.py` covers the behavior that migration added (`openspec/changes/migrate-start-status-core`); `tests/e2e/test_status_unsupervised_e2e.py` guards the not-cwcli-supervised fallback regression (a bench relaunched under plain honcho reports real per-process state instead of a false all-down), both interactive and non-interactive
- [x] `core/unlock.py`, `core/stop.py` - the `unlock`/`stop` logic core (`test_core_unlock`, `test_core_stop`), the foundation's generality proof (`unlock` migrated with zero new primitives); `cwcli axi unlock`/`stop` (`test_axi_unlock_stop`); Real-Docker E2E for `unlock` in both modes (`tests/e2e/test_unlock_e2e.py`), closing a standing both-modes gap (`openspec/changes/migrate-unlock-stop-core`)
- [x] `core/label.py` - the `label` logic core split into `list_benches`/`set_label`/`clear_label` (`test_core_label`), built with zero new primitives beyond widening `resolvers.resolve_container_state`'s hardcoded hint into a caller-supplied `not_running_hint` parameter; `cwcli axi benches`/`cwcli axi label`/`cwcli axi self-update --check` (`test_axi_label`); Real-Docker E2E for `label` (`tests/e2e/test_label_e2e.py`), the DB/marker two-store consistency invariant against a real container, non-interactive only since `label` has no prompt (`openspec/changes/migrate-label-core`)
- [x] `core/exec_stream.py`, `core/run.py` - the per-exec exec-stream contract (`test_core_exec_stream`) and `run`'s core slice built on it (`test_core_run`), replacing the fail-open that let a bench command with an unknown exit code report success; `apps`/`update` re-pointed at the same primitive rather than migrated as commands (`test_apps` unchanged, plus new coverage for a `CwcliError` from the primitive being caught and rendered instead of escaping raw); Real-Docker E2E for `run` in both modes including a >32KB unicode leg (`tests/e2e/test_run_e2e.py`) (`openspec/changes/add-exec-stream-contract`)
- [x] `core/restore.py` - the `restore` plan/apply split settling the destructive-preview boundary for cwcli's most destructive path (`restore_plan`/`receive_plan -> restore_apply`, `test_core_restore`), the reseated `commands/restore.py` renderer; Real-Docker E2E for `restore` in both modes (`tests/e2e/test_restore_e2e.py`), closing a standing E2E gap (`openspec/changes/migrate-restore-core`)
- [ ] Real-Docker E2E for the remaining commands (`rm`, `apps list`/`install`/`uninstall`, `inspect`) and the P2P (`sendme`) loopback - tracked in `openspec/changes/rebuild-e2e-test-suite`. `update`/`apps update` already closed its own E2E gap (`tests/e2e/test_apps_update_e2e.py`)

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [Python Testing Best Practices](https://realpython.com/pytest-python-testing/)
