## ADDED Requirements

### Requirement: core.stop returns a typed StopOutcome and never prints

The system SHALL provide `core.stop(project) -> Result[StopOutcome]` that owns resolving a project's containers and stopping the running ones.
`StopOutcome` SHALL be a frozen dataclass carrying `project`, `stopped` (the count this call stopped), `already_stopped`, and `containers` (the NAMES of the containers stopped).
`core.stop` SHALL raise `CwcliError(kind=NOT_FOUND)` when the project does not exist, replacing `_stop_project`'s `None` sentinel, and SHALL return `StopOutcome(already_stopped=True, stopped=0)` when the project exists but nothing is running.
`core.stop` SHALL NOT print, prompt, or call `typer.Exit`, and SHALL NOT return live Docker objects.

#### Scenario: Stopping a running project reports what it stopped

- **WHEN** `core.stop` runs against a project with running containers
- **THEN** it stops them and returns `Result(OK, StopOutcome(stopped=N, already_stopped=False, containers=[names]))` with nothing printed

#### Scenario: An already-stopped project is a success, not a failure

- **WHEN** `core.stop` runs against a project whose containers exist but none are running
- **THEN** it returns `Result(OK, StopOutcome(stopped=0, already_stopped=True))` and does NOT raise

#### Scenario: A missing project raises a typed error instead of returning a sentinel

- **WHEN** `core.stop` runs against a project with no containers
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` and stops nothing

#### Scenario: No live Docker object crosses the boundary

- **WHEN** `core.stop` returns a `StopOutcome`
- **THEN** `containers` holds container names as strings, and the DTO is serializable without touching the Docker API

### Requirement: cwcli stop is a thin frontend over core.stop

`commands/stop.py` SHALL call `core.stop` and SHALL retain only presentation and frontend parsing: the typer signature, the variadic project-name list, stdin piping, the trailing-verbose-flag recovery, the spinner, and `rich` output.
The command's behavior, messages, and exit codes SHALL be preserved, including the honest per-project exit code where a missing project among several still fails the command after the rest are processed.

#### Scenario: The multi-project fan-out and stdin piping stay in the frontend

- **WHEN** `cwcli stop` is given several project names, or names piped on stdin
- **THEN** the frontend iterates them and calls `core.stop` once per project, and the core knows nothing about the fan-out

#### Scenario: A missing project among several preserves the honest exit code

- **WHEN** `cwcli stop a b c` runs and `b` does not exist
- **THEN** `a` and `c` are still processed, the failure is reported, and the command exits non-zero

#### Scenario: The already-stopped message is preserved

- **WHEN** `cwcli stop` runs against a project that is already stopped
- **THEN** the frontend reports it as already stopped and the command succeeds

### Requirement: The in-repo callers of _stop_project use the core function

The system SHALL re-point every in-repo caller of `commands/stop.py:_stop_project` at `core.stop`: `commands/restart.py`, `commands/start.py`, `commands/rm.py`, and `commands/axi.py`.
No caller SHALL depend on a UI-coupled stop helper.
Each caller SHALL handle the typed `NOT_FOUND` error in place of the `None` sentinel, preserving its existing behavior.

#### Scenario: rm's backup gate still fails closed

- **WHEN** `rm`'s stop step encounters a project that does not exist
- **THEN** it handles the typed `NOT_FOUND` with the same fail-closed behavior the `None` sentinel produced, keeping data and exiting non-zero

#### Scenario: start's port-conflict resolution is unchanged

- **WHEN** `cwcli start` stops a conflicting Frappe project during port-conflict resolution
- **THEN** the behavior is unchanged, now via `core.stop`

### Requirement: The axi surface cannot print to stdout during port-conflict resolution

`cwcli axi start --yes` SHALL stop conflicting Frappe projects through `core.stop`, which cannot print.
Stdout SHALL carry exactly one TOON document regardless of which branch the stop takes, including when a conflicting project is concurrently removed or stopped between conflict detection and the stop call.

#### Scenario: A teardown race cannot corrupt the TOON document

- **WHEN** `cwcli axi start --yes` resolves a port conflict and a conflicting project is removed or stops between detection and the stop call
- **THEN** stdout still carries exactly one parseable TOON document, with no rich-markup error or status text emitted onto it

### Requirement: cwcli axi stop completes the lifecycle verb family

The system SHALL provide `cwcli axi stop <project>` on the existing `cwcli axi` serializer and exit-code mapper, emitting the `StopOutcome` as one TOON document on stdout.
It SHALL NOT prompt.
A missing project SHALL be a structured error with a non-zero exit; an already-stopped project SHALL be a definitive success state, not an error.

#### Scenario: Stopping reports the outcome as TOON

- **WHEN** `cwcli axi stop <project>` stops a running project
- **THEN** it emits one TOON document carrying the stopped count and container names, and exits 0

#### Scenario: Already stopped is a definitive success state

- **WHEN** `cwcli axi stop <project>` runs against an already-stopped project
- **THEN** it emits a TOON document stating that definitively and exits 0, so a repeated stop is idempotent for an agent

#### Scenario: A missing project is a structured error

- **WHEN** `cwcli axi stop <project>` runs against a project that does not exist
- **THEN** it emits a structured error on stdout and exits non-zero, never a raw traceback
