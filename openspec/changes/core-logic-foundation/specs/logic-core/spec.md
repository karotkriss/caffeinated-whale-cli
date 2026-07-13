## ADDED Requirements

### Requirement: UI-pure logic core

The system SHALL provide a logic-core package (`src/caffeinated_whale_cli/core/`) whose functions own their I/O (Docker, subprocess, filesystem, the peewee registry) but carry NO presentation and NO interaction.
Core modules SHALL NOT import `rich` or `questionary`, SHALL NOT call `typer.Exit`, and SHALL NOT prompt.
Given its inputs a core function SHALL be deterministic and testable with faked I/O.

#### Scenario: Core module imports no UI

- **WHEN** any module under `src/caffeinated_whale_cli/core/` is inspected
- **THEN** it imports neither `rich` nor `questionary`, and contains no `typer.Exit` call or interactive prompt

#### Scenario: Core is driven entirely by explicit params

- **WHEN** a core function needs a decision (which bench, whether to start a stopped container, a destructive confirmation)
- **THEN** it either reads that decision from an explicit parameter, or returns a needs-choice result, or raises a typed error - it never blocks on input

### Requirement: Serializable typed return envelope

Core functions SHALL return a light common envelope `Result[T]`, implemented with stdlib `dataclasses` (`@dataclass(frozen=True, slots=True)`), carrying a `status` (a `Status` enum of `OK` / `WARNING` / `NEEDS_CHOICE`), an optional typed `data` payload (`T`, the command's outcome DTO), a `warnings` list of `Message` DTOs (`code`, `text`, optional `detail`), and an optional `choice`.
Every DTO reachable from `Result` SHALL be serializable to plain nested data via `dataclasses.asdict`, and SHALL NOT hold any live Docker object or other non-serializable handle.
The envelope SHALL NOT carry an `errors` list; hard errors are raised (see the typed-error requirement), and soft, partial, or expected outcomes ride in `data` + `warnings` with a `WARNING` or `OK` status.

#### Scenario: Successful outcome

- **WHEN** a core function completes its operation
- **THEN** it returns `Result(status=OK, data=<outcome DTO>)`, and `dataclasses.asdict(result.data)` yields plain nested data with no live Docker object

#### Scenario: Partial or soft outcome

- **WHEN** a core function completes but with non-fatal issues (for example a partial multi-target fan-out, or a no-op because the desired state already holds)
- **THEN** it returns `Result(status=WARNING, data=<outcome DTO>, warnings=[Message(...)])` and does NOT raise

### Requirement: Non-interactive input model via needs-choice

When a core function cannot resolve a decision from its explicit params, it SHALL return `Result(status=NEEDS_CHOICE, choice=Choice(...))` without performing the operation.
The `Choice` DTO SHALL name the frontend-fillable core parameter (`param`), a machine `kind`, a human `prompt`, and, for selections, the available `options` (and an optional `default`).
The core SHALL be re-invokable with the chosen value supplied as that explicit `param`, at which point it proceeds without any further prompt for that decision.

#### Scenario: Ambiguous decision returns a choice

- **WHEN** a core function reaches a fork it cannot resolve from its params (for example a multi-bench project with no bench selected)
- **THEN** it returns `Result(status=NEEDS_CHOICE, choice=Choice(kind=..., param=..., prompt=..., options=[...]))` and does NOT act

#### Scenario: Re-invocation with the choice resolved

- **WHEN** the same core function is called again with the `choice.param` now supplied explicitly
- **THEN** it proceeds and returns an `OK`/`WARNING` result without emitting the same `NEEDS_CHOICE` again

### Requirement: Typed error hierarchy for hard failures

The system SHALL provide a typed error `CwcliError` (carrying an `ErrorKind`, a stable `code`, a `message`, and an optional `hint`) that core functions raise when they genuinely cannot proceed.
`ErrorKind` SHALL be a closed enum: `USAGE`, `NOT_FOUND`, `NOT_RUNNING`, `CONFLICT`, `PRECONDITION`, `DOCKER`, `INTERNAL`.
Core functions SHALL raise `CwcliError` rather than printing or calling `typer.Exit`, so each frontend can map `kind` to its own exit code and rendering in one place.

#### Scenario: Hard failure raises a typed error

- **WHEN** a core function cannot proceed (project absent, Docker daemon unreachable, a precondition gate fails)
- **THEN** it raises `CwcliError` with the matching `ErrorKind` and does NOT print or call `typer.Exit`

#### Scenario: Frontend maps kind to exit code

- **WHEN** a frontend catches a `CwcliError`
- **THEN** it maps `USAGE` to exit 2 and every other `ErrorKind` to exit 1, rendering the message (and `hint` where present) in that frontend's own style
