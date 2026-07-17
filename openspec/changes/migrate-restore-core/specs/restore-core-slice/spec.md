## ADDED Requirements

### Requirement: core.restore is a plan/apply split behind typed envelopes

The system SHALL provide, in a new `core/restore.py`, `core.restore_plan(project_name, *, site=None, latest=False, backup_file=None, selected_backup=None, bench=None, bench_path=None, on_event=None) -> Result[RestorePlan]` (the read-only resolve: container, bench with the no-cache inspect fallback, default site, backup scan, selection, missing-apps, origin mismatch) and `core.restore_apply(plan, *, mariadb_root_username, mariadb_root_password, admin_password=None, consent=False, no_migrate=False, on_event=None) -> Result[RestoreReport]` (the destructive act: the guarded `bench restore --force`, the encryption-key merge, migrate, restart).

Both SHALL be plain functions (no generator, no iterator return), with progress riding the optional typed-event `on_event` callback.
`RestorePlan` and `RestoreReport` SHALL carry only plain serializable data (no live Docker object, only container-path strings, no argv), and `RestoreReport` SHALL have NO password field.
`core/restore.py` SHALL import no `rich`, no `questionary`, and no `typer`, and SHALL never print, prompt, or exit.

#### Scenario: A non-interactive selector restore resolves plan then apply to OK

- **WHEN** `restore_plan("proj", site="s", latest=True)` succeeds and `restore_apply(plan, mariadb_root_username="root", mariadb_root_password="pw", consent=True)` runs
- **THEN** the plan returns `Result(OK, RestorePlan(...))` with the newest target-site backup, and the apply returns a `RestoreReport` with `restored` True and no choice surfaced

#### Scenario: The DTOs are plain data with no secret

- **WHEN** `asdict()` is applied to a returned `RestorePlan` or `RestoreReport`
- **THEN** the result contains only plain serializable values, with no Docker object, no argv, and no password at any depth

#### Scenario: The core is silent

- **WHEN** any `restore_plan`, `receive_plan`, or `restore_apply` path runs without an `on_event` callback
- **THEN** nothing is written to stdout or stderr, and `tests/test_core_envelope.py`'s import ban covers `core/restore.py` automatically

### Requirement: The destructive act stays a buffered exec with secrets off the argv

`restore_apply` SHALL run `bench restore --force` and `bench migrate` via buffered `frappe_container.exec_run(["sh", "-c", cmd], ...)` (the `backup` shape, not the exec-stream contract), with the MariaDB root password and any admin password supplied through the `environment=` parameter and referenced in the command string ONLY as unexpanded `"$CWCLI_MARIADB_ROOT_PASSWORD"` / `"$CWCLI_ADMIN_PASSWORD"`.
The argument order SHALL be byte-identical on the normal and receive paths (database path, `--mariadb-root-username`, `--mariadb-root-password`, `--force`, then the file archives), and the receive path SHALL use the FULL container path to the database dump, not the bare filename.
No event, warning, DTO field, or command-echo trace SHALL carry a secret value the old path did not print.

#### Scenario: The password rides the environment, never the command string

- **WHEN** `restore_apply` builds the restore command with `mariadb_root_password="s3cret"`
- **THEN** the command string contains `$CWCLI_MARIADB_ROOT_PASSWORD` and not `s3cret`, and the value appears only in the `environment=` passed to `exec_run`

#### Scenario: The receive path uses the full container path

- **WHEN** a receive-mode restore is applied for a downloaded dump copied into `{backup_dir}`
- **THEN** the restore command names `{backup_dir}/{database_file}` (the full container path), not the bare filename

### Requirement: The destructive consent and the backup selection are typed choices

