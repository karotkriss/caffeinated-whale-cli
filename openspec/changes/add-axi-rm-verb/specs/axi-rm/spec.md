## ADDED Requirements

### Requirement: cwcli axi rm emits one TOON document and never prompts

The system SHALL provide `cwcli axi rm <project>` with flags `--yes` and `--volumes/--no-volumes`, calling the EXISTING `core.remove` and emitting ONE TOON `RemovalOutcome` document on stdout.

The verb SHALL accept exactly ONE project per invocation, and SHALL NOT accept the human verb's variadic project list or piped stdin.
The verb SHALL NOT provide `--verbose` or `--json`.
The verb SHALL NOT prompt on any path.
Removal progress and warnings SHALL go to stderr; stdout SHALL carry only TOON.

#### Scenario: A successful removal emits the outcome and exits 0

- **WHEN** `cwcli axi rm myproj --yes` removes a running project cleanly
- **THEN** stdout is one TOON document carrying `project`, `found`, `containers_removed`, `volumes_removed`, `dir_removed` and `backup_ok`, and the process exits 0

#### Scenario: The verbose diagnostic never reaches either stream

- **WHEN** `core.remove` emits an `RmTrace` event during the removal
- **THEN** its text appears on neither stdout nor stderr, because `RmTrace` is the human verb's `--verbose`-only diagnostic and this surface has no `--verbose`

### Requirement: Consent is required and grants consent only

The verb SHALL require `--yes` before calling `core.remove`, and SHALL refuse with `USAGE` (exit 2) when it is absent, naming `--yes` and what will be permanently deleted.

`--yes` SHALL grant consent ONLY.
The verb SHALL NOT start any container on any path, diverging deliberately from the human `cwcli rm --yes`, which additionally auto-starts a stopped project to take its backup.

#### Scenario: Omitting --yes removes nothing

- **WHEN** `cwcli axi rm myproj` is run without `--yes`
- **THEN** `core.remove` is never called, stdout carries `error:` and a `help:` line naming `--yes`, and the process exits 2

#### Scenario: Consent never implies a start

- **WHEN** the verb runs with `--yes`
- **THEN** no code path reaches a container start, an `auto_start` parameter, or `ensure_containers_running`

### Requirement: The backup gate has no agent-surface bypass

The verb SHALL call `core.remove` with `no_backup=False` on every path.
The verb SHALL NOT provide a `--no-backup` flag or any other means of disabling the verified-backup gate.

#### Scenario: The gate is always on

- **WHEN** the verb calls `core.remove`
- **THEN** it passes `no_backup=False`, and the verb exposes no flag that can change it

### Requirement: A stopped project is refused before anything is touched

WHEN volumes are to be removed AND the project's frappe container is not running, the verb SHALL refuse with `NOT_RUNNING` (exit 1) BEFORE calling `core.remove`, because without an auto-start the verified backup that gates volume deletion cannot be taken.

The refusal SHALL name all three ways forward: starting the project and re-running, `--no-volumes`, and the human `cwcli rm <project> --no-backup`.

This refusal SHALL be scoped to the STOPPED case. An orphaned project (no containers at all) SHALL be passed to `core.remove`, which distinguishes leftover data-bearing volumes from a project that is genuinely absent.

`core.remove`'s own not-running branch SHALL remain the fail-closed backstop behind this pre-check, not be replaced by it.

#### Scenario: A stopped project on the volume path is refused

- **WHEN** `cwcli axi rm myproj --yes` targets a project whose frappe container is stopped
- **THEN** `core.remove` is never called, stdout names `cwcli start myproj`, `--no-volumes` and `--no-backup`, and the process exits 1

#### Scenario: A stopped project is removable when no data is destroyed

- **WHEN** `cwcli axi rm myproj --yes --no-volumes` targets a stopped project
- **THEN** the refusal does not fire, `core.remove` is called with `remove_volumes=False`, and the named volumes survive

#### Scenario: An orphan is classified by the core

- **WHEN** the target has no containers at all
- **THEN** the verb calls `core.remove`, and a project with nothing left returns `found: false` and exits 0

### Requirement: A blocked gate reports that nothing was deleted and where the hatch is

WHEN `core.remove` returns failures with `backup_ok` false, the verb SHALL add a warning stating that nothing was deleted and naming `cwcli rm <project> --no-backup`, because the core's own hint names a flag this surface does not have.

#### Scenario: The gate blocks and the caller is told what to do

- **WHEN** the verified backup cannot be created for a running project
- **THEN** nothing is removed, stdout carries a warning naming `cwcli rm myproj --no-backup`, and the process exits 1

### Requirement: The exit code reads the outcome's failures, not the envelope status

The verb SHALL derive its exit code from `RemovalOutcome.failures`, NEVER from `Result.status`.

#### Scenario: A partial removal exits non-zero

- **WHEN** `core.remove` returns `Status.WARNING` with a non-empty `failures` list
- **THEN** the process exits 1, because `WARNING` maps to exit 0 everywhere else and would report success for an instance that is still half there

### Requirement: The deliberate-absence test is reversed, not deleted

`tests/test_core_rm.py`'s registry-absence assertion SHALL be replaced by an assertion of the verb's PRESENCE, carrying the reasoning for the reversal and pinning the two properties this surface decided (consent-only `--yes`, no `--no-backup`), following the `axi apps checkout` and `axi init` precedent.

#### Scenario: Widening either decided property must edit a test

- **WHEN** someone adds a `--no-backup` flag or an auto-start to the verb
- **THEN** a test in `tests/test_core_rm.py` or `tests/test_axi_rm.py` fails
