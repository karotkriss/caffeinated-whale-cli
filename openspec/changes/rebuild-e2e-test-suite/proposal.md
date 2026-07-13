## Why

cwcli's entire test suite is mock-only and proves nothing about real behavior.
A full run against a deliberately broken Docker endpoint (`DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest`) still passes all 409 tests in ~9s: nothing in `tests/` ever opens a Docker daemon (recon r4 §1).
Every destructive command is "covered" only by asserting the exact shell string it hands to a fake `container.exec_run`, so the coverage numbers measure "did we build the right command", not "did the instance actually get backed up / restored / removed".
The fakes are a shadow re-implementation of bench's and Docker's behavior that drifts from reality silently - the `docker top` secrecy correction (commit `e8f35e9`) is a documented case of the mock model asserting something the real system does not do.
Worse, the destructive paths the captain cares about most are where the mock model is thinnest: `backup.py` and `unlock.py` have no dedicated suite at all, and `rm`/`restore` coverage is mock-string coverage against a fake container.
The declared `slow`/`integration`/`unit` markers are dead scaffolding (never applied to a single test), and there is no shared `FakeFrappeContainer` - the name is re-implemented independently in three files plus a fourth multi-bench variant.

Meanwhile the project already has a mature, isolated, real-instance E2E discipline that is run **by hand** for every behavior change (`docs/e2e/` worked runs; the "End-to-End Testing" recipe in `AGENTS.md`).
That manual recipe - temp `HOME`, unique `cwe2e-*` names, genuine `cwcli init` benches, pexpect for TTY prompts, flags for non-interactive, teardown - is the de-facto spec for a real E2E harness.
This change automates and CI-ifies that recipe so the destructive-path guarantees are enforced by machine on every PR, across the Frappe versions that actually diverge (v14/v15/v16), instead of resting on brittle string matches plus a human remembering to re-run the docs by hand.

## What Changes

