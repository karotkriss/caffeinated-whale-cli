## ADDED Requirements

### Requirement: core.remove is one plain function behind a typed envelope

The system SHALL provide, in a new `core/rm.py`, `core.remove(project_name, *, remove_volumes=True, no_backup=False, on_event=None) -> Result[RemovalOutcome]` - the migrated `_remove_project` and its data-destruction helpers (verified copy-out backup, per-bench archive, named-volume removal, project-directory removal).

It SHALL be a plain function (no generator, no iterator return), with progress and narration riding the optional typed-event `on_event` callback.
`RemovalOutcome` SHALL carry only plain serializable data - `found`, `orphan`, `containers_removed`, `volumes_removed`, `dir_removed`, `backup_ok`, and `failures` (a `list[str]`) - with no live Docker object at any depth.
`core/rm.py` SHALL import no `rich`, no `questionary`, and no `typer`, and SHALL never print, prompt, exit, or start a container.
No plan/apply preview phase SHALL be added: rm's confirmation is a frontend concern computed from data the frontend already holds, and no manifest of the volumes/sites/directories to be deleted is manufactured.

#### Scenario: A clean full removal returns OK with no failures

- **WHEN** `remove("proj", remove_volumes=True, no_backup=False)` runs against a running project whose sites all back up and verify
- **THEN** it returns `Result(OK, RemovalOutcome(found=True, backup_ok=True, failures=[], ...))`, the named volumes and project directory are removed, and the cache is cleared

#### Scenario: The DTO is plain data

- **WHEN** `asdict()` is applied to a returned `RemovalOutcome`
- **THEN** the result contains only plain serializable values, with no Docker object at any depth

#### Scenario: The core is silent

- **WHEN** any `remove` path runs without an `on_event` callback
- **THEN** nothing is written to stdout or stderr, and `tests/test_core_envelope.py`'s import ban covers `core/rm.py` automatically

### Requirement: The fail-closed backup gate is preserved byte-exactly

`core.remove` SHALL evaluate the EARLY backup gate `remove_volumes and not no_backup and not backup_ok` BEFORE removing any container: when true it SHALL record the refusal in `RemovalOutcome.failures` and return immediately without stopping or removing any container, touching any volume or directory, or clearing the cache.
`backup_ok` SHALL be `all(...)` across every bench (cached, else live-discovered via `core.inspect.discover_benches`, else the single default bench), each bench's sites backed up through the verified copy-out (`bench backup --with-files`, artifacts streamed out and the database dump confirmed present and non-empty on the host archive, scoped to this run's timestamp token).
When no frappe container is running and `remove_volumes and not no_backup`, `backup_ok` SHALL be set False so the EARLY gate aborts (the not-running fail-closed backstop).
`--no-backup` SHALL opt out of the gate entirely; `--no-volumes` SHALL leave the gate inactive (no volume data is destroyed).
The LATE gate `container_removal_failed or archive_failed` SHALL protect the named-volume and directory deletion after container removal.

#### Scenario: A failed backup aborts before any container is removed

- **WHEN** a site's live backup cannot be verified on the host on the `--volumes` path without `--no-backup`
- **THEN** `remove` records the refusal in `failures` and returns with `containers_removed=0`, no volume or directory removed, and the cache preserved

#### Scenario: One bench's failed backup blocks the whole instance

- **WHEN** a multi-bench project has two benches and the second bench's backup does not verify
- **THEN** `backup_ok` is False, the EARLY gate aborts, and neither bench's volumes are deleted

#### Scenario: A stopped project with no transient start refuses on the volumes path

- **WHEN** `remove` runs on the `--volumes` path without `--no-backup` against a project whose frappe container is not running
- **THEN** `backup_ok` is False, the gate aborts, and nothing is deleted

#### Scenario: An orphan is refused on the volumes path but cleaned under no-backup

- **WHEN** `remove` runs against a project with no containers but a lingering volume/directory
- **THEN** on the `--volumes` path without `--no-backup` it refuses (nothing to start for a backup), and with `--no-backup` it cleans up the volume and directory and clears the cache

### Requirement: The H5 path-traversal guards are public core functions

