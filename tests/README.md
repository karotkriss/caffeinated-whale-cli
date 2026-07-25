# Tests

This directory contains all tests for the caffeinated-whale-cli project.

## Two tiers: fast `unit` vs real-Docker `e2e`

The suite is split into two tiers by pytest marker.

- **`unit`** - fast, needs no Docker daemon.
  It is the default tier: a bare `pytest` runs only this tier.
  It is the mock-free/mock-based suite that verifies pure logic and command wiring against fakes, and it runs inside the uv container in CI (`test.yml`, `-m unit`).
  During the migration off the legacy mock suite (see [`../openspec/changes/rebuild-e2e-test-suite`](../openspec/changes/rebuild-e2e-test-suite)) this tier also carries the container-mock behavior tests; they are retired per command as each command's real E2E lands, and the mock-free pure-logic tests are kept permanently.
- **`e2e`** / **`e2e_p2p`** / **`e2e_pkg`** - real Docker.
  These live under [`tests/e2e/`](e2e/) and drive the real `cwcli` console script against genuine throwaway Frappe instances (real `cwcli init` up, real side-effect assertions, `cwcli rm` down).
  They require a reachable Docker daemon and are excluded by default; run them explicitly with `-m e2e`.
  See [E2E harness](#e2e-harness-real-docker) below.

The `unit`, `e2e`, `e2e_p2p`, `e2e_pkg`, and `standalone` markers are registered in `pyproject.toml`; `tests/conftest.py` auto-applies `unit` to any test not marked `e2e`/`e2e_p2p`/`e2e_pkg`, so there is nothing to hand-mark. `e2e_pkg` is the runtime-deps-only packaging leg (one full lifecycle driven against a `uv tool install .` binary via `CWCLI_BIN`); it is excluded from `-m e2e` so it does not double the version matrix's init cost. `standalone` is the one marker that *is* hand-applied: it names each `e2e` test that never touches the shared session instance, and CI splits every version leg into a `standalone` and a `shared` job on it (see [CI/CD](#cicd) below).

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

## Reading the output

Nearly every run is green, so a green run says as little as it can. [`conftest.py`](conftest.py) prints **one line per test FILE** - path, verdict, wall seconds - and then **one total coverage %**. That is the whole of a passing run:

```
tests/test_apps.py                                        PASS    3.71s
tests/test_auto_inspect.py                                PASS    2.09s
...
coverage: 66.37%
994 passed, 69 deselected in 13.56s
```

A **red** run additionally gets pytest's own stock `ERRORS` / `FAILURES` sections and its `short test summary info` - the failing test's name and its traceback (`--tb=short`) - because a red run that does not say what broke just costs a second run.

Notes for anyone changing this:

- The trim is subtractive. `pytest_report_teststatus` blanks only the per-test progress LETTER; the category and the word are load-bearing (the category feeds the run totals, the word is the `FAILED` / `ERROR` prefix in the summary), and getting either wrong corrupts the report silently.
- Colour goes through pytest's own terminal writer, so it appears only on a real TTY. **Never** set `FORCE_COLOR` to get colour into CI logs: it leaks ANSI into the app-under-test's own stdout and breaks string-match assertions (e.g. `test_where.py::...::test_no_match_plain_message`).
- Only the fast tier is trimmed. Anything at verbosity >= 0 (`-v`) is handed back to pytest's stock reporting untouched, which is how the minutes-long E2E tier keeps its live per-test `... PASSED` lines - `e2e.yml` runs it with `-o addopts="" -v`, and a long silence there would read the same as a hang.

## E2E harness (real Docker)

The E2E harness ([`tests/e2e/harness.py`](e2e/harness.py) + [`tests/e2e/conftest.py`](e2e/conftest.py)) automates the manual `docs/e2e/` recipe so the destructive-path guarantees are enforced by machine.
It drives the real `cwcli` binary (subprocess for non-interactive, `pexpect` for interactive - awaiting the prompt_toolkit `ESC[?2004h` raw-mode marker before each keystroke), waits on real readiness (never fixed sleeps), and asserts real side effects (e.g. a non-empty DB dump copied out to the host), in both modes.

Isolation and safety are non-negotiable and layered:

- Each session gets a temporary `HOME` **and** a `CWCLI_HOME` override (the precise seam that relocates only cwcli's own footprint), plus unique `cwe2e-<runid>-<n>` project/site names and a port allocator (bases ≥1006 apart).
- A **hard rail** (`enforce_isolation`) fails closed before any Docker work if `HOME` is (or nests under) the operator's real home, or if `CWCLI_HOME` is unset or does not resolve to a location inside that isolated `HOME` (so a `CWCLI_HOME` pointing at the real home can never slip through), and a name rail refuses any project name lacking the `cwe2e-` prefix.
- An **unconditional teardown backstop** (`sweep_cwe2e`) removes every `cwe2e-`-labelled compose project's containers, volumes, and networks on session teardown, so a crashed test never leaks.
- A **root-owned-path reclaim** (`reclaim_root_owned`) runs before a session's temp `HOME` is deleted: a scoped root-uid container chowns any root-owned path (e.g. a compose-created `working_dir`) back to the host uid/gid first, so a path the Docker daemon created as root can never survive teardown into the shared temp home.

`CWE2E_FRAPPE_MAJOR` selects the Frappe version leg (default 16); version-agnostic E2E tests run only on the v16 leg, version-sensitive ones on every leg.
The real-Docker E2E is Linux-only; Windows/macOS-specific code stays in the unit tier.

## Test Files

`tests/` holds one `test_*.py` suite per area in the `unit` tier.
Counts below are a moving target because a suite or a test is added or removed on nearly every PR, so treat any number in this file as illustrative, not authoritative.
For the current suite-file count, run `ls tests/test_*.py | wc -l`.
For the current test count and pass/fail totals, run `uv run pytest -m unit` and read its own summary line (see [Reading the output](#reading-the-output) above).
See [../docs/testing/README.md](../docs/testing/README.md) for the per-area breakdown.

`tests/e2e/` adds more `test_*.py` suites in the real-Docker `e2e` tier
(`test_init_e2e.py`, `test_backup_e2e.py`, `test_start_status_e2e.py`,
`test_start_status_new_behavior_e2e.py`, `test_per_process_supervisor_e2e.py`,
`test_status_unsupervised_e2e.py`, `test_logs_orphan_tail_e2e.py`,
`test_unlock_e2e.py`, `test_label_e2e.py`, `test_axi_rm_e2e.py`,
`test_rm_site_e2e.py`, `test_multibench_correctness_e2e.py`,
`test_harness_safety.py`); run `ls tests/e2e/`
for the current list. They are not part of the count above since they need a
Docker daemon and are excluded from a bare `pytest`. `test_axi_rm_e2e.py` is the
permanent net for `cwcli axi rm` (`add-axi-rm-verb`): its own dedicated instance
pair (never the shared `session_instance`, since `rm` destroys what it touches)
proves the whole destructive arc in one real run - a seeded record read back
before removal, the removal's TOON outcome and exit code, an honest deletion
(no container, no volume, no project directory left), and the archived backup
genuinely restored into a fresh second instance with the same record read back
through Frappe. It runs once (the v16 leg only; the C1 gate does not vary by
Frappe major), mirroring `test_init_e2e.py`'s existing `v16_only` precedent.
`test_rm_site_e2e.py` is the permanent net for both `cwcli rm-site` frontends.
It proves the named site's directory and database are gone, the credential-bearing archive is copied to the host and pruned from the container, the other sites and instance remain running, and the interactive and non-interactive consent paths behave honestly.
`test_start_status_e2e.py` is the
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
`test_multibench_correctness_e2e.py` is the net for the multibench correctness
fixes, each asserting the POSITIVE before the negative (a check that also passes
against a dead instance proves nothing): the web probe naming the bench's site so a
healthy bench reads 200 rather than the multi-tenant 404, `status` staying a pure
read that never resurrects a deliberately stopped bench, `cwcli stop --bench` /
`cwcli axi stop --bench` ending one bench while the containers and siblings keep
running, `cwcli restart --bench` relaunching the bench it was named, and the
advertised address (the `init` banner and `cwcli open`) resolving to the published
host port rather than the container's.
`test_logs_orphan_tail_e2e.py` (`cwcli-logs-orphan-tail-o5`) guards the orphan
`tail -F` regression: a non-TTY `cwcli logs --follow` (agent/pipe) Ctrl+C'd out used
to leave the exec'd tail running INSIDE the container forever (no kill-exec API,
and no `-it` raw mode to forward `^C`); it now asserts no orphan `tail -F` survives
the follower's exit in either the non-TTY or the already-clean `-it` mode.
`test_restore_e2e.py` is batch 11's full-lifecycle proof for cwcli's most destructive
path (`migrate-restore-core`): a cache-free DB-table marker (seed ORIGINAL ->
`cwcli backup` -> mutate to MUTATED -> `cwcli restore` -> assert ORIGINAL is back)
proves the restore genuinely drops-and-recreates the DB, in both the non-interactive
(`--latest --yes --mariadb-root-password`) and interactive (pty menu + destructive
confirm + credential prompts) modes, plus `--no-migrate`, the non-TTY refusals, and
the flag mutual exclusions.
`test_restore_p2p_e2e.py` is the `e2e_p2p` marker's real transport proof: it moves
REAL bytes over sendme, loopback on one box - `cwcli restore --send` serves a genuine
ticket, then the non-interactive `cwcli restore --receive --ticket <t>` pulls-and-
restores it, and the same ORIGINAL/MUTATED marker proves the data arrived (it FAILS if
the sendme transport breaks). It uses its OWN dedicated instance (never the shared
session one, so a receive DB-wipe cannot corrupt another test's fixture), runs the
generic loopback once on the v16 leg, and keeps the v14-only `--receive` bare-filename
reproduction as a separate, clearly-labeled, v14-gated test.

### `test_completion_utils.py`
Tests for tab completion functionality.
Coverage is listed under `utils/completion_utils.py` in [Covered Modules](#covered-modules) below.

**What it tests:**
- Project name completion from Docker
- App name completion from cache
- Site name completion from cache
- Cache TTL behavior
- Docker client reuse
- Error handling

**Test classes:**
- `TestCompleteProjectNames`
- `TestCompleteAppNames`
- `TestCompleteSiteNames`
- `TestCacheHelpers`
- `TestGetDockerClient`

Run `uv run pytest tests/test_completion_utils.py -v` for the current per-class test counts.

## Test Coverage

Every percentage below is a snapshot from the most recent full run, not a promise.
It drifts with every test added or removed.
Regenerate the current overall total and the per-module breakdown with:

```bash
uv run pytest -m unit --cov=caffeinated_whale_cli --cov-report=term-missing
```

### Covered Modules
- ✅ `utils/completion_utils.py` - 92% (7 missing lines)
- ✅ `utils/bench_labels.py` - ~94% (`test_bench_labels`, `test_bench_selector`, `test_bench_label_db_and_command`)
- ✅ `utils/db_utils.py` - ~73% (`test_db_security`, `test_config_validation`)
- ✅ `utils/auto_inspect.py` - first-ever dedicated suite (`test_auto_inspect`: the Windows `WaitForSingleObject`-based `_pid_alive` probe, the fork-unavailable subprocess fallback and its Windows-path bootstrap source, `_spawn_detached` routing the child's stderr to the log file instead of `DEVNULL`, `_log(exc_info=True)` recording the traceback, the `int(config.get("interval"))` coercion, stale-PID-file cleanup, the pid+creation-time identity gate in `is_running`/`stop_daemon` refusing to signal a recycled pid, and `_handle_sigterm` tearing down directly without re-entering itself)
- ✅ `commands/inspect.py` (renderer) + `core/inspect.py` (the tier machine + cache write) - `test_inspect_characterization` is the green-before net (committed against unmigrated code: byte-identical `--json` per tier incl. the gathered-vs-cache-read key-order swap, the persisted cache dict shape, T2 passivity on a stopped project even under `--yes`, the T3 `--yes` auto-start and non-TTY refusal, the removed `--show-apps` flag (rejected as unknown), the tree's "(default)" order); `test_core_inspect` pins the envelope AT THE CORE (every tier branch, `confirm_start` at call time, `offer_choice=False` -> `NOT_RUNNING`, drift-degrade without persisting, the `errors="replace"` decode and `CwcliError(DOCKER)` fan-out wrap, core silence, asdict-is-plain-data, config content never in the typed report); `test_inspect_partial_refresh`/`test_inspect_label_recovery` re-pointed with their subject; `TestInteractiveLabeling` covers the `-i` loop now that it only prompts and routes through `core.label.set_labels` (accept/blank, duplicate rejection, the stopped-project cache-only degrade, Ctrl+C). See `openspec/changes/migrate-inspect-core`.
- ✅ `core/restore.py` + the reseated `commands/restore.py` renderer - the plan/apply split (batch 11, `migrate-restore-core`): `test_core_restore` pins the envelope AT THE CORE (both choice surfaces - `select_backup` and `confirm_restore`; the tri-mode selection; secret-in-`environment=` and the exec arg order on both paths; the migrate-failure `WARNING`/`migrate_ok=False`; the encryption-key merge; the streamed `put_archive`/`get_archive` copies; `receive_plan`'s copy-in + no-database refusal; the hard-failure `PRECONDITION`; invalid-username `USAGE`; core silence; asdict-is-plain-data; origin mismatch; and the deliberately ABSENT `axi restore` verb asserted against the registry - DEFERRED, Decision 9); `test_restore_characterization` is the green-before net (normal-path exec arg order + secret in `environment=`, the encryption-key merge, the migrate-then-restart order + migrate-failure exit, the flag mutual exclusions), re-pointed at the migrated seams; `test_restore_safety` (the receive confirm/origin/password-off-argv/missing-apps + the normal-path selectors/exit-codes) and `test_restore_inspect_fixes` (the six restore+inspect bugs) re-pointed with their subjects, the confirm-count assertion now 1 BY DESIGN (the four confirms collapsed into one shared `_gate`). See `openspec/changes/migrate-restore-core`.
- ✅ `core/rm.py` (the migrated `core.remove` + copy-out backup gate) + the reseated `commands/rm.py` renderer - `test_rm_characterization` is the green-before net (committed against unmigrated code, driving the real `rm.rm()` command through a module-agnostic `_patch_attr` seam so it passes unchanged after the move): the confirm + stopped-project disclosure, the C1 abort-before-any-container-removal, the H5 `rm ..` refusal, the M11 partial-failure exit code, the exit-0 not-found no-op, multi-bench per-bench backup, `--no-volumes`, `--no-backup`; `test_core_rm` pins the envelope AT THE CORE (typed `USAGE`/`DOCKER` raises, the `OK`/`WARNING` status mapping, plain-data DTOs, core silence); `test_rm_safety`/`test_rm_truth`/`test_rm_stopped` re-pointed at `core.rm` with their subject via thin dict adapters (assertions untouched except the two BY-DESIGN raises - invalid-name `USAGE`, docker-error `DOCKER`); `test_rm_stopped_backup` (the transient-start orchestration) stays on the frontend. See `openspec/changes/migrate-rm-core`. The `axi rm` verb (`add-axi-rm-verb`, captain-approved 2026-07-21, reversing this file's own former absence assertion) is a thin TOON renderer over the unchanged `core.remove`; `TestAxiRmVerbShipped` in `test_core_rm.py` pins the two properties this surface DECIDED rather than inherited - consent-only `--yes` with no auto-start, and no `--no-backup` bypass - and `test_axi_rm.py` covers the frontend (both refusals naming their way out, the failures-driven exit code, narration on stderr only). `tests/e2e/test_axi_rm_e2e.py` is the real-Docker net: a real seed, a real `cwcli axi rm --yes`, an honestly-confirmed deletion, and the archived backup restored into a fresh instance with the same record read back through Frappe.
- ✅ `core/rm_site.py` + the human and `axi` renderers - `test_core_rm_site` covers explicit target validation, core-owned consent, secret transport through the exec environment, unique archive discovery, verified copy-out before pruning, fail-closed warnings, and the plain-data outcome. `test_axi_rm_site` pins the agent surface's required consent. `tests/e2e/test_rm_site_e2e.py` proves both frontends against a real bench, including the site and database deletion, host archive contents, in-container pruning, and preservation of the other sites and running instance.
- ✅ `core/init.py` + the reseated `commands/init.py` renderer - (`test_init_characterization` is the green-before net (committed against unmigrated code through migration-surviving seams - `core.docker`, `config_utils`/`db_utils` module attributes, `subprocess`/`urllib`, the SDK exec surface, a genuinely occupied socket - so it passes unchanged on both sides): the 10-exec order and exact command strings, secrets-only-on-new-site `environment=`, skip-on-exists gating, the port-conflict three-line error, the non-interactive refusals, the ENOSPC drained-exec message, the cache clear, the compose customization + Docker Hub fail-open, the add-path stdout line on both outcomes; `test_core_init` pins the envelope AT THE CORE: the three choice surfaces (`confirm_start` on both stages, the NEW `confirm_reuse_bench`), the tri-state `reuse_bench` matrix, the Decision 3 secret audit (no event/DTO/warning/command-echo trace carries a value), exec order, version gating incl. the v13 setuptools pin and soft-fail warnings, the bounded readiness poll, the honest lost-stream `DOCKER` error where `exit code None` used to print, core silence, asdict-is-plain-data, and (since `add-axi-init-verb` shipped) `TestAxiInitVerbIsRegistered` replacing the former deferral assertion; `test_init_reuse_bench` (the frontend prompt loop), `test_init_admin_password` (generation/refusal/print-once + the env-transport re-pointed at the core), `test_init_frappe_version` (resolvers re-pointed; the ref crossing the frontend->core seam), and `test_init_mariadb_flag` re-pointed with their subjects; `TestAutoStartServices` (post-create dev-services auto-start reusing `core.start` with the exact `report.bench_path`, `--no-start`, and a start failure degrading to a warning rather than a failed `init`)). See `openspec/changes/migrate-init-core`.
- ✅ `commands/axi.py` `init` verb (`add-axi-init-verb`, the deferred batch-9 verb, captain-approved 2026-07-17) - a thin TOON renderer over the UNCHANGED `core.init_instance`/`core.init_bench` (`test_axi_init`: both password transports incl. flag-wins-over-env and the missing-password USAGE exit 2 with the core untouched, the malformed-`--version` and `--frappe-branch`+`--version` usage errors, each choice-surface-to-error mapping - `confirm_reuse_bench` exit 2 naming the flags, stage-1/stage-2 `confirm_start` exit 1 tailored to init, the port-conflict `--port` hint exit 1 - the one-terminal-document stdout purity with coarse phase progress on stderr and narration-is-secret-free, WARNING->0, and the registration + no-`--auto-start`/`--verbose` assertions; `TestAutoStartServices` covers the same `--start`/`--no-start` parity, reusing `core.start` after the report is emitted). See `openspec/changes/add-axi-init-verb`.
- ✅ `commands/apps.py` + `commands/update.py` app-update path - (`test_apps`: both modes, multi-site fan-out, frappe reset, `update` deprecation, the summary reported from the `finally` surviving a mid-fan-out stream loss with both stuck sites' remediation intact, and the `sites_to_migrate`-gated abort not firing on a bare `--site` refusal)
- ✅ `core/bench_ops.py`, `cwcli axi migrate` / `cwcli axi run-tests` / `cwcli axi build` - the execution verbs. `test_core_bench_ops.py` pins the decisions rather than the implementation: a migrate resolves EXACTLY ONE site and never fans out (the module's whole reason to exist, since `apps update` discovers its targets); the maintenance gate REFUSES rather than warns, so a site that cannot enter maintenance gets no `bench migrate` at all; the disable runs in a `finally` even when the migrate raises (why this is a plain function and not a generator - the cost of skipped cleanup is a site left DOWN); a site left in maintenance is reported and fails; `run_tests` takes its site and app as REQUIRED parameters, pinned at the CORE so no frontend can default them; and `set_maintenance` is proven SHARED with `core.update` rather than copied. `test_axi_bench_ops.py` covers the frontends: one TOON document with the resolved site readable back, exit from `report.ok` so a WARNING-shaped envelope still exits 1, `maintenance_left_on` surfaced in the document, the missing-`--site`/`--app` usage errors (exit 2, before any Docker call), the `confirm_start`/`select_bench` forks as exit-2 usage errors, the command's own bytes on stderr and never stdout, a help line on failure but not on success (AXI §9), and registry assertions - all three verbs are TOP-LEVEL with no `bench` group, and none has a variadic or free-form parameter through which a command string could be expressed. The same file also covers `--module` (`run_tests` narrowed to one dotted test module, `--app` still required so the report names the scope) and `axi build` (`site: null` since a build acts on no site, `--app` narrowing the command, and the failed-build stderr pointer), all exercised through the frontend only - `core/bench_ops.py`'s `build_assets` has no dedicated `test_core_bench_ops.py` case yet. See `openspec/changes/add-axi-bench-exec-verbs`.
- ✅ `core/apps.py` (~98%), the reseated `commands/apps.py` renderer (~98%) - `list_apps`/`install_apps`/`uninstall_apps` joined `core.update` on the logic core, so the module no longer carries two contracts. `test_apps_characterization.py` is the green-before net (written and committed against unmigrated code, `commands/apps.py` 91.28% -> 99.49%); `test_core_apps.py` covers what only `axi`/a future GUI can reach - the pre-resolved `confirm_start`/`select_bench`/`confirm_uninstall` forks, the no-cache default, the shared visible and verified `restart-processes` step as install/uninstall/checkout each reach it (the step's own rules now live in `test_core_supervision.py::TestResyncAfterCodeChange`, where the logic moved), and that the core prints nothing and returns plain serializable data; `test_axi_apps_list.py` covers `cwcli axi apps list` (TOON, exit 0/1/2, the null-vs-empty-list distinction on a failed site read) plus `cwcli axi apps install` (the permitted install, the already-installed refusal on both a plain name and a git-URL spelling, the fail-closed unreadable-site-state refusal, a failed fetch step's exit 1) and the registry assertion that `apps uninstall` alone is deliberately unregistered; `test_axi_apps_checkout.py` covers `cwcli axi apps checkout` (one TOON document with a row per git step, exit from `report.ok` so a refused checkout exits 1, the `confirm_start`/`select_bench` forks as exit-2 usage errors, git's own bytes narrated to stderr and never stdout, the recache epilogue including its warn-but-exit-0 failure, and that `--reset` is the only path that discards local work). The real-Docker `test_axi_apps_install_e2e.py` proves the false-success regression by requiring a site-routed Frappe ping to return HTTP 200 after both install and uninstall before checking the installed-state negative, and `test_apps_resync_e2e.py` drives install -> checkout -> update off ONE real `bench get-app` to prove what no mock can state: every code-bearing supervisord program (`web`, `schedule`, every worker) got a NEW pid, `socketio`/`watch`/`redis_*` kept theirs, and the site serves 200 after each verb - including `update`, whose resync must land after maintenance mode is lifted or the site answers 503. The dirty-tree guard is pinned at BOTH layers: `TestCheckoutRefusesADirtyTree` in `test_core_apps.py` owns the definition (staged, unstaged tracked, AND untracked all refuse; an unreadable `git status` fails closed; `--reset` skips the check) and pins the absence of `--untracked-files=no` on the command itself, so the tracked-only narrowing the captain reversed cannot creep back, and `test_axi_apps_checkout.py` pins its agent-surface rendering as a two-line TOON refusal naming the dirty path and `--reset` - the guard is cwcli's own and stronger than git's, so a test asserting only git's refusal would not catch its removal. See `openspec/changes/migrate-apps-core` and `openspec/changes/add-axi-apps-checkout-verb`.
- ✅ `utils/config_utils.py`'s `cwcli_home()` - (`test_cwcli_home`: mock-free, sets a real `CWCLI_HOME` env var and checks real filesystem/subprocess results)
- ✅ `core/envelope.py`, `core/errors.py` - 100%, `core/resolvers.py` - ~97% (`test_core_envelope`: DTO/error contract + the AST-scan purity ban; `test_core_resolvers`: split resolvers plus the `commands/utils.py`/`docker_utils.py` CLI-wrapper exit-code preservation; the shared bench-op helpers `resolve_default_site`/`validate_site_name`/`validate_bench_path`/`require_bench_dir`/`require_site_dir`, extracted on `unlock`'s migration, are exercised via `test_core_backup` and `test_core_unlock`)
- ✅ `core/docker.py` - ~76% (`test_core_docker`: the host-uid alignment `align_container_user_to_host`/`_read_frappe_id` - the no-op paths for matching ids and platforms without `os.getuid`, the uid+gid remap with/without the home chown, a failed remap or unreadable ids degrading to a soft warning rather than a raise; `get_project_containers`/`get_project_volumes`/`get_container`'s real `docker.from_env()` DockerException branches remain only incidentally covered, unchanged by this addition)
- ✅ `core/backup.py` - ~95% (`test_core_backup`: every `core.backup` branch - success, both `NEEDS_CHOICE` forks, each `CwcliError` kind - on a fake container)
- ✅ `core/unlock.py` - ~98% (`test_core_unlock`: every branch - removal with a parsed `removed` list, already-unlocked, default-site resolution, `select_bench`/`confirm_start` choices, each `CwcliError` kind - built from the same primitives as `core/backup.py` with zero new ones)
- ✅ `core/label.py` - ~97% (`test_core_label`: every branch - list mode without a container, set/clear/rename, BOTH clear-failure modes plus the marker-before-cache ORDER asserted directly, duplicate/numeric rejection before any write, the `offer_choice=False` `NOT_RUNNING` refusal that never offers to start, the caller-supplied hint, `select_bench`/`bench.sole`, and the uninspected-project `NOT_FOUND`; `TestSetLabels` covers the batched `set_labels` sibling - multi-apply, first-wins duplicate within the batch, an invalid assignment not aborting its siblings, the stopped/gone/daemon-unreachable cache-only degrade, and that `_require_benches`'s `NOT_FOUND` still propagates rather than degrading)
- ✅ `core/stop.py` - 100% (`test_core_stop`: stopped count, already-stopped, `NOT_FOUND`, docker-unreachable, names-not-objects, and that it prints nothing at all)
- ✅ `core/exec_stream.py` - ~97% (`test_core_exec_stream`: tagged chunks in order, the terminal `ExecDone`, a mid-character split round-tripping per stream, `ExitCode: None` polled through to a real code, an unknowable code raising `CwcliError(DOCKER)`, a `DockerException` mid-poll raising the same, the stream closed on early `break` and on an exception)
- ✅ `core/run.py` - 100% (`test_core_run`: every `run_plan` branch - success, `select_bench`, the `bench.default_used` warning, each `CwcliError` kind, `RunPlan` holding no live Docker object - plus the reseated `commands/run.py` frontend: exit-code passthrough, honest non-zero on an unknown code, `--bench`/`--path` plumbing, and the `confirm_start` retry-once-then-fail-closed race - and the argv surface, which drives the real `main.app` through Typer's parser because the parser config is what is under test: an unknown flag reaching bench, no `--bench` suggestion for `--branch`, `--bench` still claimed after the bench args, and `--` still shielding a colliding flag - plus `--interactive`, whose tests pin the SEPARATION as much as the behaviour: the argv handed to `docker exec`, that it never touches `core.exec_stream`, `-t` only with a terminal on both ends, the exit-code passthrough, the quoting round-trip, and the interrupt contract in both halves - a Ctrl+C reported as 130 only once the container-side process group is verified gone, and exit 1 when that termination cannot be verified)
- ✅ `core/open.py` + the reseated `commands/open.py` renderer - (`test_core_open`: every `open_plan` branch on container fakes - the declarative four-string `LaunchTarget` (container NAME, never an argv), all three `NEEDS_CHOICE` kinds including the new `select_editor`, the fallback-populate abort/degrade matrix (a hard `CwcliError` PROPAGATES, a non-`CwcliError` exception and a succeeds-but-still-nothing populate degrade to the default with `bench.default_used`), the verbatim `--app` pass (match-by-path never `[0]`, the in-memory refresh degrade), editor detection via stdlib `shutil.which`, core silence, asdict-is-plain-data, and the deliberately ABSENT `axi open` verb asserted against the registry; `test_open_characterization.py` is the green-before net (committed against unmigrated code, written through migration-surviving surfaces so it passes unchanged on both sides); `test_open_inspect_fallback.py` and `test_inspect_partial_refresh.py`'s open classes re-pointed with their subject, assertions untouched). See `openspec/changes/migrate-open-core`.
- ✅ `core/logs.py` - 100% (`test_core_logs`: the two moved reads (`_existing_files`/`_discover_bench_log_files`) against a container fake - now `container.exec_run`, not a `docker exec` shell-out, so covered instead of monkeypatched away wholesale; every `logs_plan` branch - the combined/`--process` log selection, the `select_bench`/`confirm_start`/`select_process` `NEEDS_CHOICE` forks, the two distinct no-logs errors (`logs.none_yet` NOT_FOUND vs `logs.no_manager` NOT_RUNNING), the not-cwcli-supervised fallback firing and NOT firing, the `bench.default_used` fallback, and the no-live-object/no-argv plan invariant; plus the `subprocess`-ban purity check. `commands/logs.py` keeps its `docker exec -it ... tail` and PR #83's exit-code fix, pinned through the public surface by `test_logs` (the five exit-code regressions + the fallback rendering + the `-it`-on-TTY gating), plus the `cwcli-logs-orphan-tail-o5` fix - the non-TTY `--follow` tail wrapped to record its own PID and reaped in a `finally` (asserted: the PID-recording `sh -c` wrapper is issued, the matching pidfile is reaped, and the reap still runs when the tail itself raises `KeyboardInterrupt`), guarded end to end against real dockerd by `tests/e2e/test_logs_orphan_tail_e2e.py` (no orphan `tail -F` survives the follower's exit in either the non-TTY or `-it` mode)). Since `add-axi-logs-verb`, `logs_plan`'s resolve is extracted into `_resolve_log_files`, SHARED with the bounded `read_logs` (`tail -n N`, no follow) that backs `cwcli axi logs`; `test_core_logs` also pins `read_logs` - the `tail -v` parse into per-process groups (raw program key labels, special-char-safe lines, the `--lines` bound, empty-file groups), the running-but-quiet empty-OK divergence, the no-manager raise, the shared choice forks, and the plain-data DTO)
- ✅ `core/config.py` + `core/auto_inspect.py` + the reworked `commands/config.py` renderer - (`test_core_config`/`test_core_auto_inspect`: every branch at the core - search-path validation/normalization incl. legacy unnormalized entries, the `confirm_clear` `NEEDS_CHOICE` fork with consent as a core parameter, the F3 validate-before-write structure, the fused enable/disable/stop desired-state actions, hook-sync warnings vs daemon INTERNAL errors, core silence, asdict-is-plain-data - all against a `tmp_path` config, never the real one; `test_config_characterization.py` is the green-before net driven through Typer's parser with fakes at the storage/process layer, its six disclosed-delta assertions (F3/F4/F9/F10) committed as strict xfails that flipped XPASS when the rework landed; `test_config_frontend.py` covers the new surface - `show`/`paths`/`edit`/`--json` reads, the fused enable/disable rendering, and the Decision-4 alias contract: hidden registry flags, stderr-only deprecation warnings, stdout unchanged). See `openspec/changes/rework-config-dx`.
- ✅ `commands/axi.py` `config` verb - (`test_axi_config`: one TOON document carrying every store (nested `auto_inspect` dict, inline `search_paths`), exit 0/1 mapping, the no-arguments/no-flags pure-read signature, and the registry assertions that no config-mutating axi verb exists anywhere - the `axi apps uninstall` deferral discipline)
- ✅ `commands/axi.py` - ~96% (`test_axi`: verb exit-mapping (0/1/2), the content-first home - including `assert_is_one_toon_document`, now a recursive walker (tables, counted blocks, nested dicts, and `- ` record items) so a prose line can never reach the one-TOON-document stdout even in a nested document - `axi ls`/`axi where`, the TOON encoder; `test_axi_inspect`: the `axi inspect` verb - flag-to-refresh mapping, nested bench records as TOON not Python reprs, degrade-to-cache as WARNING/exit 0, stopped-project usage error naming `cwcli start`, NO `--yes` registered, and the `axi benches` dead-end hint re-pointed at `cwcli axi inspect`)
- ✅ `utils/agent_hooks.py` - 100% (`test_agent_hooks`: the `cwcli axi setup` installer against a THROWAWAY `tmp_path` home, never the real one - install into all three detected harnesses, an undetected one skipped rather than bootstrapped, the idempotent re-install, a moved executable repaired in place rather than duplicated, foreign hooks/settings surviving, a corrupt config replaced; the Codex `[features] hooks = true` append preserving comments and the `manual` refusal to rewrite a hand-maintained `[features]`; the OpenCode plugin's spawn args and stale rewrite; `hook_command`'s PATH-vs-absolute rule; `_is_cwcli_hook`'s false-positive guard)
- ✅ `scripts/build_skill.py` - 100% (`test_axi_skill`: the `--check` staleness gate run AS A UNIT TEST so the existing `Pytest` job blocks a stale skill with no extra CI step - stale/fresh/missing/build modes, every registered `axi` verb appearing, grouped `apps *` qualified, RST backticks collapsed, trigger-shaped frontmatter, `uvx` invocations, no live state, the documented deferrals, and `test_a_new_verb_makes_the_committed_skill_stale`, which injects a fake verb and proves the gate has teeth); the same file's `TestInternalSkillsAreNotPublished` asserts every `.claude/skills/*/SKILL.md` carries `metadata: internal: true` (so `skills add` never publishes the internal deep-dives to a user) and that the public `skills/cwcli/SKILL.md` never gains that marker
- ✅ `core/list.py` - 100%, `core/where.py` - ~99% (`test_core_list`: fake docker client, empty/aggregate/DOCKER-raise; `test_core_where`: throwaway sqlite, dedup/scoping/installed-only/USAGE, plus `TestVerifiedVsRemembered` - a cached match cross-checked against the live project listing, `present`/`absent`/`unverified` `project_state`, a stale row reported not pruned, an unreachable daemon degrading to `unverified` rather than an empty-set false-clean, and `verify=False` claiming nothing)
- ✅ `commands/list.py` - ~80% (`test_list`: the `ls --json` empty-`[]` fix, quiet/table rendering, port-range condensing)
- ✅ `commands/where.py` - 100% (`test_where`: table/JSON rendering, the `--apps`/`--sites` conflict, the definitive `[]` empty state; `TestWhereStalenessIsVisibleToAHuman` - the Instance column/state, the `--json` `project_state` field, `--no-verify`)
- ✅ `core/supervision.py` (`test_core_supervision`: supervisord discovery + label-mapping + bench-keying, the expected-set Procfile parse, config/launcher generation, the `pip install supervisor` fail-closed bootstrap, the supervisor marker present/absent, the web probe, per-process log-path resolution, `discover_unsupervised_stack`'s honcho/`bench start` fallback (cwd-keyed, live processes reported up, no-manager case), the real `frappe <cmd>` bench-helper cmdline forms mapping to labels, `web_is_serving`/`wait_web_ready`'s any-code-is-up definition, timeout, retry-until-bound polling, and `fused_probe`'s one-exec parse, honest unknowns, fail-closed reads, and read-only guard)
- ✅ `core/start.py` - ~98% (`test_core_start`: the launch outcome, the idempotent no-op, multi-bench `NEEDS_CHOICE`, an explicit `bench_path` used verbatim, the `--autorestart`/`--no-autorestart` config-state toggle, missing-project/docker-unreachable errors, the web-readiness wait threading `web_ready` True/False/None across launch/timeout/no-web-program/no-op)
- ✅ `core/status.py` - ~93% (`test_core_status`: every `overall` branch including the stable-partial-stack `degraded` (a program `FATAL` while web serves), the offline-not-raised contract, supervisor-down vs never-started, the docker-unreachable raise, the not-cwcli-supervised honcho fallback (real up/pid/uptime, the `degraded` case, the no-manager and still-supervised non-flagged cases))
- ✅ `core/restart.py` - ~95% (`test_core_restart`: `core.restart_process` restarting one program leaving siblings running, multi-bench `select_bench` and unknown/ambiguous `select_process` `NEEDS_CHOICE`, `NOT_RUNNING` for a down supervisor/container)
- ✅ `commands/status.py` - ~93% (`test_status_frontend`: the stdout-token-only contract the PR-1 E2E net pins, per-process detail (incl. supervisord `state`) on stderr, exit 0 across lifecycle states, the not-cwcli-supervised heading/hint; `test_status_watch`: the `--watch` live view - every tick re-polls with `probe_web=False` so it makes ZERO web requests, the non-TTY single-quiet-snapshot degrade, the `--interval` 1s floor, a clean `KeyboardInterrupt` exit)
- ✅ `commands/axi.py` `start`/`status` verbs - (`test_axi_start_status`: TOON rendering, exit-code mapping, needs-choice/`CONFLICT` flag-naming, the never-prompt port-conflict pre-step)
- ✅ `commands/axi.py` `restart` verb - (`test_axi_restart`: TOON `ProcessRestartOutcome` rendering, exit-code mapping, `--process` required, unknown/ambiguous process and multi-bench `select_bench` as usage errors listing valid labels)
- ✅ `commands/axi.py` `unlock`/`stop` verbs - (`test_axi_unlock_stop`: TOON `UnlockOutcome`/`StopOutcome` rendering incl. `removed` as a structured list, exit-code mapping, `axi unlock`'s multi-bench `select_bench` and stopped-container `confirm_start` as usage errors (no `--yes` on this verb - it mirrors `axi backup`), `axi stop`'s idempotent already-stopped success and structured not-found error)
- ✅ `commands/axi.py` `benches`/`label`/`self-update` verbs - (`test_axi_label`: TOON `BenchList`/`LabelOutcome` rendering, exit-code mapping, `axi benches`' uninspected-project error naming inspect rather than an empty list (since `migrate-inspect-core` the axi frontend re-points that hint at `cwcli axi inspect`; `test_axi_inspect` pins the re-point), `axi label`'s required-and-exclusive `--set`/`--clear` (neither is a usage error pointing at `axi benches`, NOT an implicit list), the multi-bench `select_bench` usage error, the `NOT_RUNNING` hint that does not name a flag `label` lacks, and `axi self-update --check`'s deliberate exit-0-when-outdated divergence from the human `--check`'s exit 1)
- ✅ `commands/axi.py` `logs` verb (`add-axi-logs-verb`) - a bounded log-read verb over the new `core.read_logs` (`test_axi_logs`: one TOON document - a metadata head then one raw-line block per process, stdout stays pure TOON even when log lines carry `:`/`,`/`"`; the exit mapping - stopped -> usage exit 2 naming `cwcli start`, multi-bench/unknown-`--process` -> usage exit 2 naming the flag, running-but-quiet -> empty success exit 0, no-manager/Docker-down -> exit 1; and the guard: the verb is registered (closing the `logs` half of the deferral guards) and deliberately carries no `--follow`/`--yes`). See `openspec/changes/add-axi-logs-verb`.
- ✅ `core/credbridge.py` - ~99% (`test_core_credbridge`: host dispatch by the `host=` field to `gh`/`glab`, the context manager's stand-up/answer/teardown over a fake container including teardown on both setup failure and a raise from the wrapped op, the accept loop surviving repeated `settimeout` wakes, and two overlapping bridges against the same bind mount keeping independent per-invocation socket/shim/git-config entries). Wrapped by `core.apps.install_apps`'s get-app fan-out and `core.update.update`'s whole dispatch; no dedicated E2E (the real proof against live GitHub/GitLab repos was run by hand, not committed as a harness test)
- ✅ `core/version.py` - ~90% (`test_core_version`: install-method detection tree (dev/uv/uvx/pip fallback), the fail-open PyPI lookup, PEP 440 compare including the dev-ahead case, the TTL cache, and the `passive_notice` cache-only gate incl. the `attempted_at` once/day refresh throttle)
- ✅ `commands/self_update.py` - 100% (`test_self_update`: dev/uvx no-op, the default upgrade run (success/failure/`FileNotFoundError`), `--check`, `--no-cache`)
- ✅ `update_notice.py` - ~95% (`test_update_notice`: stderr-only rendering never on stdout, `sys.stderr.isatty()` gating, `CWCLI_NO_UPDATE_CHECK` suppression, fail-open when the core gate raises)
- ✅ `core/scale.py` + `commands/scale.py`/`axi scale` renderers - (`test_core_scale`: the published-range parse/widen, the idempotent no-op when the range already covers every bench, `consent=False` returning `NEEDS_CHOICE` `confirm_scale` only when expansion is needed, the mandatory `--no-deps` recreate, a newly-needed port already in use refused BEFORE any write, a failed recreate rolling the compose file back rather than stranding a widened-but-unapplied file, an unreadable bench's ports reported `ports_verified=False` rather than fabricated, the `--to` floor, and the v13/v14 toolchain repair reinstalling only a genuinely broken interpreter while a working v16 interpreter is left alone). `tests/e2e/test_scale_e2e.py` is the real-Docker net: the DB survives (MariaDB container id unchanged, a seeded marker read back), frappe is recreated, the newly-covered ports are published, and `cwcli axi scale` emits TOON and is idempotent on a second run.

### Modules Needing Dedicated Suites
- ⚠️ `utils/port_utils.py` (~9%, only incidental coverage)
- ⚠️ `utils/docker_utils.py` (`test_core_resolvers`'s `get_frappe_container` CLI wrapper; the rest of the module (`handle_docker_errors`'s error branches, `exec_into_container`) is still only incidentally covered. The UI-free primitives `utf8_stream_decoder`/`get_project_containers`/`get_project_volumes` MOVED to `core/docker.py` so the UI-pure core never imports this UI module - `test_exec_stream_decode.py` dedicated-tests `utf8_stream_decoder` there now. `decode_exec_stream` is GONE: `core/exec_stream.py` superseded it and its only three callers were re-pointed at the primitive)
- ✅ `utils/sendme_utils.py` - ~89% (`test_sendme_utils`: path/target resolution across every OS/arch branch, `is_sendme_installed`/`get_sendme_command` local-vs-PATH-vs-fallback, the clipboard helper's xclip/xsel/failure paths, `download_file_with_progress` stream-and-error, `install_sendme` over a REAL tar.gz/zip archive plus its no-asset/network/download-failure branches, and `setup_path`'s append/idempotent/create-bashrc paths; only the Windows-PowerShell PATH and macOS-pbcopy lines are unreached on a Linux run). The real bytes-over-sendme transport is proved by `tests/e2e/test_restore_p2p_e2e.py` (the `e2e_p2p` marker)
- ⚠️ `utils/vscode_utils.py` (~15%, only incidental coverage; editor detection itself moved to `core/open.py`, 100%)
- ⚠️ `utils/config_utils.py` (~41%; `cwcli_home()` is covered by `test_cwcli_home`, but `load_config`/`save_config`/the custom-path and auto-inspect-config setters remain untested)
- ⚠️ `commands/start.py` (~57%; `test_yes_flag` covers the non-interactive/`--yes` contract and `tests/e2e/test_start_status_e2e.py` drives it end to end, but the port-scanning/conflict-resolution branches still have no dedicated unit suite)
- ⚠️ `commands/restart.py` (~40%; the single-process path's core call is dedicated-tested via `test_core_restart`/`test_axi_restart`, and both the whole-stack and `--process` paths are driven end to end by `tests/e2e/test_start_status_e2e.py`/`tests/e2e/test_per_process_supervisor_e2e.py`, but the CLI frontend itself - the argv-forgiveness reparsing, the multi-project loop, `_resolve_process_choice`'s TTY/non-TTY forks - has no dedicated unit suite)
- ✅ `commands/label.py` - (`test_bench_label_db_and_command`: the 15 pre-migration tests, re-pointed at the core with their assertions untouched - including the two that pin the clear-path consistency invariant; `tests/e2e/test_label_e2e.py` drives the two-store invariant against a real container, non-interactive only because `label` has no prompt)
- ⚠️ `commands/unlock.py` (~50%; its logic moved to `core/unlock.py`, which is covered - see above; `test_unlock_command_cli.py` dedicated-tests the `--bench` selector resolution, and `tests/e2e/test_unlock_e2e.py` drives both modes end to end, but the CLI frontend's choice-resolution/verbose-print branches still have no dedicated unit suite)
- ⚠️ `commands/stop.py` (~68%; its logic moved to `core/stop.py`, which is covered - see above; `test_axi_unlock_stop.py`/`test_yes_flag.py`/`test_exit_codes.py` exercise it incidentally, but the CLI frontend itself - the multi-project loop, `stop_project_best_effort` - has no dedicated unit suite)
- ⚠️ Command modules at or near 0% dedicated coverage: `backup.py` (0%; its logic moved to `core/backup.py`, which is covered - see above). **`run.py` is no longer in this bucket**: its logic moved to `core/run.py` + `core/exec_stream.py` (both covered by `test_core_run.py` and `test_core_exec_stream.py`), and `test_core_run.py` also dedicated-tests the CLI frontend itself - the exit-code passthrough, the honest non-zero on an unknown code, the `--bench` plumbing, and the ambiguity rendering. `tests/e2e/test_run_e2e.py` drives both modes end to end, including `-i` against a genuine `bench new-app` (a real prompting command) over a pty and over a pipe, with the no-`-i` `EOFError` defect pinned so it cannot return. It was previously the one genuinely untested command in cwcli

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
# No `[ 42%]` progress indicator: the fast tier prints its own per-file progress.
console_output_style = "classic"
# The default `-m` deselects the real-Docker tiers, so a bare `pytest` runs only
# the fast unit tier; override with `-m e2e` on the CLI (the last `-m` wins).
addopts = ["-q", "--strict-markers", "--tb=short", "--cov-report=", "-m", "not e2e and not e2e_p2p and not e2e_pkg"]
markers = [
    "unit: fast tests that need no Docker daemon (the default tier)",
    "e2e: real-Docker end-to-end tests driving the real cwcli binary",
    "e2e_p2p: real-Docker P2P (sendme loopback) end-to-end tests (also carry e2e, so they ride the version matrix version-gated)",
    "e2e_pkg: real-Docker full-lifecycle test against a runtime-deps-only uv-tool-install binary (CWCLI_BIN)",
    "standalone: an e2e test that never touches the shared session instance (the CI group split)",
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
    run: uv run pytest -m unit --cov=caffeinated_whale_cli
  ```

  The required-check configuration is documented in the [CI/CD Workflows guide](../docs/contributing/ci-cd.md).

- **`.github/workflows/e2e.yml`** runs the real-Docker `e2e` tier as a Frappe v14/v15/v16 matrix on GitHub-hosted `ubuntu-latest` jobs.
  The `standalone` marker splits each version leg in two: the `standalone` job runs the tests that build their own instance or need none, the `shared` job runs the tests that assert against the one session-scoped instance, and they partition the tier exactly.
  `tests/e2e/conftest.py` fails collection if the marker disagrees with the fixtures a test requests.

See the [CI/CD Workflows guide](../docs/contributing/ci-cd.md) for triggers, runner isolation, required checks, and branch-protection state.

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

Status as of 1.0.0 (see [../docs/testing/README.md](../docs/testing/README.md) for the full list):

### Done
1. **Project inspection** (`commands/inspect.py`, now a renderer over `core/inspect.py` - see 4h) - covered by `test_inspect_characterization`, `test_core_inspect`, `test_inspect_partial_refresh`, `test_inspect_label_recovery`.
2. **Database operations** (`utils/db_utils.py`) - covered by `test_db_security`, `test_config_validation` (~73%).
3. **Real-Docker E2E for `init` and `backup`** - covered by `tests/e2e/test_init_e2e.py`, `tests/e2e/test_backup_e2e.py` (both interactive and non-interactive, on the v14/v15/v16 matrix).
3a. **Real-Docker E2E for the lifecycle commands** (`start`, `status`, `logs`, `restart`) - covered by `tests/e2e/test_start_status_e2e.py` (both modes; structure-agnostic outcome invariants that the start/status core migration preserved unchanged - see `openspec/changes/add-start-status-e2e-net` - plus `test_status_is_running_immediately_after_start`, the web-readiness regression net asserting `status` reads `running` the instant `start` returns, no wait needed between the two calls) plus `tests/e2e/test_start_status_new_behavior_e2e.py` (the migration's own net for the NEW behavior: idempotency, `degraded`, real per-process health, the relocated log, and the multi-bench DOCUMENT STRUCTURE over a bench SKELETON - see `openspec/changes/migrate-start-status-core`).
3b. **Real-Docker E2E for multibench status/start against genuinely serving benches** - covered by `tests/e2e/test_multibench_serving_e2e.py` on the standalone v16 leg.
It builds a real second bench on its own assigned port, while 3a deliberately covers only document structure over a bench skeleton.
Its opt-in sibling, `tests/e2e/test_multibench_latency_e2e.py`, records the six-bench latency curve when `CWE2E_LATENCY_BENCHES=6` or the workflow's `latency_benches` input is set.
The proof and measurements live in [`docs/e2e/multibench-serving-status.md`](../docs/e2e/multibench-serving-status.md).
4. **Logic core + `cwcli axi`** (`core/`, `commands/axi.py`) - covered by `test_core_envelope`, `test_core_resolvers`, `test_core_backup`, `test_axi` (envelope/resolvers/docker wrapper at 100%, `core/backup.py` ~95%, `commands/axi.py` ~96%). `start`/`status` followed the same pattern onto `core/start.py`/`core/status.py`/`core/supervision.py`, covered by `test_core_start`, `test_core_status`, `test_core_supervision`, plus the frontends `test_status_frontend` and `test_status_watch` (the `--watch` live view's zero-web-probe/non-TTY-degrade/interval-floor/clean-`KeyboardInterrupt` contract) and `axi start`/`axi status` in `test_axi_start_status`. `test_core_supervision` also covers the Console foundation's one-exec fused health probe. `add-per-process-supervisor` re-pointed `core/supervision.py` from honcho to supervisord and added `core/restart.py` (~95%, `core.restart_process` restarting one program leaving siblings running), covered by `test_core_restart` and the `cwcli axi restart` verb in `test_axi_restart`.
4a. **`self-update`** (`commands/self_update.py`, `core/version.py`) - covered by `test_core_version` (install-method tree, fail-open PyPI lookup, PEP 440 compare, TTL cache - ~90%) and `test_self_update` (dev/uvx no-op, the default upgrade run, `--check`, `--no-cache` - 100%). No `cwcli axi` verb (deferred).
4b. **Passive update notice** (`update_notice.py` ~95%, wired once into `main.py`'s root Typer callback) - covered by `test_update_notice` (stderr-only, TTY-gated, `CWCLI_NO_UPDATE_CHECK`-suppressible, fail-open) and `test_core_version`'s `TestPassiveNotice` class (the cache-only hot path, the detached background refresh, the `attempted_at` once/day throttle on persistent failure).
4c. **`unlock`+`stop` onto the logic core** (`core/unlock.py` ~98%, `core/stop.py` 100%, the shared bench-op helpers in `core/resolvers.py` ~97%) - the foundation's generality proof: `unlock` was migrated with zero new primitives. Covered by `test_core_unlock`, `test_core_stop`, `test_unlock` (re-pointed argv-injection guards), `test_unlock_command_cli` (the `--bench` selector resolution), and the `cwcli axi unlock`/`cwcli axi stop` verbs in `test_axi_unlock_stop`. `tests/e2e/test_unlock_e2e.py` closes `unlock`'s standing both-modes E2E gap (pty-driven interactive leg, non-interactive `--yes` leg, real locks-directory removal); `stop` needed no new E2E, already pinned by `test_start_status_e2e.py`/`test_status_unsupervised_e2e.py`. See `openspec/changes/migrate-unlock-stop-core`.
4d. **`label` onto the logic core, plus `cwcli axi benches`/`cwcli axi label`/`cwcli axi self-update --check`** (`core/label.py` ~98%) - split into `list_benches`/`set_label`/`clear_label` rather than one function, built with zero new core primitives beyond widening `resolvers.resolve_container_state`'s hardcoded `--yes` hint into a caller-supplied `not_running_hint` parameter (every existing caller unchanged). Covered by `test_core_label` (every branch, including the marker-before-cache clear ORDER asserted directly), `test_bench_label_db_and_command` (the 15 pre-migration tests, re-pointed at the core with their assertions untouched), and `test_axi_label` (the new `axi benches`/`axi label`/`axi self-update --check` verbs). `tests/e2e/test_label_e2e.py` covers the DB/marker two-store consistency invariant against a real container, non-interactive only (`label` has no prompt). See `openspec/changes/migrate-label-core`.
4e. **The exec-stream contract, and `run` onto the logic core** (`core/exec_stream.py` ~97%, `core/run.py` 100%) - the first batch to ADD architecture rather than migrate onto it: one `exec_create`/`exec_start`/`exec_inspect` loop, reused by `run`, `apps`, and `update`, replacing the fail-open that let a bench command with an unknown exit code report success (`run` exited 0 via `typer.Exit(code=None)`; `apps uninstall-app` reported `"ok": true`). Covered by `test_core_exec_stream` (tagged chunks, per-stream mid-character splits, the bounded exit-code poll settling vs. expiring, a dropped connection mid-poll as a typed error rather than a guess) and `test_core_run` (every `run_plan` branch plus the reseated CLI frontend: exit-code passthrough, the honest non-zero on an unknown code, `--bench`/`--path` plumbing, and the `confirm_start` retry-once-then-fail-closed race mirroring `backup`/`unlock`). `apps` and `update` are re-pointed at the primitive, not migrated as commands; `tests/test_apps.py` staying green unchanged is the refactor's proof, now extended with the exec-stream `CwcliError` handling at the exec-loop choke point. `tests/e2e/test_run_e2e.py` gives `run` its first both-modes E2E, including a >32KB unicode leg no faked stream can reproduce. See `openspec/changes/add-exec-stream-contract`.
4f. **`update` onto the logic core, plus `apps update --json` and `cwcli axi apps update`** (`core/update.py` ~90%, the reseated `commands/update.py` renderer ~88%) - the largest migration the rework has done, and the one where the state machine's coverage went from **71.06% to 90.46%**. `core.update(...) -> Result[UpdateReport]` returns the seven-way aggregation instead of printing it. Ordered so the risky part landed under tests that actually cover it: `test_update_characterization` was written FIRST against unmigrated `develop` (the five untested aggregation branches plus `--build`/`--skip-maintenance`/`--clear-website-cache`/`--no-recache`/multi-app fan-out - none of which had ANY coverage, which is exactly where PR #81's reporting bug shipped) and passes **byte-identical** either side of the migration; only the shared fake helpers in `test_apps.py` were re-pointed. `test_core_update` covers the envelope, the load-bearing maintenance gate, the `finally` under a raise (**by test, never by inspection**), `UpdateAborted` on a Ctrl-C, unknown-vs-failed, and the frappe fork; `test_axi_apps_update` covers both structured surfaces (stdout purity on the frappe path included, exit 0/1/2, and that `report.ok` - not `result.status` - drives the exit). `tests/e2e/test_apps_update_e2e.py` closes the standing gap that `apps`/`update` had NO E2E at all: both modes on a real instance, with `bench` shimmed for the output-purity legs because an empty bench output cannot show a purity break. See `openspec/changes/migrate-update-core`.
4g. **`apps`'s remaining three subcommands onto the logic core, plus `cwcli axi apps list`** (`core/apps.py` ~98%, the reseated `commands/apps.py` renderer ~98%) - finishes what 4f left half-migrated: `list`/`install`/`uninstall` join `update` on the core, so `commands/apps.py` no longer carries two contracts (a typed report from one subcommand, hand-rolled result dicts from the other three). `test_apps_characterization.py` is the green-before net, committed as its own commit against unmigrated code (`commands/apps.py` 91.28% -> 99.49%) so refactor-under-green is auditable rather than claimed. `test_core_apps.py` covers every branch the CLI pre-resolves away and only `axi`/a future GUI can reach - `confirm_start`, `select_bench`, the no-cache default, `confirm_uninstall` - plus that the core prints nothing at all and returns plain serializable data. `test_axi_apps_list.py` covers the new read verb: TOON rendering, exit 0/1/2, a failed site read reported as `null` (never an empty list), and a dedicated assertion that `axi apps install`/`uninstall` are NOT registered (captain-locked 2026-07-15 - an agent destroying site data is a product decision on its own evidence, not a side effect of moving code). No new E2E: `list`/`install`/`uninstall` still have none (item 10), unchanged by this batch. See `openspec/changes/migrate-apps-core`.
4h. **`inspect` onto the logic core, plus `cwcli axi inspect`** (`core/inspect.py`: the tier machine, the discovery/gather fan-out, AND the cache write; `commands/inspect.py` reseated as a renderer) - kills the frontend-calling-frontend class at all seven consumer edges (`recache_project`'s body, `auto_inspect`, `open`'s fallback populate + in-memory `partial_refresh`, `update`'s fallback, `restore`'s three fallback copies, `rm`'s live `discover_benches`) and with it `core/update.py`'s runtime reach into the CLI layer - the last such site. `test_inspect_characterization.py` is the green-before net (byte-identical `--json` per tier, the persisted cache shape, T2 passivity under `--yes`, the T3 `--yes`/non-TTY contract), `test_core_inspect.py` pins the envelope at the core (forks, per-tier write discipline, the two disclosed hardenings, silence, plain-data DTOs), and `test_axi_inspect.py` covers the new tiered read verb (one TOON document with nested bench records, exit 0/1/2, degrade-to-cache as WARNING/exit 0, NO `--yes`, and the `axi benches` dead-end hint re-pointed at `cwcli axi inspect`). See `openspec/changes/migrate-inspect-core`.
4i. **`logs` onto the logic core, plus `cwcli axi logs`** (`core/logs.py` 100%) - the resolve (`_existing_files`/`_discover_bench_log_files` + every `logs_plan` branch, the not-cwcli-supervised honcho fallback, the two distinct no-logs errors) moved off `commands/logs.py`; the `docker exec -it ... tail` handover and PR #83's exit-code fix stay in the frontend. Covered by `test_core_logs` (the migrated reads on a container fake, the `subprocess`-ban purity check, and `read_logs` - the bounded `tail -n N` behind `cwcli axi logs`), `test_logs` (the five exit-code regressions, the fallback rendering, `-it` TTY gating, and the `cwcli-logs-orphan-tail-o5` reap), and `test_axi_logs` (the new read verb - one TOON document, the exit mapping, no `--follow`/`--yes`). `tests/e2e/test_logs_orphan_tail_e2e.py` proves no orphan `tail -F` survives against real dockerd. See `openspec/changes/migrate-logs-core`, `openspec/changes/add-axi-logs-verb`.
4j. **`open` onto the logic core** (`core/open.py`) - `open_plan(...) -> Result[LaunchTarget]` resolves everything (container, run-state, bench, the no-cache fallback populate, `--app`, editor detection via stdlib `shutil.which`); `commands/open.py` is a renderer whose final lines perform the `docker`/editor handover. Covered by `test_core_open` (every branch on container fakes - the four-string `LaunchTarget`, all three `NEEDS_CHOICE` kinds incl. the new `select_editor`, the fallback-populate abort/degrade matrix, and the ABSENT `axi open` verb asserted against the registry), `test_open_characterization.py` (the green-before net), and the re-pointed `test_open_inspect_fallback.py`. See `openspec/changes/migrate-open-core`.
4k. **`init` onto the logic core, plus `cwcli axi init`** (`core/init.py`) - the largest migration and the exec-stream contract's last consumer, split into `init_instance`/`init_bench` with the existing-bench decision as the new `confirm_reuse_bench` choice. Covered by `test_init_characterization` (the green-before net - 10-exec order/strings, secrets-only-on-new-site `environment=`, the port-conflict/ENOSPC/refusal paths), `test_core_init` (the envelope at the core - the three choice surfaces, the secret audit pinning no value in any event/DTO/warning/echo, version gating, the honest lost-stream `DOCKER` error), the re-pointed `test_init_reuse_bench`/`test_init_admin_password`/`test_init_frappe_version`/`test_init_mariadb_flag`, and `test_axi_init` (the shipped `cwcli axi init` verb - both password transports, the choice-to-usage-error mappings, one-terminal-document stdout purity). See `openspec/changes/migrate-init-core`, `openspec/changes/add-axi-init-verb`.
4l. **`config` onto the logic core (and the DX rework), plus `cwcli axi config`** (`core/config.py` + `core/auto_inspect.py`) - the rework and the migration landed as one change. Covered by `test_core_config`/`test_core_auto_inspect` (every branch at the core against a `tmp_path` config - search-path validation/normalization, the `confirm_clear` fork with consent as a core parameter, the F3 validate-before-write structure, the fused enable/disable/stop desired-state actions), `test_config_characterization.py` (the green-before net, its six disclosed-delta xfails flipping XPASS when the rework landed), `test_config_frontend.py` (the new `show`/`paths`/`edit`/`--json` surface and the frozen-alias deprecation contract), and `test_axi_config` (the one read-only agent verb, with the registry assertion that no config-mutating axi verb exists). See `openspec/changes/rework-config-dx`.
4m. **`restore` onto the logic core** (`core/restore.py`) - cwcli's most destructive path, the plan/apply split (`restore_plan`/`receive_plan -> restore_apply`, a read/destroy safety separation). Covered by `test_core_restore` (the envelope at the core - both `select_backup`/`confirm_restore` choice surfaces, secret-in-`environment=` and exec arg order on both paths, the migrate-failure `WARNING`/`migrate_ok=False`, the encryption-key merge, the streamed copies, and the deliberately ABSENT `axi restore` verb asserted against the registry), `test_restore_characterization` (the green-before net), and the re-pointed `test_restore_safety`/`test_restore_inspect_fixes` (the confirm-count now 1 by design). `tests/e2e/test_restore_e2e.py` drives both modes on a real instance with a cache-free DB marker proving the destructive restore actually dropped-and-recreated the DB. See `openspec/changes/migrate-restore-core`.
4n. **`rm` onto the logic core** (`core/rm.py`) - the delete-with-backup-gate command, the most safety-critical code in the repo, as one plain function `core.remove(...) -> Result[RemovalOutcome]` (plan/apply declined on rm's real behavior). Covered by `test_rm_characterization` (the green-before net driving the real `rm.rm()` through a module-agnostic seam - the C1 abort-before-any-removal, the H5 `rm ..` refusal, the M11 partial-failure exit code, the exit-0 not-found no-op, multi-bench per-bench backup), `test_core_rm` (the envelope at the core - typed `USAGE`/`DOCKER` raises, the status mapping, plain-data DTOs), and the re-pointed `test_rm_safety`/`test_rm_truth`/`test_rm_stopped`/`test_rm_stopped_backup`. See `openspec/changes/migrate-rm-core`. The `axi rm` verb this design once deferred has since SHIPPED (`add-axi-rm-verb`, captain-approved 2026-07-21) - see `test_core_rm.py::TestAxiRmVerbShipped`, `test_axi_rm.py`, and the real-Docker `tests/e2e/test_axi_rm_e2e.py`.
4o. **The workspace bind-mount fix, `init` onto a per-project host `data/` dir** (`core/init.py`) - the ephemeral-bench fix: a custom `--bench-parent` used to land outside the upstream `..:/workspace:cached` mount and evaporate on container recreation. `init_instance` now rewrites the frappe workspace mount to `../data:{bench_parent}:cached` and `working_dir` to `{bench_parent}`, gated on `new_instance` (a frozen compose is never re-targeted), with a re-init mount-mismatch `USAGE` guard whose remedy hint reaches both frontends' stderr. Since the mount alone does not make a custom bench resolvable (the SQLite cache still needs repopulating), both `commands/init.py` and `commands/axi.py:axi_init` recache the project right after bench creation, degrading a recache failure to a stderr-only warning. Covered by `TestWorkspaceMount` in `test_core_init.py` (default/custom `--bench-parent` rewrite, `working_dir` at the mount root, the unchanged `volumes:` block, host `data/` creation, the frozen-compose non-rewrite, the re-init mismatch/match cases), `TestBenchParentMismatch` in `test_init_characterization.py` (the mismatch hint on the human CLI's stderr), and `TestRecache` in `test_axi_init.py` (the post-init recache, its stderr-only warning on failure, and axi's stdout purity). `tests/e2e/test_workspace_persistence_e2e.py` proves real persistence across a `docker compose down`/`up` recreation for both the default and a custom `--bench-parent`, and the frozen-compose refusal, on the v16 leg (version-agnostic mechanism). See `openspec/changes/map-bench-workspace-volume`.
4p. **Host-uid alignment for the workspace bind mount** (`core/docker.py`) - covered by `test_core_docker.py` (the no-op paths for matching ids and platforms without `os.getuid`, the uid+gid remap with/without the home chown, and failed remaps or unreadable ids degrading to warnings rather than raises).
The `id -u`/`id -g frappe` probe fake plumbing in `test_core_init.py`/`test_core_supervision.py` keeps the align step a no-op in those existing suites.
The host-uid alignment note in the `cwcli-lifecycle` skill's `init.md` reference owns the behavior and rationale.
4q. **TOON help across the complete `cwcli axi` tree** (`commands/axi.py`) - `TestAxiHelpIsToon` recursively walks the mounted Click registry, proves usage, commands, arguments, flags, required state, defaults, and examples, then rejects Rich box drawing, ANSI, blank alignment lines, trailing spaces, and column padding.
The same test proves a representative human leaf remains Rich.
`test_all_axi_help_is_toon_on_runtime_only_binary` repeats the complete 28-path walk against the `uv tool install .` binary in the `e2e_pkg` job, which also covers Typer 0.27's vendored Click parameter classes.
The measured before and after artifact evidence is `docs/e2e/axi-help-toon.md`.

### Partial
5. **Port conflict detection** (`commands/start.py`) - `test_yes_flag` covers the non-interactive/`--yes` contract; the interactive port-conflict confirmation prompt is driven end to end in `tests/e2e/test_start_status_e2e.py`, and the remaining port-scanning helpers still have no dedicated unit suite (~57%).

### Still needed
6. **Docker utilities** (`utils/docker_utils.py`) - foundation for all commands; the `get_frappe_container` CLI wrapper is covered by `test_core_resolvers` and the exec-stream decode helpers by `test_exec_stream_decode`, but the rest of the module is still only incidentally covered (~58%).
7. **Port utilities** (`utils/port_utils.py`) - cross-platform process detection (~9%).
8. **VS Code integration** (`utils/vscode_utils.py`) - container attachment fallback (~15%).
9. **Configuration management** (`utils/config_utils.py`) - distinct from `db_utils`'s config validation (~37%).
10. **Real-Docker E2E for the remaining commands** (`rm`, `apps list`/`install`/`uninstall`, `open`, `config`, `inspect`) - tracked in `openspec/changes/rebuild-e2e-test-suite`. The P2P (`sendme`) loopback is no longer in this list: `tests/e2e/test_restore_p2p_e2e.py` fills the `e2e_p2p` marker with a real send->receive transfer (see item 4c's sibling above), and `test_sendme_utils.py` lifts `utils/sendme_utils.py` off its 9% floor. `unlock` is no longer in this list: `tests/e2e/test_unlock_e2e.py` covers it (see item 4c). `update`/`apps update` is no longer in this list either: `tests/e2e/test_apps_update_e2e.py` covers it (see item 4f). `restore` is no longer in this list: `tests/e2e/test_restore_e2e.py` drives both modes with a cache-free DB marker proving the destructive restore (see item 4m). `apps`'s other three subcommands moving onto the core (item 4g) did not close this gap - it is unit-tier only, by design (see `migrate-apps-core/tasks.md`). `install`'s fetch/install execution is now PARTLY closed: `tests/e2e/test_axi_apps_install_e2e.py` drives the scoped `cwcli axi apps install` verb through a real bench end to end (the permitted install, the same-command refusal, the git-URL refusal, and a fail-closed unreadable site), but the human `cwcli apps install`'s own every-site fan-out and interactive/non-interactive confirm paths stay unit-tier only, so `install` remains in this list.
11. Other command modules at or near 0% dedicated unit coverage: `backup.py` (its logic moved to `core/backup.py`, which is covered). `status.py` and `unlock.py` are no longer in this bucket: `test_status_frontend`/`test_status_watch` dedicated-test `status.py` (~95%, on top of `tests/e2e/test_start_status_e2e.py`, see item 3a), and `unlock.py`'s logic moved to `core/unlock.py` (covered, see item 4c) with `commands/unlock.py` itself now at ~50% via `test_unlock_command_cli`. `run.py` is also no longer in this bucket: its logic moved to `core/run.py` + `core/exec_stream.py` (see item 4e), and `test_core_run.py` dedicated-tests the reseated CLI frontend itself.

## Resources

- [Testing Guide](../docs/testing/guide.md) - Comprehensive testing documentation
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-cov Plugin](https://pytest-cov.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
