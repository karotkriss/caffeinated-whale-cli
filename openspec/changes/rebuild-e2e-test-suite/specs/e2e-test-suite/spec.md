## ADDED Requirements

### Requirement: Two-tier pytest suite (fast unit + real-Docker E2E)
The test suite SHALL be organized into two tiers selected by pytest markers.
A fast `unit` tier SHALL contain only mock-free pure-logic tests (input validators, the `resolve_frappe_ref` / `_frappe_major_version` version resolver, the `_redact_config_for_cache` whitelist, the label model, MariaDB-flag selection, `select_backup_set`) and SHALL require no Docker daemon.
A real-Docker `e2e` tier SHALL drive the real `cwcli` console script against genuine throwaway instances.
The declared-but-unused `slow` / `integration` / `unit` markers SHALL be repurposed into real `unit` / `e2e` / `e2e_p2p` markers, registered in `pyproject.toml`, and a bare `pytest` invocation SHALL default to the fast tier (`-m "not e2e and not e2e_p2p"`).
The suite SHALL NOT adopt `testcontainers`: cwcli itself owns the `docker compose` lifecycle, so the harness drives `cwcli`, not containers directly.

#### Scenario: Unit tier runs with no Docker
- **WHEN** the `unit` tier runs with no reachable Docker daemon (e.g. `DOCKER_HOST` pointed at a dead endpoint)
- **THEN** every unit test still passes, because no unit test opens a Docker connection

#### Scenario: Bare pytest stays fast
- **WHEN** a developer runs `pytest` with no marker selection
- **THEN** only the `unit` tier runs and the `e2e` / `e2e_p2p` tiers are excluded by default

#### Scenario: E2E tier is real Docker
- **WHEN** the `e2e` tier runs
- **THEN** it invokes the real `cwcli` console script (exercising Typer parsing) against a real Docker daemon, not an in-process function call with a fake container

### Requirement: E2E instances are stood up and torn down with real cwcli
The E2E harness SHALL stand up each test instance by running the real `cwcli init` (a genuine `bench init` + `bench new-site` against a freshly pulled image), expose it to tests through a session-scoped fixture, and tear it down by running the real `cwcli rm --yes --volumes`.
Interactive flows SHALL be driven through a real pty with pexpect, awaiting the prompt_toolkit raw-mode readiness marker `ESC[?2004h` before each keystroke.
Non-interactive flows SHALL be driven with flags (`--yes`, `--mariadb-root-username`/`--mariadb-root-password`, `--site`, backup selectors, `--bench`).
The harness SHALL wait on real readiness (health / a `bench` command responding), never fixed sleeps.

#### Scenario: Session fixture builds and tears down a genuine instance
- **WHEN** an E2E module requests the shared instance fixture
- **THEN** the fixture runs `cwcli init` to build a real bench + site, yields the running instance to the tests, and on teardown runs `cwcli rm --yes --volumes`

#### Scenario: Interactive prompt driven via pexpect
- **WHEN** an E2E test drives an interactive confirmation or credential prompt
- **THEN** it awaits the `ESC[?2004h` raw-mode marker before sending each keystroke, so no keystroke races the prompt

#### Scenario: Readiness is waited on, never slept
- **WHEN** the harness needs an instance or bench to be ready
- **THEN** it polls a real readiness signal until ready (or times out with a clear error) rather than sleeping a fixed duration

### Requirement: Hard isolation safety rails and a leaked-resource backstop
The E2E harness SHALL guarantee it can never touch the operator's real cwcli state or instances.
Before any Docker work, a hard rail SHALL refuse to run (fail-closed, non-zero) if `HOME` resolves to the operator's real home directory, or if any E2E project name lacks the `cwe2e-` prefix.
Isolation SHALL be provided by a per-session temporary `HOME` plus the `CWCLI_HOME` override, unique `cwe2e-<runid>-<n>` project and site names, and a port allocator that assigns non-overlapping bases at least 1006 apart (web `{port}..{port+5}`, socketio `{port+1000}..{port+1005}`).
An unconditional teardown backstop SHALL run regardless of test outcome and sweep every `com.docker.compose.project` matching the `cwe2e-` prefix (`docker compose down -v` + volume prune by label + drop the temporary `HOME`), so a crashed or aborted test never leaks containers, volumes, or a project directory.

#### Scenario: Refuse to run against the real home
- **WHEN** the E2E harness is started but `HOME` still resolves to the operator's real home directory
- **THEN** the harness refuses to run any E2E, exits non-zero, and touches no Docker resource

#### Scenario: Refuse a non-prefixed project name
- **WHEN** an E2E attempts to create or operate on a project whose name lacks the `cwe2e-` prefix
- **THEN** the harness refuses, exits non-zero, and creates nothing

