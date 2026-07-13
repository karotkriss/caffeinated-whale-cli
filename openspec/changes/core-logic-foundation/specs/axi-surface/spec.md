## ADDED Requirements

### Requirement: cwcli axi namespace over the shared core

The system SHALL provide a `cwcli axi` sub-namespace (a Typer sub-app registered in `main.py` like the existing `add_typer(apps_cmd.app, name="apps")`) whose verbs call the SAME core functions the human CLI calls, add no business logic, and never prompt or call `questionary`.
Each `axi` verb SHALL parse its flags, invoke the core function, serialize the returned DTO to stdout, and map the result `status` (or a raised `CwcliError.kind`) to a process exit code.

#### Scenario: axi verb reuses the core

- **WHEN** `cwcli axi backup <project> --site <site>` runs
- **THEN** it calls the same `core.backup` the human `cwcli backup` calls and performs the same operation, differing only in how it renders output and resolves choices

#### Scenario: axi never prompts

- **WHEN** any `cwcli axi` verb reaches a decision it cannot resolve from its flags
- **THEN** it emits a structured error on stdout and exits non-zero, and never blocks on an interactive prompt

### Requirement: TOON output at the stdout boundary, JSON internal

The system SHALL provide one shared, dependency-free serializer that emits TOON on stdout for every `axi` verb, converting the core DTO to plain data internally via `dataclasses.asdict` (JSON-shaped) and encoding TOON only at the output boundary.
Structured output (data AND errors) SHALL go to stdout; progress and diagnostics SHALL go to stderr; the serializer SHALL add no new runtime dependency.

#### Scenario: Successful verb emits TOON on stdout

- **WHEN** `cwcli axi backup <project> --site <site>` succeeds
- **THEN** stdout carries a TOON document describing the `BackupOutcome`, stderr carries any progress, and the process exits 0

#### Scenario: A WARNING outcome still exits 0

- **WHEN** a core function returns `Result(status=WARNING, data=<outcome>, warnings=[...])` (a completed operation with a non-fatal note, e.g. a partial fan-out or a no-op)
- **THEN** the axi verb renders the outcome plus the warning(s) as usual, and the process still exits 0 - `WARNING` is a success status, not an error

#### Scenario: No progress text on stdout

- **WHEN** any `cwcli axi` verb runs
- **THEN** stdout contains only the structured TOON document (never a progress or status line an agent could misread as data)

### Requirement: Content-first cwcli axi home

The system SHALL make bare `cwcli axi` (no verb) a content-first home that prints, in order: `bin:` (the absolute path of the current executable with the home directory collapsed to `~`), a one-sentence `description:`, live state (the current Frappe instances, produced by the existing `ls` core producer `_list_instances`), and a `help[N]:` block of a few logical next-step commands.
The home SHALL reuse the same boundary-clean instance DTO the `ls` command already produces, and SHALL state a definitive empty state when there are no instances.

#### Scenario: Home shows live instances

- **WHEN** a user runs `cwcli axi` with Frappe instances present
- **THEN** it prints `bin:`, `description:`, the live instances as a TOON collection, and a `help[N]:` block of next steps

#### Scenario: Home definitive empty state

- **WHEN** a user runs `cwcli axi` with no Frappe instances present
- **THEN** it states explicitly that there are zero instances (a successful empty result), not an ambiguous blank

### Requirement: Structured errors and needs-choice as usage errors, exit codes 0/1/2

Every `cwcli axi` verb SHALL emit errors as structured output on stdout (never a leaked docker-py stack trace), using exit code 0 for success (including no-ops), 1 for an error, and 2 for a usage error.
A raised `CwcliError` SHALL be rendered as `error: <message>` plus `help: <hint>` where a hint exists, exiting 2 for `ErrorKind.USAGE` and 1 for every other kind.
A `NEEDS_CHOICE` result SHALL be rendered as a usage error naming the exact flag the agent must pass to resolve it, exiting 2.

#### Scenario: Typed error rendered on stdout

- **WHEN** `cwcli axi backup <project>` fails because the project is not found
- **THEN** stdout carries `error: ...` (and a `help:` line if present), no stack trace leaks, and the process exits 1

#### Scenario: Needs-choice becomes a flag-naming usage error

- **WHEN** `cwcli axi backup <project>` runs on a multi-bench project with no `--bench`
- **THEN** stdout carries a usage error naming `--bench <index|label>` (with the available benches), and the process exits 2

#### Scenario: Usage error exit code

- **WHEN** an `axi` verb is given mutually-exclusive or missing required flags (a `CwcliError(kind=USAGE)`)
- **THEN** the process exits 2
