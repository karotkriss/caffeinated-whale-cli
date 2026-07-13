## ADDED Requirements

### Requirement: core.start returns a typed StartOutcome and is idempotent

The system SHALL provide `core.start(project, *, bench=None, bench_path=None, auto_start=False, ...) -> Result[StartOutcome]` that owns starting a project's stopped containers, resolving which bench to run, launching `bench start` detached, capturing its stream to the persisted bounded log, writing the supervisor marker, and discovering the launched process set.
`core.start` SHALL be idempotent: it detects an already-running bench by DISCOVERED supervisor PID keyed to the resolved bench path (NOT `pkill -f 'bench start'`), and if the bench is already running it no-ops and returns `Result(OK, StartOutcome(already_running=True, ...))` without spawning a second honcho.
`StartOutcome` SHALL be a dataclass carrying `project`, `container`, `bench_path`, `supervisor`, `log_path`, `already_running`, and `processes` (a list of `ProcessLaunch(label, pid)`).
`core.start` SHALL NOT print, prompt, or call `typer.Exit`: it returns `select_bench` `NEEDS_CHOICE` on multi-bench ambiguity with no selector, raises `CwcliError` for hard failures (`NOT_FOUND` for a missing project/frappe service, `DOCKER` for daemon errors), and does NOT run port-conflict prompting (a documented CLI-frontend precondition).

#### Scenario: Start launches and returns the process set

- **WHEN** `core.start` runs against a stopped or not-yet-benched project with a resolvable single bench
- **THEN** it starts the containers, launches `bench start`, writes the marker, and returns `Result(OK, StartOutcome(already_running=False, processes=[...]))` with no printing

#### Scenario: Already-running is a clean no-op (double-start fixed)

- **WHEN** `core.start` runs against a bench whose honcho supervisor is already discovered running at the resolved bench path
- **THEN** it does NOT launch a second honcho and returns `Result(OK, StartOutcome(already_running=True, ...))` reflecting the live process set

#### Scenario: Multi-bench with no selector

- **WHEN** `core.start` runs on a multi-bench project with neither `bench` nor `bench_path`
- **THEN** it returns a `select_bench` `NEEDS_CHOICE` result and starts nothing

#### Scenario: Explicit bench_path is used verbatim (post-restore restart preserved)

- **WHEN** `core.start` is given an explicit `bench_path` (as the post-restore restart passes, to restart the SAME bench it migrated)
- **THEN** it uses that path verbatim, skipping bench resolution, and never falls back to the first sorted bench

#### Scenario: Missing project raises a typed error