#### Scenario: Port allocator prevents collisions between parallel instances
- **WHEN** two E2E instances are stood up in the same run
- **THEN** the allocator gives them port bases at least 1006 apart so their web and socketio ranges never overlap

#### Scenario: Backstop sweeps leaked resources after a crash
- **WHEN** a test aborts mid-run and its `cwcli rm` teardown does not fire
- **THEN** the unconditional backstop still removes every `cwe2e-`-labelled compose project's containers and volumes and drops the temporary `HOME`

### Requirement: Destructive commands assert real side effects in both modes
The E2E tier SHALL cover each destructive command (`rm`, `restore`, `backup`, `update`, `unlock`) by asserting the actual on-disk / in-container side effect, never a mock string match, in BOTH interactive and non-interactive modes.
- `rm` SHALL be verified to remove the project's named volumes and its `~/.cwcli/projects/<name>` directory, AND a verified non-empty database dump SHALL be shown to exist first (the C1 backup gate).
- `backup` SHALL be verified to produce a real, non-empty database dump.
- `restore` SHALL be verified to actually replace the site's data, run `bench migrate`, and restart the instance.
- `update` SHALL be verified to pull/migrate the app(s) and to toggle maintenance mode (on then off).
- `unlock` SHALL be verified to clear the site lock.

#### Scenario: rm removes volumes and dir only after a verified backup
- **WHEN** an E2E runs `cwcli rm cwe2e-<name> --yes --volumes` on a real instance
- **THEN** the test asserts a non-empty DB dump was produced before deletion, then asserts the named volumes and `~/.cwcli/projects/cwe2e-<name>` no longer exist

#### Scenario: backup produces a real non-empty dump
- **WHEN** an E2E runs `cwcli backup` on a real instance
- **THEN** the test asserts a database dump file exists on the host and is non-empty

#### Scenario: restore replaces data, migrates, and restarts
- **WHEN** an E2E restores a backup into a real site
- **THEN** the test asserts the site's data reflects the backup, that `bench migrate` ran, and that the instance was restarted

#### Scenario: update pulls, migrates, and toggles maintenance mode
- **WHEN** an E2E runs `cwcli update` (or `cwcli apps update`) on a real instance
- **THEN** the test asserts the app was pulled/migrated and that maintenance mode was turned on during and off after the update

#### Scenario: unlock clears the site lock
- **WHEN** a site holds a lock and an E2E runs `cwcli unlock`
- **THEN** the test asserts the lock is cleared afterward

#### Scenario: Both modes are exercised per command
- **WHEN** a destructive command's E2E runs
- **THEN** it exercises both the interactive path (pexpect, `ESC[?2004h`) and the non-interactive path (flags), and asserts a non-TTY without the required flag refuses with a non-zero exit

### Requirement: Permanent v14/v15/v16 CI matrix, agnostic-once and sensitive-per-version
CI SHALL run the E2E tier as one parallel job PER Frappe version (14, 15, 16), each on its own GitHub-hosted `ubuntu-latest` runner.
Version-agnostic E2E tests SHALL run only once (on the v16 leg), not three times.
The version-sensitive set SHALL run on every version leg and SHALL include: the MariaDB flag divergence (`≤14 --no-mariadb-socket` vs `15+ --mariadb-user-host-login-scope=%`), the pyenv/nvm install branches (v14 python3.10 + node16 + yarn, v15 python3.12, v16 image defaults), the v14-only `--receive` bare-filename bug, and apps behaviors.

#### Scenario: Version-sensitive test runs on all three legs
- **WHEN** the E2E matrix runs
- **THEN** the MariaDB-flag, pyenv/nvm-branch, `--receive` bare-filename, and apps tests each run on the v14, v15, and v16 legs

#### Scenario: Version-agnostic test runs once
- **WHEN** the E2E matrix runs
- **THEN** a version-agnostic E2E runs only on the v16 leg and is skipped on the v14 and v15 legs

