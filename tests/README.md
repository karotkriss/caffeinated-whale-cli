# Tests

This directory contains all tests for the caffeinated-whale-cli project.

## Two tiers: fast `unit` vs real-Docker `e2e`

The suite is split into two tiers by pytest marker.

- **`unit`** - fast, needs no Docker daemon.
  It is the default tier: a bare `pytest` runs only this tier.
  It is the mock-free/mock-based suite that verifies pure logic and command wiring against fakes, and it runs inside the uv container in CI (`test.yml`, `-m unit`).
  During the migration off the legacy mock suite (see [`../openspec/changes/rebuild-e2e-test-suite`](../openspec/changes/rebuild-e2e-test-suite)) this tier also carries the container-mock behavior tests; they are retired per command as each command's real E2E lands, and the mock-free pure-logic tests are kept permanently.
- **`e2e`** / **`e2e_p2p`** - real Docker.
  These live under [`tests/e2e/`](e2e/) and drive the real `cwcli` console script against genuine throwaway Frappe instances (real `cwcli init` up, real side-effect assertions, `cwcli rm` down).
  They require a reachable Docker daemon and are excluded by default; run them explicitly with `-m e2e`.
  See [E2E harness](#e2e-harness-real-docker) below.

The `unit`, `e2e`, and `e2e_p2p` markers are registered in `pyproject.toml`; `tests/conftest.py` auto-applies `unit` to any test not marked `e2e`/`e2e_p2p`, so there is nothing to hand-mark.

## Quick Reference

```bash
# Fast tier only (the default; no Docker needed)
uv run pytest

# Fast tier, explicit + coverage (what CI's unit job runs)
uv run pytest -m unit --cov=caffeinated_whale_cli

# Prove the fast tier is mock-free: still green with a dead Docker endpoint
DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest -m unit

# Real-Docker E2E tier (needs a Docker daemon), one Frappe version leg
CWE2E_FRAPPE_MAJOR=16 uv run pytest tests/e2e -m e2e

# Run specific test file
uv run pytest tests/test_completion_utils.py

# Run tests matching a pattern
uv run pytest -k "cache"

# Stop on first failure / show locals / debugger
uv run pytest -x
uv run pytest -l
uv run pytest --pdb
```

## Reading the output (timing & descriptions)

`tests/conftest.py` layers pure-observability reporting on top of every run (it changes nothing a test asserts), so the output explains itself instead of being a wall of opaque `file::test_name` lines. This surfaces in CI logs too, where the E2E matrix runs:

- **Each `-v` line carries its own time and a plain-English description**, e.g. `... test_tty_accept_proceeds PASSED  0.000s  · \`confirm_or_exit\` proceeds when the TTY prompt is accepted.`. The description is the test's docstring first line when it has one, else its function name humanised - so a newcomer can read what a test does without decoding the filename. Docstring-first is the convention; the humanised name is the floor. Improve a description by giving the test a one-line docstring.
- **A single "two-faced" end-of-run summary** (built in [`_reporting.py`](_reporting.py)): one report, two faces from one code path. When stdout is a colour-capable terminal it renders **Cockpit** - a header verdict line, the highlighted init pole, a colour-graded slowest-tests table, a per-file rollup with bars, and a phase-totals panel; otherwise it renders **Ledger** - the identical sections as plain aligned columns under `==== headers ====`, no boxes or colour, so raw CI logs and piped output stay clean and greppable. Colour is forced only on the reporter's own `rich.Console`, never via `FORCE_COLOR` (which would leak ANSI into the app-under-test's stdout and break string-match assertions).
- **The E2E `cwcli init` / bench-build pole is highlighted separately** in that summary (the `One-time E2E cost` panel / section). That one session-scoped step is what makes the E2E matrix ~10-30+ min, so it is split out and flagged as **not** per-test time; the fixture that builds it times itself in [`e2e/conftest.py`](e2e/conftest.py) and stashes the duration on `config`. It is session-scoped, which a generic per-test timer cannot observe (the session node sits above `tests/`), hence the explicit timing there - and the panel appears only when that duration was recorded, so the fast tier (no bench build) omits it.
- `--durations=15` (in `pyproject.toml` addopts) adds pytest's built-in slowest-N view for a quick scan.