- **Two-tier pytest suite.** Keep the mock-free pure-logic tests (validators, the `resolve_frappe_ref`/`_frappe_major_version` resolver, the `_redact_config_for_cache` whitelist, the label model, MariaDB-flag selection, `select_backup_set`) as a fast `unit` tier that runs with no Docker in the existing uv container. Add a real-Docker `e2e` tier whose session-scoped fixtures stand up a genuine instance with `cwcli init` and tear it down with `cwcli rm`, driving the real `cwcli` console script (so Typer parsing is exercised too). Repurpose the declared-but-unused `slow`/`integration`/`unit` markers into a real `unit` / `e2e` / `e2e_p2p` split. Reject `testcontainers` - cwcli itself owns the `docker compose` lifecycle, so the harness drives *cwcli*, not containers directly.
- **Real side effects, not string matches, for the destructive commands** (this absorbs the standalone destructive-path coverage task t6). Each of `rm`, `restore`, `backup`, `update`, `unlock` gets a real-instance E2E asserting the actual outcome: `rm` removes the named volumes + project dir AND a verified non-empty DB dump existed first (the C1 gate); `backup` produces a real non-empty dump; `restore` actually replaces the site's data + runs `bench migrate` + restarts; `update` pulls/migrates apps + toggles maintenance mode; `unlock` clears the site lock. Both interactive (pexpect, awaiting the prompt_toolkit `ESC[?2004h` raw-mode marker before keystrokes) AND non-interactive (flags) per command.
- **Isolation + hard safety rails.** Temp `HOME` + a new explicit `CWCLI_HOME` override (see below) + unique `cwe2e-` project/site names + a port allocator (bases ≥1006 apart; web `{port}..{port+5}`, socketio `{port+1000}..{port+1005}`). A hard rail refuses to run any E2E if `HOME` resolves to the real home directory or if any project name lacks the `cwe2e-` prefix. An unconditional teardown backstop sweeps every `com.docker.compose.project` matching `cwe2e-` (`compose down -v` + volume prune by label + drop the temp `HOME`) so a crashed test never leaks. The captain's real instances must never be touched.
- **Source hardening (decision #9, its own PR): an explicit `CWCLI_HOME` env override** that relocates cwcli's on-disk footprint (`config`, `projects`, `cache/cwc-cache.db`) without repointing the whole process `HOME`. This is a genuine user-facing feature (redirect cwcli's state cleanly) and the isolation seam the E2E harness builds on, so it lands first/separately as source hardening the rest of the work depends on.
- **Permanent v14/v15/v16 CI matrix.** One parallel GitHub-hosted `ubuntu-latest` job PER Frappe version (drop the `container:`; install `uv` via `astral-sh/setup-uv`), running the full real `cwcli init` each run. Version-agnostic E2E tests run **once** (on v16); the version-sensitive set runs per version: the MariaDB flag (`≤14 --no-mariadb-socket` vs `15+ --mariadb-user-host-login-scope=%`), the pyenv/nvm install branches (v14 python3.10 + node16 + yarn, v15 python3.12, v16 image defaults), the v14-only `--receive` bare-filename bug, and apps behaviors.
- **Two-tier CI gating.** The fast `unit` job runs on every push/PR (stays in the uv container). The `e2e` matrix is required on PRs into protected branches plus on-demand via a PR label. (Branch protection is not enabled on `develop`/`master` today; it must be turned on for "required" to actually block a merge.)
- **Track latest upstream, with diagnosable failures and authenticated pulls.** `init` keeps pulling the compose file from `frappe_docker@main` and the latest `frappe/bench` tag, matching real user behavior so the E2E catches upstream breakage. Upstream-caused failures (compose fetch / image pull / bench-tag lookup) surface as a **distinct "upstream pull/compose fetch failed" signal**, never an ambiguous red assertion. Docker Hub anonymous pull rate limits are mitigated with authenticated pulls.
- **A single gated P2P loopback E2E.** `restore --send`/`--receive` is covered by one loopback send+receive on a single runner, marked `e2e_p2p`, NOT run on every version leg.
- **An explicit per-job runtime budget.** Every E2E job sets `timeout-minutes` (start ~45 - generous but well under GitHub's 6h cap so a hung `bench init` fails fast). Provisional, to tune once real runtimes are known.
- **Parallel-run transition, not a big-bang.** The current mock suite stays green while the E2E layer is built; each command's brittle container-mock tests are retired only as its real E2E lands and proves out. The pure-logic tests are kept permanently.

## Capabilities

### New Capabilities
- `cwcli-home-override`: an explicit `CWCLI_HOME` environment override that relocates cwcli's entire on-disk footprint (config, projects, cache DB) to a caller-chosen directory in place of `~/.cwcli`, resolved at a single point, taking precedence over `HOME`, without repointing the process `HOME` (so git/ssh/other HOME-derived tooling is unaffected).
- `e2e-test-suite`: a two-tier pytest suite - a permanent fast mock-free `unit` tier plus a real-Docker `e2e`/`e2e_p2p` tier that drives the real `cwcli` binary against genuine throwaway Frappe instances - with hard isolation safety rails and a leaked-resource teardown backstop, real-side-effect assertions for the destructive commands in both interactive and non-interactive modes, a permanent v14/v15/v16 CI matrix (agnostic-once, sensitive-per-version), two-tier CI gating, latest-upstream tracking with a distinct upstream-failure signal and authenticated pulls, a gated single P2P loopback test, explicit per-job runtime budgets, and a parallel-run migration off the mock suite that keeps the pure-logic tests permanently.

### Modified Capabilities
- (none - `openspec/specs/` holds no archived capabilities yet, so nothing's requirements change. The deprecation of the mock-based container tests is a test-code change captured by the new `e2e-test-suite` capability's transition requirement and the Impact below.)

## Impact

- **New source (decision #9, separate PR):**
  - `src/caffeinated_whale_cli/utils/config_utils.py` and `src/caffeinated_whale_cli/utils/db_utils.py` - resolve the cwcli base directory through one helper that honors `CWCLI_HOME` (env) before falling back to `Path.home() / ".cwcli"`; `CONFIG_DIR`, `PROJECTS_DIR`, `CACHE_DIR`/`DB_PATH` all derive from it, keeping the existing 0700/0600 permissions.
  - `README.md` - document the `CWCLI_HOME` override; `CHANGELOG.md` entry.
- **New tests / harness:**
  - A `tests/e2e/` package: shared session/instance fixtures (real `cwcli init` up, `cwcli rm` down), the isolation safety rails + `cwe2e-` label teardown backstop, a port allocator, a pexpect helper (awaits `ESC[?2004h`), and per-command real-side-effect E2E modules for `rm`/`restore`/`backup`/`update`/`unlock` (+ `init`, `apps`) in both modes, plus a single `e2e_p2p` loopback restore test.
  - `pyproject.toml` - repurpose the `slow`/`integration`/`unit` markers into real `unit` / `e2e` / `e2e_p2p` markers; add an E2E-only dep group (`pexpect`) and a default `-m "not e2e and not e2e_p2p"` so a bare `pytest` stays fast.
- **New CI:**
  - `.github/workflows/e2e.yml` - a `strategy.matrix.frappe: [14, 15, 16]` of GitHub-hosted `ubuntu-latest` jobs (no `container:`), `astral-sh/setup-uv`, authenticated Docker Hub login, `timeout-minutes`, a distinct upstream-failure step/annotation, and the `cwe2e-` teardown backstop in an `always()` step. Version-agnostic tests run only on the v16 leg; the P2P loopback runs on one gated leg.
  - `.github/workflows/test.yml` - the existing `Pytest` job stays in the uv container but runs only the fast `unit` tier (`-m unit`).
- **Modified tests (parallel-run retirement):** the container-mock behavior tests in `tests/test_rm_safety.py`, `tests/test_restore_safety.py`, `tests/test_restore_inspect_fixes.py`, `tests/test_inspect_partial_refresh.py`, `tests/test_apps.py`, `tests/test_yes_flag.py`, and the shared fakes (`tests/bench_fakes.py`, `tests/bench_fakes_mb.py`) are retired per command **only as** its real E2E lands and proves out - not up front.
- **Kept permanently (pure logic, mock-free):** `tests/test_init_mariadb_flag.py`, `tests/test_config_validation.py`, `tests/test_bench_labels.py`, the resolver cases in `tests/test_init_frappe_version.py`, the `select_backup_set` / redaction-whitelist tests, and any other test that touches no Docker.
- **Docs:** update `tests/README.md` and `docs/testing/README.md` for the two-tier model; add an E2E-harness note to `AGENTS.md`; the manual `docs/e2e/` runs remain the human-precedent reference the harness is built from.
- **Dependencies:** add `pexpect` (E2E extra only). No runtime dependency added. `testcontainers` explicitly NOT added.
- **Future optimizations noted, not built now:** a pre-built "sited" base image per version and/or self-hosted runners to amortize `init` cost - revisit once real per-job runtimes are known.

## Non-Goals

- Removing the pure-logic unit tests (kept permanently) or deleting the mock suite in a big bang (retired per command, parallel-run).
- A `testcontainers`-managed lifecycle (cwcli owns `docker compose`; the harness drives cwcli).
- A pre-built base image, image-layer cache pipeline, or self-hosted runners in this change (future optimizations only).
- Pinning upstream refs for reproducibility (deliberately track latest to catch upstream breakage; the mitigation is a diagnosable upstream-failure signal, not pinning).
- Windows/macOS Docker E2E (Linux-only; `port_utils`, `startup`, `vscode_utils`, and named-pipe Docker-error paths stay unit-tested).
- Frappe v13 coverage (out of the requested matrix).