`core/rm.py` SHALL expose `is_valid_project_name(name)` and `is_safe_project_dir(project_dir)` as public functions rejecting empty/`.`/`..`/absolute/separator/NUL names and any path that resolves outside `PROJECTS_DIR`.
`core.remove` SHALL re-check its own `project_name` at entry and raise `CwcliError(USAGE)` on an invalid name; the destructive helpers SHALL hard-guard on `is_safe_project_dir` immediately before any `rmtree`/copytree.
The human CLI SHALL keep its up-front pre-filter loop calling these validators, rejecting invalid names with today's message and exiting 1 if any was rejected.

#### Scenario: rm .. cannot escape the projects root

- **WHEN** `cwcli rm ..` is invoked
- **THEN** the CLI rejects the name before any destructive step, exits 1, and no `rmtree` or volume removal runs

#### Scenario: The core refuses an invalid name directly

- **WHEN** `remove("..")` is called directly (bypassing the CLI pre-filter)
- **THEN** it raises `CwcliError(USAGE)` before touching Docker or the filesystem

### Requirement: Honest exit codes read the outcome's failures, not the envelope status

The human CLI SHALL decide its exit code from `RemovalOutcome.failures` being non-empty (exit 1), never from `Result.status`, so a partial failure carried as a `WARNING`-shaped envelope never reports success.
A genuinely-not-found project SHALL be `found=False` with empty `failures`, rendered as today's "Project not found" error and preserved as an exit-0 no-op in the aggregate.
A Docker connection error SHALL raise `CwcliError(DOCKER)`, which the CLI catches per-project, records as a failure, and exits 1.

#### Scenario: A partial failure exits non-zero

- **WHEN** a container removal fails and the LATE gate blocks volume/directory deletion
- **THEN** `failures` is non-empty, the CLI prints the failures (no green success line), and exits 1

#### Scenario: Removing an absent project is an exit-0 no-op

- **WHEN** `cwcli rm nonexistent` runs against a project with no containers, volumes, or directory
- **THEN** the CLI prints "Project not found" and exits 0

### Requirement: The stopped-project transient-start flow stays in the frontend

The start -> back up -> delete orchestration for a stopped project on the `--volumes` path (`_transient_start_for_backup`, `_wait_for_db_ready`, `_project_run_state`, `_frappe_container_running`, `_stop_after_transient_start`) SHALL remain in `commands/rm.py`, running OUTSIDE the removal spinner because it reuses `commands/start.py:_check_port_conflicts`, which prompts.
On a failed start or a failed backup, the CLI SHALL abort the project, return it to its stopped state via `core.stop`, keep all data, and exit non-zero.
`core.remove` SHALL never start a container.

#### Scenario: A stopped project is started, backed up, then deleted

- **WHEN** `cwcli rm <stopped> --volumes` runs and the transient start and backup succeed
- **THEN** the frontend starts the project outside the spinner, `core.remove` takes the verified backup and deletes, and no explicit stop is needed

#### Scenario: A failed transient start keeps all data and returns to stopped

- **WHEN** the transient start cannot bring the project up (port conflict, image gone, DB never ready)
- **THEN** the frontend does not call `core.remove`, returns the project to stopped, records a failure, and exits non-zero with nothing deleted

### Requirement: There is no axi rm verb this batch, and the deferral is asserted

The `axi` surface SHALL NOT gain an `rm` verb in this change: whether an agent may delete an instance's data is a product decision deferred to the captain, decoupled from the migration.
A test SHALL assert the `axi` Typer registry has no `rm` command, and its docstring SHALL record the deferral (not a structural refusal) so it can be deliberately revisited.

#### Scenario: The verb cannot slip in unnoticed

- **WHEN** the axi verb registry is inspected by the test suite
- **THEN** no `rm` command is registered, and the assertion records the deferred-decision reason

### Requirement: The human CLI preserves today's observable behavior

`commands/rm.py` SHALL become a renderer over `core.remove`: the trailing-flag recovery, piped input, up-front name pre-filter, the recache-skip-for-stopped, the confirm prompt with its stopped-project disclosure, the transient-start orchestration, and the aggregate reporting SHALL all preserve today's exact messages, exit codes, and the streamed multi-GB-safe copy-out.
Progress and narration SHALL be driven by `core.remove`'s typed events reproduced in this CLI's historical styling and streams.

#### Scenario: The characterization suite survives the migration unchanged

- **WHEN** the pre-migration characterization tests (what-deletes-what, the C1 abort-before-removal, the H5 refusals, the M11 exit codes, the multi-bench fan-out, the stopped/orphan paths) run against the migrated code
- **THEN** they pass without assertion changes