## E2E harness (real Docker)

The E2E harness ([`tests/e2e/harness.py`](e2e/harness.py) + [`tests/e2e/conftest.py`](e2e/conftest.py)) automates the manual `docs/e2e/` recipe so the destructive-path guarantees are enforced by machine.
It drives the real `cwcli` binary (subprocess for non-interactive, `pexpect` for interactive - awaiting the prompt_toolkit `ESC[?2004h` raw-mode marker before each keystroke), waits on real readiness (never fixed sleeps), and asserts real side effects (e.g. a non-empty DB dump copied out to the host), in both modes.

Isolation and safety are non-negotiable and layered:

- Each session gets a temporary `HOME` **and** a `CWCLI_HOME` override (the precise seam that relocates only cwcli's own footprint), plus unique `cwe2e-<runid>-<n>` project/site names and a port allocator (bases ≥1006 apart).
- A **hard rail** (`enforce_isolation`) fails closed before any Docker work if `HOME` is (or nests under) the operator's real home, or if `CWCLI_HOME` is unset or does not resolve to a location inside that isolated `HOME` (so a `CWCLI_HOME` pointing at the real home can never slip through), and a name rail refuses any project name lacking the `cwe2e-` prefix.
- An **unconditional teardown backstop** (`sweep_cwe2e`) removes every `cwe2e-`-labelled compose project's containers, volumes, and networks on session teardown, so a crashed test never leaks.

`CWE2E_FRAPPE_MAJOR` selects the Frappe version leg (default 16); version-agnostic E2E tests run only on the v16 leg, version-sensitive ones on every leg.
The real-Docker E2E is Linux-only; Windows/macOS-specific code stays in the unit tier.

## Test Files

As of 0.37.0 (unreleased), `tests/` holds 52 `test_*.py` suites totaling 814 tests in the
`unit` tier (measured with `uv run pytest --cov`). Run `ls tests/` for the
authoritative current list; see [../docs/testing/README.md](../docs/testing/README.md)
for the per-area breakdown.

`tests/e2e/` adds more `test_*.py` suites in the real-Docker `e2e` tier
(`test_init_e2e.py`, `test_backup_e2e.py`, `test_start_status_e2e.py`,
`test_start_status_new_behavior_e2e.py`, `test_per_process_supervisor_e2e.py`,
`test_status_unsupervised_e2e.py`, `test_unlock_e2e.py`, `test_label_e2e.py`,
`test_harness_safety.py`); run `ls tests/e2e/`
for the current list. They are not part of the 814/52 count above since they need a
Docker daemon and are excluded from a bare `pytest`. `test_start_status_e2e.py` is the
lifecycle-command net (`start`/`status`/`logs`/`restart`, both modes) that pins the
outcome-level invariants the start/status core migration must preserve; it is
structure-agnostic (it asserts observable states, not `/tmp` log paths, `pkill`
patterns, or the exact status tokens) so it survives that migration unchanged.
`test_start_status_new_behavior_e2e.py` is PR 2's own net for the NEW behavior the
migration adds (per-process health, `degraded`, the idempotent single-supervisor
no-op, the relocated bounded log, the multi-bench prompt/error), on top of PR 1's net.
`test_per_process_supervisor_e2e.py` is `add-per-process-supervisor`'s own net for the
features honcho's all-or-nothing model made impossible: supervisord as the live
supervisor, `cwcli restart --process`/`cwcli axi restart --process` cycling one program
while its siblings keep their pids, auto-heal after a killed process, `cwcli logs
--process`, and the unknown-process usage error.

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

Current overall coverage at 0.37.0, unreleased (814 tests across 52 test files, ~62% overall).

### Covered Modules
- ✅ `utils/completion_utils.py` - 92% (7 missing lines)
- ✅ `utils/bench_labels.py` - ~94% (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`)
- ✅ `utils/db_utils.py` - ~68% (`test_db_security`, `test_config_validation`)
- ✅ `commands/inspect.py` - ~62% (`test_inspect_partial_refresh`, `test_inspect_label_recovery`)
- ✅ `commands/restore.py` - ~43% (`test_restore_safety`, `test_restore_inspect_fixes`)
- ✅ `commands/rm.py` - ~84% (`test_rm_safety`, `test_rm_truth`, `test_rm_stopped`, `test_rm_stopped_backup`)
- ✅ `commands/init.py` - ~49% (`test_init_reuse_bench`, `test_init_mariadb_flag`, `test_init_admin_password`)
- ✅ `commands/apps.py` + `commands/update.py` app-update path - (`test_apps`: both modes, multi-site fan-out, frappe reset, `update` deprecation)
- ✅ `utils/config_utils.py`'s `cwcli_home()` - (`test_cwcli_home`: mock-free, sets a real `CWCLI_HOME` env var and checks real filesystem/subprocess results)
- ✅ `core/envelope.py`, `core/errors.py`, `core/docker.py` - 100%, `core/resolvers.py` - ~97% (`test_core_envelope`: DTO/error contract + the AST-scan purity ban; `test_core_resolvers`: split resolvers plus the `commands/utils.py`/`docker_utils.py` CLI-wrapper exit-code preservation; the shared bench-op helpers `resolve_default_site`/`validate_site_name`/`validate_bench_path`/`require_bench_dir`/`require_site_dir`, extracted on `unlock`'s migration, are exercised via `test_core_backup` and `test_core_unlock`)
- ✅ `core/backup.py` - ~95% (`test_core_backup`: every `core.backup` branch - success, both `NEEDS_CHOICE` forks, each `CwcliError` kind - on a fake container)
- ✅ `core/unlock.py` - ~98% (`test_core_unlock`: every branch - removal with a parsed `removed` list, already-unlocked, default-site resolution, `select_bench`/`confirm_start` choices, each `CwcliError` kind - built from the same primitives as `core/backup.py` with zero new ones)
- ✅ `core/label.py` - (`test_core_label`: every branch - list mode without a container, set/clear/rename, BOTH clear-failure modes plus the marker-before-cache ORDER asserted directly, duplicate/numeric rejection before any write, the `offer_choice=False` `NOT_RUNNING` refusal that never offers to start, the caller-supplied hint, `select_bench`/`bench.sole`, and the uninspected-project `NOT_FOUND`)
- ✅ `core/stop.py` - 100% (`test_core_stop`: stopped count, already-stopped, `NOT_FOUND`, docker-unreachable, names-not-objects, and that it prints nothing at all)
- ✅ `commands/axi.py` - ~97% (`test_axi`: verb exit-mapping (0/1/2), the content-first home, `axi ls`/`axi where`, the TOON encoder)
- ✅ `core/list.py`, `core/where.py` - 100% (`test_core_list`: fake docker client, empty/aggregate/DOCKER-raise; `test_core_where`: throwaway sqlite, dedup/scoping/installed-only/USAGE)
- ✅ `commands/list.py` - ~80% (`test_list`: the `ls --json` empty-`[]` fix, quiet/table rendering, port-range condensing)
- ✅ `commands/where.py` - 100% (`test_where`: table/JSON rendering, the `--apps`/`--sites` conflict, the definitive `[]` empty state)
- ✅ `core/supervision.py` - ~82% (`test_core_supervision`: supervisord discovery + label-mapping + bench-keying, the expected-set Procfile parse, config/launcher generation, the `pip install supervisor` fail-closed bootstrap, the supervisor marker present/absent, the web probe, per-process log-path resolution, `discover_unsupervised_stack`'s honcho/`bench start` fallback (cwd-keyed, live processes reported up, no-manager case), the real `frappe <cmd>` bench-helper cmdline forms mapping to labels)
- ✅ `core/start.py` - ~98% (`test_core_start`: the launch outcome, the idempotent no-op, multi-bench `NEEDS_CHOICE`, an explicit `bench_path` used verbatim, the `--autorestart`/`--no-autorestart` config-state toggle, missing-project/docker-unreachable errors)
- ✅ `core/status.py` - ~93% (`test_core_status`: every `overall` branch including the stable-partial-stack `degraded` (a program `FATAL` while web serves), the offline-not-raised contract, supervisor-down vs never-started, the docker-unreachable raise, the not-cwcli-supervised honcho fallback (real up/pid/uptime, the `degraded` case, the no-manager and still-supervised non-flagged cases))
- ✅ `core/restart.py` - ~95% (`test_core_restart`: `core.restart_process` restarting one program leaving siblings running, multi-bench `select_bench` and unknown/ambiguous `select_process` `NEEDS_CHOICE`, `NOT_RUNNING` for a down supervisor/container)
- ✅ `commands/status.py` - ~93% (`test_status_frontend`: the stdout-token-only contract the PR-1 E2E net pins, per-process detail (incl. supervisord `state`) on stderr, exit 0 across lifecycle states, the not-cwcli-supervised heading/hint; `test_status_watch`: the `--watch` live view - every tick re-polls with `probe_web=False` so it makes ZERO web requests, the non-TTY single-quiet-snapshot degrade, the `--interval` 1s floor, a clean `KeyboardInterrupt` exit)
- ✅ `commands/axi.py` `start`/`status` verbs - (`test_axi_start_status`: TOON rendering, exit-code mapping, needs-choice/`CONFLICT` flag-naming, the never-prompt port-conflict pre-step)
- ✅ `commands/axi.py` `restart` verb - (`test_axi_restart`: TOON `ProcessRestartOutcome` rendering, exit-code mapping, `--process` required, unknown/ambiguous process and multi-bench `select_bench` as usage errors listing valid labels)
- ✅ `commands/axi.py` `unlock`/`stop` verbs - (`test_axi_unlock_stop`: TOON `UnlockOutcome`/`StopOutcome` rendering incl. `removed` as a structured list, exit-code mapping, `axi unlock`'s multi-bench `select_bench` and stopped-container `confirm_start` as usage errors (no `--yes` on this verb - it mirrors `axi backup`), `axi stop`'s idempotent already-stopped success and structured not-found error)
- ✅ `commands/axi.py` `benches`/`label`/`self-update` verbs - (`test_axi_label`: TOON `BenchList`/`LabelOutcome` rendering, exit-code mapping, `axi benches`' uninspected-project error naming `cwcli inspect` rather than an empty list, `axi label`'s required-and-exclusive `--set`/`--clear` (neither is a usage error pointing at `axi benches`, NOT an implicit list), the multi-bench `select_bench` usage error, the `NOT_RUNNING` hint that does not name a flag `label` lacks, and `axi self-update --check`'s deliberate exit-0-when-outdated divergence from the human `--check`'s exit 1)
- ✅ `core/version.py` - ~90% (`test_core_version`: install-method detection tree (dev/uv/uvx/pip fallback), the fail-open PyPI lookup, PEP 440 compare including the dev-ahead case, the TTL cache, and the `passive_notice` cache-only gate incl. the `attempted_at` once/day refresh throttle)
- ✅ `commands/self_update.py` - 100% (`test_self_update`: dev/uvx no-op, the default upgrade run (success/failure/`FileNotFoundError`), `--check`, `--no-cache`)
- ✅ `update_notice.py` - ~95% (`test_update_notice`: stderr-only rendering never on stdout, `sys.stderr.isatty()` gating, `CWCLI_NO_UPDATE_CHECK` suppression, fail-open when the core gate raises)

### Modules Needing Dedicated Suites
- ⚠️ `utils/port_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/docker_utils.py` (~51%, up from ~37% now that `test_core_resolvers` dedicated-tests the `get_frappe_container` CLI wrapper; the rest of the module is still only incidentally covered)
- ⚠️ `utils/sendme_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/vscode_utils.py` (~16%, only incidental coverage)
- ⚠️ `utils/config_utils.py` (~41%; `cwcli_home()` is covered by `test_cwcli_home`, but `load_config`/`save_config`/the custom-path and auto-inspect-config setters remain untested)
- ⚠️ `commands/start.py` (~57%; `test_yes_flag` covers the non-interactive/`--yes` contract and `tests/e2e/test_start_status_e2e.py` drives it end to end, but the port-scanning/conflict-resolution branches still have no dedicated unit suite)
- ⚠️ `commands/restart.py` (~40%; the single-process path's core call is dedicated-tested via `test_core_restart`/`test_axi_restart`, and both the whole-stack and `--process` paths are driven end to end by `tests/e2e/test_start_status_e2e.py`/`tests/e2e/test_per_process_supervisor_e2e.py`, but the CLI frontend itself - the argv-forgiveness reparsing, the multi-project loop, `_resolve_process_choice`'s TTY/non-TTY forks - has no dedicated unit suite)
- ✅ `commands/label.py` - (`test_bench_label_db_and_command`: the 15 pre-migration tests, re-pointed at the core with their assertions untouched - including the two that pin the clear-path consistency invariant; `tests/e2e/test_label_e2e.py` drives the two-store invariant against a real container, non-interactive only because `label` has no prompt)
- ⚠️ `commands/unlock.py` (~50%; its logic moved to `core/unlock.py`, which is covered - see above; `test_unlock_command_cli.py` dedicated-tests the `--bench` selector resolution, and `tests/e2e/test_unlock_e2e.py` drives both modes end to end, but the CLI frontend's choice-resolution/verbose-print branches still have no dedicated unit suite)
- ⚠️ `commands/stop.py` (~68%; its logic moved to `core/stop.py`, which is covered - see above; `test_axi_unlock_stop.py`/`test_yes_flag.py`/`test_exit_codes.py` exercise it incidentally, but the CLI frontend itself - the multi-project loop, `stop_project_best_effort` - has no dedicated unit suite)
- ⚠️ Command modules at or near 0% dedicated coverage: `backup.py` (0%; its logic moved to `core/backup.py`, which is covered - see above), `run.py`

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
# The default `-m` deselects the real-Docker tiers, so a bare `pytest` runs only
# the fast unit tier; override with `-m e2e` on the CLI (the last `-m` wins).
addopts = ["-v", "--strict-markers", "--tb=short", "--cov-report=term-missing", "--durations=15", "-m", "not e2e and not e2e_p2p"]
markers = [
    "unit: fast tests that need no Docker daemon (the default tier)",
    "e2e: real-Docker end-to-end tests driving the real cwcli binary",
    "e2e_p2p: real-Docker P2P (sendme loopback) end-to-end tests",
]
```

Coverage configuration:

```toml
[tool.coverage.run]
source = ["src/caffeinated_whale_cli"]
omit = ["*/tests/*", "*/__init__.py"]
```

## CI/CD

CI is two-tiered.

- **`.github/workflows/test.yml`** runs the fast `unit` tier on every push and PR, inside the uv container:

  ```yaml
  - name: Run unit tests with coverage
    run: uv run pytest -m unit --cov=caffeinated_whale_cli --cov-report=term-missing
  ```

  The `Pytest` (unit) job is the always-required gate.

- **`.github/workflows/e2e.yml`** runs the real-Docker `e2e` tier as a `strategy.matrix.frappe: [14, 15, 16]` of GitHub-hosted `ubuntu-latest` jobs (no `container:`, so `docker`/`docker compose` reach the daemon).
  It is triggered on PRs into `develop`/`master` and on-demand via the `e2e` PR label, with per-job `timeout-minutes` and an `always()` `cwe2e-` teardown backstop.
  Note: `develop`/`master` have no branch protection today, so a repo admin must enable it and tick these checks before the E2E matrix is a *required* gate; until then the unit tier is the only gate that blocks a merge.

See the [CI/CD Workflows guide](../docs/contributing/ci-cd.md) for the full setup.

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
3. **Real-Docker E2E for `init` and `backup`** - covered by `tests/e2e/test_init_e2e.py`, `tests/e2e/test_backup_e2e.py` (both interactive and non-interactive, on the v14/v15/v16 matrix).
3a. **Real-Docker E2E for the lifecycle commands** (`start`, `status`, `logs`, `restart`) - covered by `tests/e2e/test_start_status_e2e.py` (both modes; structure-agnostic outcome invariants that the start/status core migration preserved unchanged - see `openspec/changes/add-start-status-e2e-net`) plus `tests/e2e/test_start_status_new_behavior_e2e.py` (the migration's own net for the NEW behavior: idempotency, `degraded`, real per-process health, the relocated log, multi-bench refuse - see `openspec/changes/migrate-start-status-core`).
4. **Logic core + `cwcli axi`** (`core/`, `commands/axi.py`) - covered by `test_core_envelope`, `test_core_resolvers`, `test_core_backup`, `test_axi` (envelope/resolvers/docker wrapper at 100%, `core/backup.py` ~95%, `commands/axi.py` ~97%). `start`/`status` followed the same pattern onto `core/start.py`/`core/status.py`/`core/supervision.py` (~98%/~93%/~82%), covered by `test_core_start`, `test_core_status`, `test_core_supervision`, plus the frontends `test_status_frontend` and `test_status_watch` (the `--watch` live view's zero-web-probe/non-TTY-degrade/interval-floor/clean-`KeyboardInterrupt` contract) and `axi start`/`axi status` in `test_axi_start_status`. `add-per-process-supervisor` re-pointed `core/supervision.py` from honcho to supervisord and added `core/restart.py` (~95%, `core.restart_process` restarting one program leaving siblings running), covered by `test_core_restart` and the `cwcli axi restart` verb in `test_axi_restart`.
4a. **`self-update`** (`commands/self_update.py`, `core/version.py`) - covered by `test_core_version` (install-method tree, fail-open PyPI lookup, PEP 440 compare, TTL cache - ~90%) and `test_self_update` (dev/uvx no-op, the default upgrade run, `--check`, `--no-cache` - 100%). No `cwcli axi` verb (deferred).
4b. **Passive update notice** (`update_notice.py` ~95%, wired once into `main.py`'s root Typer callback) - covered by `test_update_notice` (stderr-only, TTY-gated, `CWCLI_NO_UPDATE_CHECK`-suppressible, fail-open) and `test_core_version`'s `TestPassiveNotice` class (the cache-only hot path, the detached background refresh, the `attempted_at` once/day throttle on persistent failure).
4c. **`unlock`+`stop` onto the logic core** (`core/unlock.py` ~98%, `core/stop.py` 100%, the shared bench-op helpers in `core/resolvers.py` ~97%) - the foundation's generality proof: `unlock` was migrated with zero new primitives. Covered by `test_core_unlock`, `test_core_stop`, `test_unlock` (re-pointed argv-injection guards), `test_unlock_command_cli` (the `--bench` selector resolution), and the `cwcli axi unlock`/`cwcli axi stop` verbs in `test_axi_unlock_stop`. `tests/e2e/test_unlock_e2e.py` closes `unlock`'s standing both-modes E2E gap (pty-driven interactive leg, non-interactive `--yes` leg, real locks-directory removal); `stop` needed no new E2E, already pinned by `test_start_status_e2e.py`/`test_status_unsupervised_e2e.py`. See `openspec/changes/migrate-unlock-stop-core`.

### Partial
5. **Port conflict detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract; the interactive port-conflict confirmation prompt is driven end to end in `tests/e2e/test_start_status_e2e.py`, and the remaining port-scanning helpers still have no dedicated unit suite (~57%).

### Still needed
6. **Docker utilities** (`utils/docker_utils.py`) - foundation for all commands; the `get_frappe_container` CLI wrapper is now covered by `test_core_resolvers`, but the rest of the module is still only incidentally covered (~51%).
7. **Port utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
8. **VS Code integration** (`utils/vscode_utils.py`) - container attachment fallback (~16%).
9. **Configuration management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation (~37%).
10. **Real-Docker E2E for the remaining commands** (`rm`, `restore`, `update`/`apps`, `inspect`) and the P2P (`sendme`) loopback - tracked in `openspec/changes/rebuild-e2e-test-suite`. `unlock` is no longer in this list: `tests/e2e/test_unlock_e2e.py` covers it (see item 4c).
11. Other command modules at or near 0% dedicated unit coverage: `backup.py` (its logic moved to `core/backup.py`, which is covered), `run.py`. `status.py` and `unlock.py` are no longer in this bucket: `test_status_frontend`/`test_status_watch` dedicated-test `status.py` (~95%, on top of `tests/e2e/test_start_status_e2e.py`, see item 3a), and `unlock.py`'s logic moved to `core/unlock.py` (covered, see item 4c) with `commands/unlock.py` itself now at ~50% via `test_unlock_command_cli`.

## Resources

- [Testing Guide](../docs/testing/guide.md) - Comprehensive testing documentation
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Plugin](https://pytest-cov.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