`restore_apply` SHALL return `NEEDS_CHOICE`/`confirm_restore` when `consent` is not granted, so no frontend can bypass the destructive gate; the CLI SHALL render today's missing-apps and destructive confirms (honoring `--yes`, refusing on a non-TTY without `--yes` with exit 1, both keeping `auto_enter=False`) and grant `consent` only when both pass.
`restore_plan` SHALL return `NEEDS_CHOICE`/`select_backup` (carrying the target-site and other-site candidate sets) when no `--latest`/`--backup-file`/`selected_backup` is given, and the CLI SHALL render today's interactive menu on a TTY and refuse on a non-TTY naming `--latest`/`--backup-file` (exit 1).

#### Scenario: An unconsented apply refuses instead of destroying

- **WHEN** `restore_apply(plan, ..., consent=False)` is called
- **THEN** it returns `NEEDS_CHOICE`/`confirm_restore` and runs no `bench restore --force`

#### Scenario: A non-TTY without a selector refuses instead of a menu

- **WHEN** `restore_plan` is resolved with no selector and the CLI is on a non-TTY
- **THEN** the CLI exits 1 naming `--latest`/`--backup-file`, and no restore runs

### Requirement: Post-restore migrate and restart are preserved with the honest exit code

`restore_apply` SHALL, unless `no_migrate` is set, run `bench --site <site> migrate` (a failed or exception-raising migrate does NOT undo the restore, is surfaced, and still restarts) then restart the SAME restored bench via `core.start(bench_path=plan.bench_path, restart=True)`.
A failed migrate SHALL return a `WARNING`-shaped `Result` carrying `RestoreReport.migrate_ok=False`, and the CLI SHALL exit 1 when `migrate_ran and not migrate_ok` (the `core.update` report-drives-the-exit-code precedent), while a genuinely OK restore returns exit 0.

#### Scenario: A failed migrate still restarts and exits non-zero

- **WHEN** the restore succeeds but `bench migrate` returns non-zero
- **THEN** the restart still runs, `RestoreReport.migrate_ok` is False, and the CLI exits 1 without undoing the restore

### Requirement: The streamed copies and the sendme flows are preserved

The receive-path copy INTO the container SHALL stream the tar via `container.put_archive` and fail closed (`PRECONDITION`) on an unsuccessful copy; the send-path copy OUT of the container SHALL stream via `container.get_archive`.
The sendme send/receive subprocess, the ticket prompt, the clipboard, and the Ctrl-C wait SHALL remain in `commands/restore.py` (interactive host I/O the core deliberately does not consume, the `commands/logs.py` precedent), calling the core for the container copies and the plan/apply.

#### Scenario: A failed copy-in fails closed

- **WHEN** `receive_plan` copies a downloaded file into the container and `put_archive` reports failure
- **THEN** it raises `CwcliError(PRECONDITION)` and no restore proceeds

### Requirement: There is no axi restore verb this batch, and the deferral is asserted

The `axi` surface SHALL NOT gain a `restore` verb in this change: whether an agent may drop and recreate a live site's database is a product decision deferred to the captain, decoupled from the migration.
A test SHALL assert the `axi` Typer registry has no `restore` command, and its docstring SHALL record the deferral (not a structural refusal) so it can be deliberately revisited.

#### Scenario: The verb cannot slip in unnoticed

- **WHEN** the axi verb registry is inspected by the test suite
- **THEN** no `restore` command is registered, and the assertion records the deferred-decision reason

### Requirement: The human CLI preserves today's observable behavior

`commands/restore.py` SHALL become a renderer over the core: every exit code preserved (refusals and hard failures 1, decline paths 1, mutual-exclusion 1); messages pinned by the existing suites byte-identical; the destructive confirm on BOTH the normal and receive paths; the non-interactive `--latest`/`--backup-file` selectors and their mutual exclusion; the `--send`/`--receive` exclusion; `--yes`, `--no-migrate`, and the deprecated `--no-recache` no-op preserved; the encryption-key merge; and restore's own live-`currentsite.txt` default-site fallback.

#### Scenario: The characterization suite survives the migration unchanged

- **WHEN** the pre-migration characterization tests (exec arg order, secret in `environment=`, the confirms, the selectors, the streamed copies, the origin mismatch) run against the migrated code
- **THEN** they pass without assertion changes