- **WHEN** `core.start` runs against a project with no containers
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` and starts nothing

### Requirement: core.status returns a typed StatusReport with a pre-computed overall

The system SHALL provide `core.status(project, *, bench=None, bench_path=None) -> Result[StatusReport]` that reports real per-process health for the resolved bench from the shared discovery substrate, keeps the web HTTP probe as one field, and pre-computes an `overall` aggregate.
`StatusReport` SHALL carry `project`, `container_running`, `supervisor_up`, `web_http_code`, `processes` (a list of `ProcessHealth(label, up, pid, uptime_s, cpu_pct, rss_kb)`), and `overall` (`offline`/`online`/`running`/`degraded`).
`core.status` SHALL treat an absent project, absent frappe service, or stopped container as `offline` (returning a `StatusReport`, NOT raising), preserving today's "offline, exit 0" contract; only an unreachable Docker daemon raises `CwcliError(DOCKER)`.
`core.status` SHALL be a one-shot snapshot and SHALL NOT print, prompt, or call `typer.Exit`; on multi-bench ambiguity with no selector it returns a `select_bench` `NEEDS_CHOICE`, sharing the SAME selector as `core.start`.

#### Scenario: Running reports full per-process health

- **WHEN** `core.status` runs against a bench whose marker is present, honcho and all expected labels are up, and the web probe answers
- **THEN** it returns `overall="running"` with a `processes` list carrying per-label up/uptime/CPU/RSS and the web HTTP code

#### Scenario: Offline for absent or stopped containers

- **WHEN** `core.status` runs with the project's containers absent or stopped
- **THEN** it returns `Result(OK, StatusReport(overall="offline", container_running=False))` and does NOT raise

#### Scenario: Online when containers up but never started

- **WHEN** `core.status` runs on a running container with no supervisor marker
- **THEN** it returns `overall="online"` (bench never started), distinct from `running`

#### Scenario: Degraded when started but a process or the web port is down

- **WHEN** `core.status` runs with the marker present but honcho down, or honcho up with an expected label down or the web probe not answering
- **THEN** it returns `overall="degraded"`, honestly surfacing the partial/dead state

#### Scenario: start and status agree on the bench

- **WHEN** `core.start` and `core.status` are called on the same multi-bench project with the same `--bench` selector
- **THEN** both resolve to the SAME bench (one shared selector), so status reports the bench start acted on

### Requirement: Reseated interactive cwcli start and cwcli status on the core

The system SHALL reseat `cwcli start` and `cwcli status` as thin frontends over `core.start` / `core.status`.
The `cwcli start` frontend SHALL keep its variadic multi-project loop, stdin piping, trailing-flag recovery, `_check_port_conflicts` host-side pre-step (prompting stays here), spinner, and `rich` output, resolving a returned `select_bench` choice via the CLI wrapper (prompt then re-invoke), and preserving honest per-project exit codes.
The multi-bench no-selector behavior SHALL change from the pre-migration `first-with-note` to the family's prompt (interactive) / error (non-TTY without `--bench`) - a deliberate change (D5) floored by the green E2E net.
The `cwcli status` frontend SHALL render the `StatusReport` with the `overall` aggregate as its primary line (now able to report `degraded`) plus per-process detail, exiting 0 across the lifecycle states.
The stale "runs bench start in tmux" docstrings SHALL be corrected.

#### Scenario: cwcli start is idempotent on the CLI

- **WHEN** `cwcli start <project>` is run twice against a running single-bench instance
- **THEN** the second run reports "already running" and exits 0 without spawning a second stack

#### Scenario: cwcli start prompts on multi-bench interactively

- **WHEN** `cwcli start <project>` runs interactively on a multi-bench project with no `--bench`
- **THEN** it prompts which bench (the shared `select_bench` prompt), not the old first-with-note default

#### Scenario: cwcli start refuses ambiguous multi-bench non-interactively

- **WHEN** `cwcli start <project>` runs from a non-TTY on a multi-bench project with no `--bench`
- **THEN** it errors and exits non-zero, never silently starting the first bench

#### Scenario: cwcli status shows per-process health

- **WHEN** `cwcli status <project>` runs against a running instance
- **THEN** it prints the `overall` aggregate plus per-process up/uptime/CPU/RSS, exiting 0

### Requirement: cwcli axi start and cwcli axi status verbs

The system SHALL add `cwcli axi start <project> [--bench] [--yes]` and `cwcli axi status <project>` on the existing `cwcli axi` serializer/exit-mapper, each ~15-25 lines calling the same core function.
`cwcli axi start` SHALL emit the `StartOutcome` as TOON on success (idempotent no-op included), map a `select_bench` `NEEDS_CHOICE` to a usage error naming `--bench` (exit 2), surface an unresolved port conflict as a `CONFLICT` structured error naming `--yes` (with `--yes` auto-resolving conflicting Frappe projects), and never prompt.
`cwcli axi status` SHALL emit the `StatusReport` as TOON with the pre-computed `overall` up front and a definitive offline/empty state, exit 0.
Both SHALL map a raised `CwcliError` to the shared exit-code mapping (`USAGE` -> 2, else 1) and carry only TOON on stdout (progress/diagnostics to stderr).

#### Scenario: axi start emits the outcome as TOON

- **WHEN** `cwcli axi start <project> --yes` runs against a startable single-bench instance
- **THEN** stdout is the `StartOutcome` as TOON (including `already_running`), exit 0, with no interactive prompt

#### Scenario: axi start names the flag on multi-bench ambiguity

- **WHEN** `cwcli axi start <project>` runs on a multi-bench project with no `--bench`
- **THEN** it emits a structured usage error naming `--bench <index|label>` and exits 2

#### Scenario: axi start surfaces a port conflict without prompting

- **WHEN** `cwcli axi start <project>` (no `--yes`) hits an unresolved port conflict
- **THEN** it emits a `CONFLICT` structured error naming `--yes` and exits 1, never prompting

#### Scenario: axi status leads with the overall aggregate

- **WHEN** `cwcli axi status <project>` runs
- **THEN** stdout is TOON with the `overall` aggregate first, then the per-process table, exit 0 - a definitive offline state when the instance is down

### Requirement: The start/status E2E net stays green and new behavior is covered

The pre-existing `start-status-e2e` net (from `add-start-status-e2e-net`) SHALL stay green UNCHANGED under the migration (refactor-under-green for the preserved invariants).
This change SHALL add its own real-Docker E2E for the NEW behavior - per-process health fields, the `degraded` aggregate, the idempotent single-supervisor no-op, the relocated bounded log, and the multi-bench prompt/error - in both interactive and non-interactive modes.
Per the captain standard, the real-instance E2E SHALL be re-run after the no-mistakes run and after any review fixes.

#### Scenario: Preserved invariants stay green

- **WHEN** the `start-status-e2e` suite runs against the reseated commands
- **THEN** it passes without modification to the test files (the invariants are preserved)

#### Scenario: New behavior is proven end to end

- **WHEN** the new-behavior E2E runs against a real instance
- **THEN** it asserts real per-process health, an idempotent no-op that does not spawn a second supervisor, the log persisted under the bench `logs/` dir, and multi-bench prompt/error - in both modes
