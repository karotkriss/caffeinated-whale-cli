## ADDED Requirements

### Requirement: core.backup returns a typed BackupOutcome envelope

The system SHALL provide `core.backup(project, *, site=None, bench=None, bench_path=None, with_files=False, ...) -> Result[BackupOutcome]` that owns the backup logic and I/O currently in `commands/backup.py` (container resolution, bench resolution, default-site resolution, site/path shell-metachar validation, backup-directory ensure, and the `bench --site <site> backup` exec).
`BackupOutcome` SHALL be a dataclass carrying `site`, `bench_path`, `artifact_path`, and `included_files`.
`core.backup` SHALL NOT print, prompt, or call `typer.Exit`: it returns `NEEDS_CHOICE` for the multi-bench and stopped-container forks, raises `CwcliError` for hard failures (project/site/bench not found -> `NOT_FOUND`; empty or shell-unsafe site/path -> `USAGE`; a failed `bench backup` -> `PRECONDITION`; daemon errors -> `DOCKER`), and returns `Result(status=OK, data=BackupOutcome(...))` on success.

#### Scenario: Successful backup returns an outcome

- **WHEN** `core.backup` runs against a running project with a resolvable site and the `bench backup` exec succeeds
- **THEN** it returns `Result(status=OK, data=BackupOutcome(site=..., bench_path=..., artifact_path=..., included_files=...))` and prints nothing

#### Scenario: Multi-bench project with no selector

- **WHEN** `core.backup` runs on a multi-bench project with neither `bench` nor `bench_path`
- **THEN** it returns a `NEEDS_CHOICE` result carrying a `select_bench` choice and does NOT run any backup

#### Scenario: Stopped container without auto-start

- **WHEN** `core.backup` runs against a stopped project without an auto-start directive
- **THEN** it returns a `NEEDS_CHOICE` result carrying a `confirm_start` choice and does NOT run any backup

#### Scenario: Missing site raises a typed error

- **WHEN** `core.backup` is given a `site` that does not exist on the resolved bench
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` and does NOT run any backup

#### Scenario: Failed bench backup raises a typed error

- **WHEN** the `bench --site <site> backup` exec returns a non-zero exit code
- **THEN** `core.backup` raises `CwcliError(kind=PRECONDITION)` and returns no success outcome

### Requirement: Re-seated interactive cwcli backup on the core

The system SHALL re-seat `cwcli backup` as a thin frontend over `core.backup`: the typer signature (`--site`/`--bench`/`--path`/`--with-files`/`--yes`/`--verbose`), the `TipSpinner`, and every `console.print` stay in the command, and the frontend resolves any returned `confirm_start`/`select_bench` choice via the CLI wrappers (prompt then re-invoke) and maps any `CwcliError` to the current message and exit code.
The user-facing behavior of `cwcli backup` SHALL be unchanged - identical success banner, identical error messages and non-zero exits, identical `--yes` auto-start and multi-bench handling.

#### Scenario: Non-interactive backup preserved

- **WHEN** a user runs `cwcli backup <project> --site <site> -y` from a non-TTY (stdin closed)
- **THEN** the command creates a real backup, prints "Successfully created backup", and exits 0 - exactly as before this change (as pinned by `tests/e2e/test_backup_e2e.py`)

#### Scenario: Interactive backup preserved

- **WHEN** a user runs `cwcli backup <project> --site <site>` on a running instance through a pty
- **THEN** the command runs straight through, prints "Successfully created backup", and exits 0 - exactly as before this change

#### Scenario: Backup E2E stays green unchanged

- **WHEN** the existing `tests/e2e/test_backup_e2e.py` suite runs against the re-seated command
- **THEN** both the non-interactive and interactive tests pass without modification to the test file (refactor-under-green)
