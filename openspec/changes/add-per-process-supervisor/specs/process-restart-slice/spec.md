## ADDED Requirements

### Requirement: core.restart_process restarts one program while siblings keep running

The system SHALL provide `core.restart_process(project, label, *, bench=None, bench_path=None) -> Result[ProcessRestartOutcome]` that restarts the single supervised program named by `label` (via `supervisorctl restart <label>`) while every sibling program keeps running.
It SHALL be UI-pure: it prints nothing, prompts nothing, and raises `typer.Exit` never; it returns a typed `Result` or raises `CwcliError`.
The returned `ProcessRestartOutcome` SHALL carry `project`, `bench_path`, `label`, `old_pid` (discovered before the restart, `None` if the program was down), `new_pid` (discovered after), and `supervisor_state` (the program's supervisord state after the restart), and SHALL leak no live Docker object across the boundary.

#### Scenario: One program is restarted, siblings untouched

- **WHEN** `core.restart_process(project, "worker")` runs against a bench where web, socketio, worker, and schedule are all running
- **THEN** only the worker is cycled (a new PID), the outcome reports `old_pid`/`new_pid`/`supervisor_state`, and web, socketio, and schedule keep their original PIDs

#### Scenario: Restarting a down program brings it up

- **WHEN** `core.restart_process(project, "worker")` runs while the worker is `FATAL`/down
- **THEN** the outcome reports `old_pid=None`, a `new_pid`, and a `supervisor_state` reflecting the restart (e.g. `RUNNING` or `STARTING`)

### Requirement: An unknown or ambiguous --process label returns NEEDS_CHOICE listing the valid labels

When `label` does not name exactly one supervised program, `core.restart_process` SHALL return a `NEEDS_CHOICE` whose choices are the valid program labels for the bench (from the expected-label set / `supervisorctl status`), NEVER a prompt.
Each frontend SHALL resolve it its own way: the interactive CLI presents the labels, and `cwcli axi restart` renders a usage error naming the unknown label and listing the valid ones.

#### Scenario: Unknown process name lists valid labels

- **WHEN** `core.restart_process(project, "wroker")` (a typo) runs
- **THEN** it returns `NEEDS_CHOICE` whose choices are the valid labels (web, socketio, worker, schedule, watch, redis_cache, redis_queue as the bench defines), and does not restart anything

#### Scenario: axi renders the unknown label as a usage error

- **WHEN** `cwcli axi restart <project> --process wroker` runs
- **THEN** it emits a usage error naming the unknown label and listing the valid ones, exits non-zero, and never prompts

### Requirement: Multi-bench selection reuses the shared bench resolver

`core.restart_process` SHALL resolve which bench to act on via the shared `resolvers.resolve_bench`, returning its `select_bench` `NEEDS_CHOICE` on a multi-bench instance with no selector - the same selector `start` and `status` use, so all three agree on the bench.
An explicit `--bench <sel>` (index or label) or `bench_path` SHALL be honored verbatim.

#### Scenario: Multi-bench with no selector returns NEEDS_CHOICE

- **WHEN** `core.restart_process(project, "worker")` runs on a multi-bench instance with no `bench`/`bench_path`
- **THEN** it returns the `select_bench` `NEEDS_CHOICE` (interactive CLI prompts, `cwcli axi restart` returns a usage error naming `--bench`), and restarts nothing until the bench is chosen

#### Scenario: An explicit --bench selects the bench

- **WHEN** `core.restart_process(project, "worker", bench="<index-or-label>")` runs on a multi-bench instance
- **THEN** it acts on the selected bench's worker only

### Requirement: cwcli restart scopes to one program with --process, whole-stack otherwise

`cwcli restart <project> [--process <label>] [--bench <sel>]` SHALL restart the single program named by `--process` when given (via `core.restart_process`, leaving siblings running), and SHALL preserve today's whole-stack restart (stop + start the containers, relaunching the supervisor) when `--process` is omitted.
`--bench` SHALL select the bench on a multi-bench instance via the shared selector.

#### Scenario: --process restarts one program

- **WHEN** `cwcli restart <project> --process web` runs
- **THEN** only the web program is restarted and the siblings keep running

#### Scenario: No --process restarts the whole stack

- **WHEN** `cwcli restart <project>` runs with no `--process`
- **THEN** it performs today's whole-stack restart (containers stopped and started, the supervisor relaunched), unchanged user-facing behavior

### Requirement: cwcli axi restart is a one-shot single-program mutation emitting one TOON document

`cwcli axi restart <project> --process <label>` SHALL call `core.restart_process` and emit the `ProcessRestartOutcome` as one TOON document on stdout, with no progress text on stdout and no prompt.
`--process` SHALL be required on the axi verb; omitting it SHALL be a usage error.
There SHALL be no `axi restart --watch` (the one-TOON-document-per-invocation contract); auto-heal is supervisord config state, not a live loop.

#### Scenario: axi restart emits one TOON outcome

- **WHEN** `cwcli axi restart <project> --process worker` runs on a started bench
- **THEN** it restarts the worker and emits the `ProcessRestartOutcome` as one TOON document on stdout, exit 0, with no progress text on stdout

#### Scenario: axi restart requires --process

- **WHEN** `cwcli axi restart <project>` runs with no `--process`
- **THEN** it emits a usage error naming `--process` and exits non-zero, restarting nothing
