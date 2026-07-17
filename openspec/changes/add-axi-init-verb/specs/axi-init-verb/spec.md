## ADDED Requirements

### Requirement: The axi surface provides an `init` verb that renders the existing init core to TOON

The system SHALL provide a `cwcli axi init` command in `commands/axi.py` that is a thin renderer over the existing `core.init_instance` then `core.init_bench`, adding no business logic and touching no `core/` module.
It SHALL call the SAME two core functions the human `cwcli init` calls, emit the returned `InitReport` as ONE TOON document on stdout, and NEVER prompt.
It SHALL take `project` as an argument plus the options `--port`, `--bench`, `--site`, `--bench-parent`, `--frappe-branch`, `--version`, `--db-root-password`, `--admin-password`, `--reuse-bench/--no-reuse-bench`, `--install-erpnext`, and `--erpnext-branch`, and SHALL NOT take `--verbose` or `--auto-start`.
`--bench` SHALL be the new bench NAME to create (matching `cwcli init --bench`), documented as distinct from the `--bench <index|label>` selector other axi verbs use.

#### Scenario: A successful init emits one terminal TOON document

- **WHEN** `cwcli axi init proj --admin-password s3cret` runs against a fresh project and both core stages return OK
- **THEN** stdout carries exactly one TOON `InitReport` document, stderr carried the progress narration, and the process exits 0

#### Scenario: stdout stays pure TOON

- **WHEN** any `axi init` run emits progress
- **THEN** all progress narration is on stderr and stdout contains only the single terminal TOON document

#### Scenario: The verb is registered

- **WHEN** the axi Typer registry is inspected
- **THEN** `init` is present as a top-level axi command

### Requirement: `axi init` blocks until done and narrates coarse progress to stderr

`axi init` SHALL block for the full provisioning run and emit its terminal TOON document only when `core.init_bench` returns, exactly as `axi apps update` blocks on a long update.
It SHALL wire `core.init`'s `on_event` callback to a narrator that writes coarse phase-level lines (`InitStepStart.message` and `InitNotice.text`) to STDERR, and SHALL NOT write `InitOutput` (raw exec bytes) or `InitTrace` (verbose diagnostics).
No stderr narration line SHALL contain a secret value.
The verb's help text SHALL direct the agent to `cwcli logs <project>` / `cwcli status <project>` for live and deeper progress.

#### Scenario: Progress rides stderr, not stdout

- **WHEN** `axi init` runs a multi-step provisioning
- **THEN** phase labels appear on stderr as they occur and stdout receives nothing until the single terminal document

#### Scenario: The narration is secret-free

- **WHEN** every stderr line emitted during a site creation with `--admin-password s3cret` is captured
- **THEN** no stderr line contains `s3cret` or the db-root password value

### Requirement: `axi init` takes the admin password by env var or argv, and refuses when neither is given

`axi init` SHALL resolve the site administrator password from the `CWCLI_ADMIN_PASSWORD` environment variable and from the `--admin-password` option, with `--admin-password` winning when both are supplied.
When NEITHER is supplied it SHALL refuse with a USAGE error (exit 2) naming both the env var and the flag, and SHALL NEVER generate a password and NEVER prompt.
The MariaDB root password SHALL resolve the same way from `CWCLI_DB_ROOT_PASSWORD` or `--db-root-password`, defaulting to `123`.
The resolved password SHALL reach `bench new-site` through the core's existing `exec_stream(environment=)` transport unchanged; no stdout or stderr output SHALL carry a secret value.

#### Scenario: The env var supplies the admin password

- **WHEN** `CWCLI_ADMIN_PASSWORD=s3cret cwcli axi init proj` runs with no `--admin-password`
- **THEN** the site is created with that password and the process exits 0

#### Scenario: The flag wins over the env var

- **WHEN** both `CWCLI_ADMIN_PASSWORD=fromenv` and `--admin-password fromflag` are supplied
- **THEN** the site is created with `fromflag`

#### Scenario: A missing admin password is a usage error

- **WHEN** neither `CWCLI_ADMIN_PASSWORD` nor `--admin-password` is supplied
- **THEN** the verb emits a TOON `error:` line naming `CWCLI_ADMIN_PASSWORD` and `--admin-password`, does not prompt, and exits 2

### Requirement: The interactive choice surfaces become non-prompting errors naming the exact flag

`axi init` SHALL render every `NEEDS_CHOICE` returned by the core as a non-prompting error, never a prompt, default, or hang.
An unresolved `confirm_reuse_bench` (the bench directory exists and neither `--reuse-bench` nor `--no-reuse-bench` was passed) SHALL be a USAGE error (exit 2) naming both flags, rendered by a new branch in `emit_axi_choice_as_usage_error`.
A `confirm_start` (a stage-1 readiness poll timeout, or a stage-2 race where the container stopped between stages) SHALL be an operational error (exit 1) with a message tailored to init pointing at `cwcli status` / `cwcli logs`, and the verb SHALL NOT re-invoke a stage with `auto_start=True`.
A port conflict (`CwcliError(CONFLICT, "ports.in_use")`) and a `bench.exists` conflict (`--no-reuse-bench` against an existing bench) SHALL flow through the generic `emit_axi_error` to exit 1 carrying their existing `--port` / bench hints.

#### Scenario: An existing bench with no reuse flag is a usage error

- **WHEN** `axi init` runs and the target bench directory already exists with neither `--reuse-bench` nor `--no-reuse-bench` passed
- **THEN** the verb emits a TOON `error:` line naming `--reuse-bench` and `--no-reuse-bench`, does not prompt, and exits 2

#### Scenario: A readiness timeout is an operational error, not a prompt

- **WHEN** stage 1 brings up the containers but they do not pass the readiness poll
- **THEN** the verb emits a TOON `error:` line naming `cwcli status` / `cwcli logs`, does not prompt, and exits 1

#### Scenario: A port conflict names --port and exits 1

- **WHEN** the requested ports are already in use
- **THEN** the verb emits a TOON `error:` line with a `help:` line naming `--port` and exits 1

### Requirement: `axi init` maps status and errors to honest exit codes

`axi init` SHALL exit 0 when the core result is `OK` or `WARNING` (a soft provisioning failure is a `WARNING` and still exits 0 because the bench and site were created, with the warnings in the TOON warnings block); exit 1 on any operational `CwcliError` (`PRECONDITION`, `DOCKER`, `CONFLICT`, `NOT_RUNNING`) and the `confirm_start` cases; and exit 2 on any USAGE error and the unresolved `confirm_reuse_bench`.

#### Scenario: A soft provisioning failure exits 0

- **WHEN** a yarn or setuptools install soft-fails but the bench and site are created
- **THEN** the result is a `WARNING`, the warning rides the TOON document, and the process exits 0

#### Scenario: A malformed --version is a usage error

- **WHEN** `axi init proj --version 16.26 --admin-password s3cret` runs
- **THEN** the verb emits the core's USAGE error message on a TOON `error:` line and exits 2
