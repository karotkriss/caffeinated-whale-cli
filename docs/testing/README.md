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

Measured with `uv run pytest --cov` at 0.37.0 (unreleased): ~70% overall coverage; see [tests/README.md](../../tests/README.md#test-coverage) for the current test/file counts.
Per-area breakdown (highest-coverage module in each area; see the module list in each test file for what else it exercises):

- **rm safety** (`test_rm_safety`, `test_rm_truth`, `test_rm_stopped`, `test_rm_stopped_backup`, `test_rm_characterization`, `test_core_rm`) - the delete-with-backup-gate state machine now lives in `core/rm.py`; `commands/rm.py` is a renderer
- **`restore` onto the logic core** (`test_core_restore`, `test_restore_characterization`, `test_restore_safety`, `test_restore_inspect_fixes`) - `core/restore.py` (the plan/apply split `restore_plan`/`receive_plan -> restore_apply`), the reseated `commands/restore.py` renderer; see `tests/README.md` for the full per-suite breakdown
- **inspect freshness** (`test_inspect_characterization`, `test_core_inspect`, `test_inspect_partial_refresh`, `test_inspect_label_recovery`, `test_inspect_apps_error`) - the T1/T2/T3 tier machine and cache write now live in `core/inspect.py`; `commands/inspect.py` is a renderer (includes the `_get_installed_apps` failure path: a non-zero `list-apps` exit warns to stderr and caches `[]`, never the old poisoned sentinel string)
- **bench labels/selectors** (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`) - `utils/bench_labels.py` ~94%
- **yes-flag contract** (`test_yes_flag`) - covers the `confirm_or_exit`/`ensure_containers_running` non-interactive contract across `start`, `config`, `logs`
- **db security** (`test_db_security`) - `utils/db_utils.py` ~68%
- **auto-inspect daemon** (`test_auto_inspect`) - `utils/auto_inspect.py`'s first-ever dedicated suite: the Windows `WaitForSingleObject`-based `_pid_alive` probe (immune to the `os.kill(pid, 0)`/`CTRL_C_EVENT` false-alive bug), the fork-unavailable subprocess fallback and its Windows-path bootstrap source, `_spawn_detached` routing the child's stderr to the log file instead of `DEVNULL`, `_log(exc_info=True)` recording the traceback, and stale-PID-file cleanup
- **`init` onto the logic core** (`test_core_init`, `test_init_characterization`, `test_init_reuse_bench`, `test_init_admin_password`, `test_init_frappe_version`, `test_init_mariadb_flag`) - `core/init.py` (the two-call `init_instance`/`init_bench` provisioning slice), the reseated `commands/init.py` renderer; see `tests/README.md` for the full per-suite breakdown
- **completion** (`test_completion_utils`) - `utils/completion_utils.py` ~92%
- **tips** (`test_tips`) - `utils/tips.py` ~91%
- **config validation** (`test_config_validation`) - config validation helpers in `utils/db_utils.py`
- **exit codes** (`test_exit_codes`) - cross-command honest-exit-code contract
- **app management** (`test_apps`) - `commands/apps.py` ~98% (multi-site fan-out, frappe reset, `update` deprecation)
- **`update` onto the logic core, plus `apps update --json`/`axi apps update`** (`test_core_update`, `test_axi_apps_update`, `test_update_characterization`) - `core/update.py` ~90%, the reseated `commands/update.py` renderer ~88% (the maintenance-mode `finally` surviving a mid-fan-out stream loss with remediation intact, unknown-vs-failed reporting for a lost output stream, the `UpdateAborted` callback on a Ctrl-C, both structured surfaces incl. frappe-path stdout purity, and the seven-way aggregation across every flag)
- **`apps`'s remaining subcommands onto the logic core, plus `cwcli axi apps list`** (`test_apps_characterization`, `test_core_apps`, `test_axi_apps_list`) - `core/apps.py` ~98%, the reseated `commands/apps.py` renderer ~98% (`list`/`install`/`uninstall` joined `update` on the core, so the module no longer carries two contracts; the green-before characterization net taking `commands/apps.py` from 91.28% to 99.49% before anything moved; the forks only `axi`/a future GUI can reach - `confirm_start`, `select_bench`, `confirm_uninstall`, the no-cache default; the new `axi apps list` verb's null-vs-empty-list distinction on a failed site read; and a dedicated assertion that `axi apps install`/`uninstall` are deliberately NOT registered)
- **`logs` onto the logic core** (`test_core_logs`, `test_logs`) - `core/logs.py` 100% (the resolve migrated off `commands/logs.py`: the combined/`--process` log selection, the `select_bench`/`confirm_start`/`select_process` `NEEDS_CHOICE` forks, the two distinct no-logs errors - `logs.none_yet` NOT_FOUND when a manager is up vs `logs.no_manager` NOT_RUNNING with the start hint - the not-cwcli-supervised fallback firing and not firing, and the `subprocess`-ban purity check), the reseated `commands/logs.py` renderer ~80% (the `--follow` default flip to `False`, gating `docker exec -it` on `sys.stdin.isatty()` so a non-TTY/piped invocation never requests a TTY, and PR #83's exit-code fix pinned through the public surface)
- **unlock** (`test_unlock`, `test_unlock_command_cli`) - `commands/unlock.py` ~50% (the `test -d` probes and locks removal run as argv lists, not `sh -c` string interpolation; its logic moved to `core/unlock.py` - see the core unlock/stop slice below)
- **`CWCLI_HOME` override** (`test_cwcli_home`) - `utils/config_utils.py`'s `cwcli_home()` ~41% file-wide; mock-free, sets a real `CWCLI_HOME` env var and resolves the import-time footprint constants in a fresh subprocess
- **logic core purity + contract** (`test_core_envelope`) - AST-scans `core/*.py` for the `rich`/`questionary`/`typer` import ban and `typer.Exit`/`confirm_or_exit` references, and pins the `Result`/`CwcliError` DTO shapes - `core/envelope.py`, `core/errors.py` 100%
- **core resolvers + CLI wrappers** (`test_core_resolvers`) - `core/resolvers.py` ~97%, `core/docker.py` 100%; also dedicated-tests the thin `commands/utils.py`/`docker_utils.py` wrappers that translate a core choice/error into today's prompts, messages, and exit codes. The shared bench-op helpers (`resolve_default_site`, `validate_site_name`, `validate_bench_path`, `require_bench_dir`, `require_site_dir`), extracted out of `core/backup.py` on `unlock`'s migration, are exercised via `test_core_backup`/`test_core_unlock` instead
- **core backup slice** (`test_core_backup`) - `core/backup.py` ~95% (success, both `NEEDS_CHOICE` forks, every `CwcliError` kind, on a fake container)
- **core unlock/stop slices** (`test_core_unlock`, `test_core_stop`) - `core/unlock.py` ~98% (built from the same primitives as `core/backup.py` with zero new ones - the foundation's generality proof; the locks removal is a single buffered `exec_run` parsed into a structured `removed` list, never streamed), `core/stop.py` 100% (stopped count, already-stopped, `NOT_FOUND`, docker-unreachable, names-not-objects, prints nothing at all)
- **exec-stream contract + core run slice** (`test_core_exec_stream`, `test_core_run`) - `core/exec_stream.py` ~97% (tagged chunks, per-stream mid-character splits, the bounded exit-code poll settling vs. expiring, a dropped connection - whether mid-stream or mid-poll - raised as `CwcliError(DOCKER)` rather than guessed), `core/run.py` 100% (every `run_plan` branch plus the reseated `commands/run.py` frontend: exit-code passthrough, honest non-zero on an unknown code, `--bench`/`--path` plumbing, the `confirm_start` retry-once-then-fail-closed race). `apps`/`update` are re-pointed at the same primitive (not migrated as commands); `test_apps` pins that a `CwcliError` from it is caught and rendered rather than escaping as a raw traceback
- **`cwcli axi` surface** (`test_axi`) - `commands/axi.py` ~97% (TOON serializer round-trip, verb exit-mapping 0/1/2, the content-first home, `axi ls`/`axi where`)
- **AXI cross-cutting shell** (`test_agent_hooks`, `test_axi_skill`) - `utils/agent_hooks.py` 100% (the `cwcli axi setup` installer against a throwaway `tmp_path` home: install into all three detected harnesses, an undetected one skipped rather than bootstrapped, the idempotent re-install, a moved executable repaired in place, foreign hooks/settings surviving, a corrupt config replaced, the Codex `[features] hooks = true` append preserving comments), `scripts/build_skill.py` 100% (the `--check` staleness gate driven as a unit test, every registered `axi` verb appearing in the generated skill, and `test_a_new_verb_makes_the_committed_skill_stale` proving the gate has teeth), and `TestInternalSkillsAreNotPublished` (every `.claude/skills/*/SKILL.md` carries `metadata: internal: true` so `skills add` never publishes the internal deep-dives, and the public `skills/cwcli/SKILL.md` never gains that marker)
- **core read-only slices** (`test_core_list`, `test_core_where`) - `core/list.py`, `core/where.py` 100% (fake docker client / throwaway sqlite - empty/aggregate/DOCKER-raise, dedup/scoping/installed-only/USAGE)
- **`ls`/`where` human frontends** (`test_list`, `test_where`) - `commands/list.py` ~80%, `commands/where.py` 100% (the `--json` empty-`[]` fix, quiet/table rendering, the `--apps`/`--sites` conflict)
- **supervision substrate** (`test_core_supervision`) - `core/supervision.py` ~82% (supervisord discovery + label-mapping + bench-keying against faked `ps`/Procfile/exec I/O, config/launcher generation, the `pip install supervisor` fail-closed bootstrap, the supervisor marker present/absent, the web probe, per-process log-path resolution, `discover_unsupervised_stack`'s honcho/`bench start` fallback, the real `frappe <cmd>` bench-helper cmdline forms)
- **core start/status/restart slices** (`test_core_start`, `test_core_status`, `test_core_restart`) - `core/start.py` ~98%, `core/status.py` ~93%, `core/restart.py` ~95% (the launch outcome, the `--autorestart` config-state toggle, the idempotent no-op, multi-bench `NEEDS_CHOICE`, an explicit `bench_path` used verbatim, every `overall` branch incl. the stable-partial-stack `degraded`, the stopped-is-offline-not-raised contract vs the nonexistent-project `NOT_FOUND` raise, docker-unreachable errors, single-program restart leaving siblings running with unknown/ambiguous-process `NEEDS_CHOICE`, and the not-cwcli-supervised honcho fallback reporting real up/pid/uptime instead of a false all-down)
- **`status` human frontend** (`test_status_frontend`) - `commands/status.py` ~93% (the stdout-token-only contract the PR-1 E2E net pins, per-process detail incl. supervisord `state` on stderr, exit 0 for a stopped project vs non-zero for a nonexistent one, the not-cwcli-supervised heading and hint on stderr)
- **`status --watch` live view** (`test_status_watch`) - the load-bearing behavior that every watch tick re-polls with `probe_web=False` (zero `curl localhost:8000` calls), the non-TTY-either-stream single-quiet-snapshot degrade, the `--interval` 1s floor, and a clean `KeyboardInterrupt` exit (exit 0, nothing on stdout)
- **`axi start`/`axi status` verbs** (`test_axi_start_status`) - TOON rendering, exit-code mapping, needs-choice/`CONFLICT` flag-naming, the never-prompt port-conflict pre-step (core stubbed)
- **`axi restart` verb** (`test_axi_restart`) - TOON `ProcessRestartOutcome` rendering, exit-code mapping, `--process` required, unknown/ambiguous process and multi-bench `select_bench` as usage errors listing valid labels (core stubbed)
- **`axi unlock`/`axi stop` verbs** (`test_axi_unlock_stop`) - TOON `UnlockOutcome`/`StopOutcome` rendering incl. `removed` as a structured list, exit-code mapping, `axi unlock`'s multi-bench `select_bench` and stopped-container `confirm_start` as usage errors (deliberately NO `--yes` on this verb - it mirrors `axi backup` exactly), `axi stop`'s idempotent already-stopped success and structured not-found error
- **`self-update`** (`test_core_version`, `test_self_update`) - `core/version.py` ~90% (install-method detection tree, fail-open PyPI lookup, PEP 440 compare incl. the dev-ahead case, the ~1-day TTL cache, the cache-only `passive_notice` gate and its `attempted_at` once/day refresh throttle), `commands/self_update.py` 100% (dev/uvx no-op, the default upgrade run, `--check`, `--no-cache`)
- **passive update notice** (`test_update_notice`) - `update_notice.py` ~95% (stderr-only rendering, gated on `sys.stderr.isatty()`, `CWCLI_NO_UPDATE_CHECK` suppression, fail-open when the core gate raises; the core `passive_notice` gate itself is faked here and dedicated-tested in `test_core_version`)

**No dedicated suite** (only incidental coverage from other tests' mocking): `utils/port_utils.py` (~9%), `utils/sendme_utils.py` (~9%), `utils/vscode_utils.py` (~15%, down from ~16% after the dead editor-selection helpers were deleted in `migrate-open-core`).
`utils/docker_utils.py` is now partially covered (~58%) since `test_exec_stream_decode` dedicated-tests its `utf8_stream_decoder` helper on top of `test_core_resolvers`'s `get_frappe_container` CLI wrapper; the rest of the module remains incidental. `decode_exec_stream` is GONE - `core/exec_stream.py` superseded it and its only three callers were re-pointed at that primitive.
**Target**: add dedicated suites for the remaining three modules next.

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

Status as of 0.37.0 (based on `ls tests/` and the coverage run above):

### Done
1. **Project Inspection** (`commands/inspect.py`, now a renderer over `core/inspect.py`) - covered by `test_inspect_partial_refresh`, `test_inspect_label_recovery`; the core migration itself is item 6g.
2. **Database Operations** (`utils/db_utils.py`) - covered by `test_db_security`, `test_config_validation` (~68%).
3. **App Management** (`commands/apps.py`, `commands/update.py`) - `commands/apps.py`'s `list`/`install`/`uninstall` moved to `core/apps.py` (~98%), the reseated renderer is ~98%, see item 6f; `commands/update.py`'s state machine moved to `core/update.py` (~90%), the reseated renderer is ~88%, see item 6e.
4. **`CWCLI_HOME` override** (`utils/config_utils.py`'s `cwcli_home()`) - covered by `test_cwcli_home`, mock-free (real env var, real filesystem, real subprocess).
5. **Real-Docker E2E for `init` and `backup`** (`tests/e2e/test_init_e2e.py`, `tests/e2e/test_backup_e2e.py`) - genuine `bench init`/`bench backup` against throwaway Frappe instances on the v14/v15/v16 matrix, both interactive and non-interactive. The remaining commands (`rm`, `inspect`, `apps list`/`install`/`uninstall`) and the P2P loopback are deferred to follow-up PRs (`openspec/changes/rebuild-e2e-test-suite`); `unlock` closed its own gap (item 6d), `update`/`apps update` closed theirs (item 6e), and `restore` closed its own gap (item 6i) - `apps`'s other three subcommands moving onto the core (item 6f) is unit-tier only and does not close this gap, by design.
5a. **Real-Docker E2E for the lifecycle commands `start`/`status`/`logs`/`restart`** (`tests/e2e/test_start_status_e2e.py`, both modes, v14/v15/v16 matrix) - a structure-agnostic outcome net (a started instance genuinely serves, `status` discriminates the real lifecycle states, `logs` shows the bench stream, `restart` recovers the instance, honest exit codes) that pinned the invariants the start/status core migration had to preserve and abstained from the mechanics it replaced, so it survived that migration unchanged (`openspec/changes/add-start-status-e2e-net`). `tests/e2e/test_start_status_new_behavior_e2e.py` is that migration's own net for the behavior it ADDED (genuine idempotency, `degraded`, real per-process health, the relocated bounded log, the multi-bench refuse) - see item 6a and `openspec/changes/migrate-start-status-core`. `tests/e2e/test_per_process_supervisor_e2e.py` is `add-per-process-supervisor`'s own net for the features honcho's all-or-nothing model made impossible (supervisord as the live in-container supervisor, `cwcli restart --process`/`cwcli axi restart --process` cycling one program while its siblings keep their pids, auto-heal after a killed process, `cwcli logs --process`, the unknown-process usage error) - see item 6a. `tests/e2e/test_status_unsupervised_e2e.py` guards the not-cwcli-supervised FALLBACK regression: with the bench's supervisord stopped and the Procfile relaunched under plain honcho, `status`/`axi status` report the real per-process up/pid state (not a false all-down), flag `not_cwcli_supervised`, and never launch supervisord themselves, in both interactive and non-interactive modes; the supervisord path is restored afterward so sibling tests are undisturbed - see item 6a.
6. **Logic core + `cwcli axi`** (`core/`, `commands/axi.py`) - covered by `test_core_envelope`, `test_core_resolvers`, `test_core_backup`, `test_axi` (envelope/resolvers/docker wrapper at 100%, `core/backup.py` ~95%, `commands/axi.py` ~97%). The read-only `ls`/`list` and `where` slices followed the same pattern onto `core/list.py`/`core/where.py` (both 100%) and thin frontends `commands/list.py` (~80%)/`commands/where.py` (100%), covered by `test_core_list`, `test_core_where`, `test_list`, `test_where`, plus `axi ls`/`axi where` in `test_axi`.
6a. **`start`+`status`+`restart` onto the logic core** (`core/start.py` ~98%, `core/status.py` ~93%, `core/restart.py` ~95%, sharing `core/supervision.py` ~82%) - covered by `test_core_start`, `test_core_status`, `test_core_restart`, `test_core_supervision` (every branch against faked `ps`/Procfile/exec I/O: idempotent no-op, the `--autorestart` config-state toggle, multi-bench `NEEDS_CHOICE`, every `overall` state including the stable-partial-stack `degraded` and the `probe_web=False`/`web_probed` branch the `--watch` loop uses, the supervisor marker, supervisord discovery/bench-keying, single-program restart leaving siblings running, and the not-cwcli-supervised honcho/`bench start` fallback reporting real up/pid/uptime instead of a false all-down). The reseated `commands/status.py` (~93%) is covered by `test_status_frontend` (the stdout-token-only contract, the not-cwcli-supervised heading and hint) and its `--watch` live view by `test_status_watch` (zero web-probe calls, non-TTY degrade, `--interval` floor, clean `KeyboardInterrupt` exit), and the `cwcli axi start`/`axi status`/`axi restart` verbs by `test_axi_start_status`/`test_axi_restart`. `add-per-process-supervisor` re-pointed the substrate from honcho to supervisord and added `core/restart.py` + the `--process`/`--autorestart` command surface; see `openspec/changes/add-per-process-supervisor`.
6b. **`self-update`** (`commands/self_update.py` 100%, `core/version.py` ~90%) - covered by `test_self_update` (dev/uvx no-op, the default upgrade run incl. failure/`FileNotFoundError`, `--check`, `--no-cache`) and `test_core_version` (install-method detection tree, fail-open PyPI lookup, PEP 440 compare incl. the dev-ahead case, the TTL cache). No `cwcli axi` verb (deferred).
6c. **Passive update notice** (`update_notice.py` ~95%, sharing `core/version.py`'s `passive_notice` gate above) - covered by `test_update_notice` (stderr-only, TTY-gated, `CWCLI_NO_UPDATE_CHECK`-suppressible, fail-open) and `test_core_version`'s `TestPassiveNotice` class (cache-only hot path, the detached background refresh, the `attempted_at` once/day throttle on persistent failure). Wired once into `main.py`'s root Typer callback, so it covers both the human CLI and every `cwcli axi` verb.
6d. **`unlock`+`stop` onto the logic core** (`core/unlock.py` ~98%, `core/stop.py` 100%, sharing `core/resolvers.py`'s newly-extracted bench-op helpers ~97%) - the foundation's generality proof: `unlock` is `backup`'s near-twin and was migrated with zero new primitives. Covered by `test_core_unlock`, `test_core_stop`, `test_unlock` (the re-pointed argv-injection guards), `test_unlock_command_cli` (the `--bench` selector resolution), and the `cwcli axi unlock`/`cwcli axi stop` verbs by `test_axi_unlock_stop`. `tests/e2e/test_unlock_e2e.py` closes `unlock`'s standing both-modes E2E gap (pty-driven interactive leg, non-interactive `--yes` leg, real locks-directory removal against a genuine instance); `stop` needed no new E2E, already pinned by `test_start_status_e2e.py`/`test_status_unsupervised_e2e.py`. See `openspec/changes/migrate-unlock-stop-core`.
6e. **`update` onto the logic core, plus `apps update --json`/`cwcli axi apps update`** (`core/update.py` ~90%, the reseated `commands/update.py` renderer ~88%) - the update state machine's coverage went from 71.06% to 90.46%. Covered by `test_core_update` (the envelope, the maintenance-mode `finally` surviving a raise, `UpdateAborted` on a Ctrl-C, unknown-vs-failed, the frappe fork), `test_axi_apps_update` (both structured surfaces, stdout purity on the frappe path, exit 0/1/2 driven by `report.ok`), and `test_update_characterization` (the seven-way aggregation and every flag, written before the migration and passing byte-identical either side of it). `tests/e2e/test_apps_update_e2e.py` closes `apps`/`update`'s standing E2E gap: both modes on a real instance, with `bench` shimmed for the output-purity legs. See `openspec/changes/migrate-update-core`.
6f. **`apps`'s remaining three subcommands onto the logic core, plus `cwcli axi apps list`** (`core/apps.py` ~98%, the reseated `commands/apps.py` renderer ~98%) - finishes what 6e left half-migrated: `list`/`install`/`uninstall` join `update` on the core, so `commands/apps.py` no longer carries two contracts. Covered by `test_apps_characterization` (the green-before net, committed before anything moved, taking `commands/apps.py` from 91.28% to 99.49%), `test_core_apps` (every branch only `axi`/a future GUI can reach - `confirm_start`, `select_bench`, `confirm_uninstall`, the no-cache default - plus that the core prints nothing and returns plain serializable data), and `test_axi_apps_list` (TOON rendering, exit 0/1/2, a failed site read as `null` rather than an empty list, and a dedicated assertion that `axi apps install`/`uninstall` are deliberately unregistered). No new E2E - `list`/`install`/`uninstall` remain in item 5's gap by design. See `openspec/changes/migrate-apps-core`.
6g. **`inspect` onto the logic core, plus `cwcli axi inspect`** (`core/inspect.py` - the T1/T2/T3 freshness tier machine, the discovery/gather fan-out, AND the cache write, one contract; `commands/inspect.py` reseated as a renderer) - kills the frontend-calling-frontend class at all seven consumer edges (`recache_project`, `auto_inspect`, `open`'s fallback populate + in-memory `--app` freshness pass, `update`'s fallback, `restore`'s three fallback copies, `rm`'s live discovery) and with it `core/update.py`'s last runtime reach into the CLI layer. `test_inspect_characterization` is the green-before net (byte-identical `--json` per tier, the persisted cache shape, T2 passivity under `--yes`, the T3 `--yes`/non-TTY contract), `test_core_inspect` pins the envelope at the core (every tier fork, the two disclosed hardenings, plain-data DTOs), and `test_axi_inspect` covers the new tiered read verb (nested bench records as TOON, exit 0/1/2, degrade-to-cache as WARNING/exit 0, NO `--yes`, and the `axi benches` dead-end hint re-pointed at `cwcli axi inspect`). No new Real-Docker E2E test file - `inspect` remains in item 5's gap; validated manually instead on a real throwaway instance (`docs/e2e/inspect-core-migration-m9.md`). See `openspec/changes/migrate-inspect-core`.
6h. **`open` onto the logic core** (`core/open.py` 100%, the reseated `commands/open.py` renderer ~80%) - `core.open_plan(...) -> Result[LaunchTarget]` resolves the container, run-state, bench (with the no-cache fallback populate), `--app`, and editor in one plain function (`run_plan` minus `run_stream`); `commands/open.py` becomes a renderer whose final lines perform the handover (`docker` -> `exec_into_container`/`execvp`; editors -> `vscode_utils.open_in_vscode`, untouched). `test_core_open` covers every branch (the declarative four-string `LaunchTarget`, all three `NEEDS_CHOICE` kinds including the new `select_editor`, the fallback-populate abort/degrade matrix, the verbatim `--app` pass, editor detection via stdlib `shutil.which`, core silence, plain-data DTOs, and the deliberately absent `axi open` verb asserted against the registry); `test_open_characterization` is the green-before net (written and committed against unmigrated code, passing unchanged after the migration); `test_open_inspect_fallback` and `test_inspect_partial_refresh`'s open classes are re-pointed at the core, assertions untouched. The dead `vscode_utils` selection helpers (`select_vscode_editor`, the three `is_*_installed` one-liners) were grep-proven caller-free and deleted. No new Real-Docker E2E test file - validated manually instead on a real throwaway instance (15 legs, both modes, including the fallback hard-abort contract; see `openspec/changes/migrate-open-core/tasks.md`). See `openspec/changes/migrate-open-core`.
6i. **`restore` onto the logic core, plus its own Real-Docker E2E gap closing** (`core/restore.py`) - the plan/apply split settling the destructive-preview boundary for cwcli's most destructive path: `restore_plan`/`receive_plan -> Result[RestorePlan]` is the pure, secret-free resolve, `restore_apply(plan, *, consent=False, ...) -> Result[RestoreReport]` is the single guarded destructive act (`confirm_restore` `NEEDS_CHOICE` when `consent` is false). Covered by `test_core_restore` (both choice surfaces, the tri-mode selection, secrets in `environment=` on both paths, the migrate-failure `WARNING`/`migrate_ok=False`, the encryption-key merge, the streamed copies, core silence, the deliberately absent `axi restore` verb asserted against the registry), `test_restore_characterization` (the green-before net, re-pointed at the migrated seams), and the re-pointed `test_restore_safety`/`test_restore_inspect_fixes`. `tests/e2e/test_restore_e2e.py` closes `restore`'s item-5 Real-Docker E2E gap: a full-lifecycle proof (real init -> seed -> backup -> mutate -> restore) on one throwaway instance, both interactive (pty menu + destructive confirm + credential prompts) and non-interactive (`--latest --yes --mariadb-root-password`) modes, plus `--no-migrate` and the non-TTY refusals. See `openspec/changes/migrate-restore-core`.
6j. **`rm` onto the logic core** (`core/rm.py` ~92%, the reseated `commands/rm.py` renderer ~89%) - `core.remove(...) -> Result[RemovalOutcome]` is ONE plain function carrying the fail-closed C1 backup gate (a copy-out-and-VERIFY, deliberately distinct from `core.backup`), the H5 path-traversal guards, and the M11 honest-exit accounting; plan/apply was declined on rm's real behavior (Decision 1), so there is no plan/apply split. `commands/rm.py` is a renderer; the stopped-project transient-start orchestration stays frontend because it prompts. `test_rm_characterization` is the green-before net (committed against unmigrated code, passing unchanged after the move); `test_core_rm` pins the envelope at the core (the typed `USAGE`/`DOCKER` raises, `asdict`-is-plain-data, core silence, and the deliberately absent `axi rm` verb asserted against the registry); `test_rm_safety`/`test_rm_truth`/`test_rm_stopped` re-pointed at the core with their subject, assertions unchanged except the two BY-DESIGN raises; `test_rm_stopped_backup` (the transient-start orchestration) stays on the frontend. No new Real-Docker E2E test file - `rm` remains in item 5's gap; validated manually instead on two throwaway instances (both modes, including the stopped-project transient-start leg). See `openspec/changes/migrate-rm-core`.

### Partial
7. **Port Conflict Detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract, and the interactive port-conflict confirmation prompt is driven end to end by `tests/e2e/test_start_status_e2e.py`; the remaining port-scanning helpers still have no dedicated unit suite (~57%).
7a. **`restart` CLI frontend** (`commands/restart.py`, ~40%) - the single-process path's core call is dedicated-tested via `test_core_restart`/`test_axi_restart`, and both the whole-stack and `--process` paths are driven end to end by `tests/e2e/test_start_status_e2e.py`/`tests/e2e/test_per_process_supervisor_e2e.py`, but the CLI frontend itself (the argv-forgiveness reparsing, the multi-project loop, `_resolve_process_choice`'s TTY/non-TTY forks) has no dedicated unit suite.
7b. **`unlock`/`stop` CLI frontends** (`commands/unlock.py` ~50%, `commands/stop.py` ~68%) - both verbs' core calls are dedicated-tested via `test_core_unlock`/`test_core_stop`/`test_axi_unlock_stop`, and `unlock` is driven end to end in both modes by `tests/e2e/test_unlock_e2e.py`, but the CLI frontends themselves (`unlock`'s choice-resolution/verbose-print branches, `stop`'s multi-project loop and `stop_project_best_effort` adapter) have no dedicated unit suite.

### Still needed
8. **Docker Utilities** (`utils/docker_utils.py`) - foundation for all commands; the `get_frappe_container` CLI wrapper is covered by `test_core_resolvers` and the exec-stream decode helpers by `test_exec_stream_decode`, but the rest of the module's error handling is untested by a dedicated suite (~58%, up from ~51%).
9. **Port Utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
10. **VS Code Integration** (`utils/vscode_utils.py`) - container attachment fallback logic (~15%); editor detection itself moved to `core/open.py` (100%, see item 6h).
11. **Configuration Management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation, which `test_config_validation` already covers; `cwcli_home()` is now covered by `test_cwcli_home`, but `load_config`/`save_config` and the custom-path/auto-inspect-config setters remain untested (~41%).
12. Other command modules at or near 0% dedicated unit coverage: `backup.py` (its logic moved to `core/backup.py`, which is covered). `status.py` and `unlock.py` are no longer in this bucket (`status.py` - see item 6a and `test_status_frontend`; `unlock.py` moved its logic to `core/unlock.py` - see item 6d - and is now ~50% via `test_unlock`/`test_unlock_command_cli`). `run.py` is also no longer in this bucket: `test_exec_stream_decode` dedicated-tests its exec-stream decode loop, though the rest of the command is still untested.

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