#### Scenario: v14-only receive bug is caught on v14
- **WHEN** the `--receive` E2E runs on the v14 leg
- **THEN** it exercises the bare-filename path that only reproduces on v14 (v15's alternative-directory fallback masks it) and asserts the restore succeeds

### Requirement: E2E jobs run on a host runner with an explicit runtime budget
Each E2E job SHALL run directly on GitHub-hosted `ubuntu-latest` with no `container:`, so `docker` and `docker compose` and the daemon are reachable.
`uv` SHALL be installed via `astral-sh/setup-uv` (the E2E job cannot use the uv container image).
Each E2E job SHALL run the full real `cwcli init` each run (no pre-built base image in this change).
Each E2E job SHALL set an explicit `timeout-minutes` (starting at ~45, provisional) so a hung `bench init` fails fast rather than burning toward GitHub's 6h cap.
Disk pressure SHALL be managed within a leg by pruning Docker images/volumes between phases, respecting the 4 vCPU / 16 GB / 14 GB hosted-runner ceiling.

#### Scenario: E2E job reaches the Docker daemon
- **WHEN** an E2E job starts on `ubuntu-latest`
- **THEN** it runs on the host (no `container:`), installs uv via `astral-sh/setup-uv`, and `docker` / `docker compose` reach the preinstalled daemon

#### Scenario: A hung init is bounded by the job timeout
- **WHEN** a `bench init` hangs during an E2E job
- **THEN** the job's `timeout-minutes` ceiling fails it fast rather than running toward the 6h cap

### Requirement: Docker-not-available skips loudly, never a silent green
The E2E harness SHALL detect an unavailable Docker daemon (`docker info` / `client.ping()`) and SKIP loudly - the E2E result SHALL make the missing-Docker condition obvious as an infrastructure failure, never report a silent green pass as if the tests had run.

#### Scenario: No Docker daemon is an obvious skip
- **WHEN** the E2E tier runs on a machine with no reachable Docker daemon
- **THEN** it emits a loud skip that clearly signals "Docker unavailable - infrastructure problem" rather than passing silently

### Requirement: Track latest upstream with a distinct failure signal and authenticated pulls
The E2E SHALL track latest upstream (the `frappe_docker@main` compose file and the latest `frappe/bench` tag), matching real user behavior, so it catches upstream breakage of a real `cwcli init`.
Upstream-caused failures (compose fetch, image pull, or bench-tag lookup) SHALL surface as a DISTINCT "upstream pull/compose fetch failed" signal, separable from a real assertion failure, so a red gate is never ambiguous.
Docker Hub anonymous pull rate limits SHALL be mitigated with authenticated pulls.

#### Scenario: Upstream fetch failure is distinguishable from a test failure
- **WHEN** an upstream compose fetch or image pull fails during an E2E run
- **THEN** the failure is annotated as an upstream/infrastructure failure, visibly distinct from a cwcli assertion failure

#### Scenario: Authenticated pulls dodge anonymous rate limits
- **WHEN** an E2E job pulls the multi-GB `frappe/bench` image
- **THEN** it authenticates to Docker Hub so the pull is not subject to the anonymous per-IP rate limit

### Requirement: P2P sendme is a single gated loopback E2E
The `restore --send` / `--receive` P2P path SHALL be covered by a SINGLE loopback E2E (send + receive on one runner), marked `e2e_p2p`, and SHALL NOT run on every version leg.

#### Scenario: P2P runs once, gated, off the version matrix
- **WHEN** the E2E suite runs
- **THEN** the P2P send+receive loopback test runs on a single gated leg under the `e2e_p2p` marker and is excluded from the per-version `e2e` matrix legs

### Requirement: Two-tier CI gating
CI SHALL run the fast `unit` tier on every push and pull request (kept in the existing uv container).
The `e2e` matrix SHALL be required on pull requests into protected branches and SHALL also be runnable on-demand via a PR label.
The proposal SHALL note that branch protection must be enabled on `develop`/`master` for a "required" E2E gate to actually block a merge (it is not enabled today).

#### Scenario: Unit tier gates every push
- **WHEN** any push or PR is made
- **THEN** the fast `unit` job runs as an always-required gate in the uv container

#### Scenario: E2E gates protected-branch PRs and runs on-demand via label
- **WHEN** a PR targets a protected branch, OR a PR carries the E2E label
- **THEN** the `e2e` matrix runs; on a protected-branch PR it is a required gate (contingent on branch protection being enabled)

### Requirement: Parallel-run transition off the mock suite
The migration SHALL be parallel-run, not a big bang.
The current mock-based suite SHALL stay green while the E2E layer is built.
Each command's brittle container-mock behavior tests SHALL be retired only as that command's real E2E lands and proves out.
The mock-free pure-logic tests SHALL be kept permanently.

#### Scenario: Mock suite stays green during the transition
- **WHEN** the E2E layer is being built but a given command's E2E has not yet landed
- **THEN** that command's existing mock tests remain in place and green

#### Scenario: A command's mock tests are retired only after its E2E proves out
- **WHEN** a command's real E2E has landed and proven out
- **THEN** that command's container-mock behavior tests are retired, while the pure-logic unit tests are kept

### Requirement: Docker E2E is Linux-only; OS-specific code stays unit-tested
The real-Docker E2E SHALL be Linux-only.
Windows/macOS-specific code (`port_utils`, `startup`, `vscode_utils`, named-pipe Docker-error handling) SHALL remain covered by the mock-free / mock-based unit tier rather than the Docker E2E.

#### Scenario: OS-specific paths are not in the Docker E2E
- **WHEN** the Docker E2E matrix runs
- **THEN** it runs only on Linux, and the Windows/macOS-specific code paths are exercised by the unit tier instead
