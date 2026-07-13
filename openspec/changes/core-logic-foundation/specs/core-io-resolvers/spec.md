## ADDED Requirements

### Requirement: Core Docker container accessor with typed errors and no leaked objects

The system SHALL provide a core accessor that resolves a project's frappe container to a handle usable for exec, raising `CwcliError` (`NOT_FOUND` when the project or its frappe service is absent, `DOCKER` when the daemon is unreachable) instead of printing and calling `typer.Exit`.
No live Docker object resolved by this accessor SHALL appear in any DTO returned across a `core.<verb>` boundary; the handle stays internal to the core function that uses it.
The existing print-and-`typer.Exit` accessor (`docker_utils.get_frappe_container`) SHALL remain available to CLI callers as a thin wrapper defined in terms of the core accessor (call core, map the typed error to the current message and exit code), so there is a single resolution implementation.

#### Scenario: Project not found raises a typed error

- **WHEN** the core accessor is asked for a project's frappe container and no such container exists
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` and does NOT print or call `typer.Exit`

#### Scenario: Docker daemon unreachable raises a typed error

- **WHEN** the core accessor cannot reach the Docker daemon
- **THEN** it raises `CwcliError(kind=DOCKER)` and does NOT print or call `typer.Exit`

#### Scenario: CLI wrapper preserves current behavior

- **WHEN** a CLI command calls the existing `get_frappe_container` wrapper and the project is not found
- **THEN** the user sees the same error message and non-zero exit as before this change

### Requirement: Pure ensure-containers-running core resolver

The system SHALL split `ensure_containers_running` so a pure core resolver reports the container state without prompting or printing: it returns "running" when the frappe container is up, a `NEEDS_CHOICE` result carrying a `confirm_start` choice when the container is stopped and auto-start was not requested, and it treats an explicit auto-start request as proceeding.
When the container is stopped and the resolver is told not to offer a choice (the non-interactive/no-auto-start case), it SHALL signal a typed `NOT_RUNNING` condition rather than hang or prompt.
The `questionary.confirm` prompt, the warning/print text, and the `raise typer.Exit` on decline (`utils.py:89-129`) SHALL move into a thin CLI wrapper that resolves the `confirm_start` choice and preserves today's exit codes and messages.

#### Scenario: Container already running

- **WHEN** the core resolver checks a project whose frappe container is running
- **THEN** it reports running and no choice or error is produced

#### Scenario: Stopped container offers a start choice

- **WHEN** the core resolver checks a stopped project without an auto-start directive
- **THEN** it returns a `NEEDS_CHOICE` result whose `choice.kind` is `confirm_start`, and does NOT prompt or start anything

#### Scenario: CLI wrapper prompts and preserves the contract

- **WHEN** the CLI wrapper receives the `confirm_start` choice on an interactive TTY and the user declines (or the session is a non-TTY without `--yes`)
- **THEN** it exits non-zero with the same message as today, and on acceptance it starts the containers and proceeds

### Requirement: Pure resolve-bench-path core resolver

The system SHALL split `resolve_bench_path` so a pure core resolver returns the resolved bench path, a `NEEDS_CHOICE` result carrying a `select_bench` choice (with the bench list as `options`) when a multi-bench project has no selector, a `CwcliError(kind=USAGE)` when `--bench` and `--path` are both given, and `None` only in the no-cache fall-back case.
The bench-list printing and the `raise typer.Exit` on ambiguity (`utils.py:206-221`) SHALL move into the thin CLI wrapper, which resolves the `select_bench` choice (prompt or, for the `on_ambiguous="first"` callers, pick the first with a note) and preserves today's messages and exit codes.

#### Scenario: Single bench resolves directly

- **WHEN** the core resolver runs on a single-bench project with no selector
- **THEN** it returns that bench's path with no choice or error

#### Scenario: Multi-bench with no selector returns a choice

- **WHEN** the core resolver runs on a multi-bench project with neither `--bench` nor `--path`
- **THEN** it returns a `NEEDS_CHOICE` result whose `choice.kind` is `select_bench` and whose `options` list the benches, and does NOT print or exit

#### Scenario: --bench and --path together is a usage error

- **WHEN** the core resolver is given both a bench selector and a path override
- **THEN** it raises `CwcliError(kind=USAGE)` (which the axi frontend maps to exit 2)

### Requirement: confirm_or_exit stays CLI-only

`confirm_or_exit` (`utils.py:224-259`) SHALL remain a CLI-frontend helper and SHALL NOT be imported or called by any module under `core/`.
The destructive-confirmation decision reaches the core only as an explicit param (for example an already-confirmed flag) or a resolved `confirm_destructive` choice, never as an in-core prompt.

#### Scenario: Core never calls confirm_or_exit

- **WHEN** any module under `src/caffeinated_whale_cli/core/` is inspected
- **THEN** it does not import or call `confirm_or_exit`
