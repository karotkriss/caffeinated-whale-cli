## ADDED Requirements

### Requirement: core.update owns the update state machine and returns a typed report

The system SHALL provide `core.update(project_name, apps, *, bench=None, bench_path=None, sites=None, clear_cache=False, clear_website_cache=False, build=False, skip_maintenance=False, no_recache=False, auto_start=False, on_event=None) -> Result[UpdateReport]` in a new `core/update.py`, owning everything `commands/update.py:_update_project` performs between its arguments and its printed summary: the per-app pull, the post-pull recache, the affected-site discovery, the `--site` narrowing, the maintenance-mode lifecycle, the migration fan-out, the optional build and cache/lock clears, and the seven-way failure aggregation.
`core.update` SHALL NOT print, prompt, or call `typer.Exit`, and `core/update.py` SHALL import no `rich`, no `questionary`, and no `typer`.
The seven-way aggregation SHALL be RETURNED as `UpdateReport` data rather than printed, so a frontend that renders what it is handed cannot omit it.

#### Scenario: The report is a returned value, not a printed side effect

- **WHEN** `core.update` completes a run in which some phase failed
- **THEN** it returns a `Result` carrying an `UpdateReport` whose per-phase lists name every failure, and it prints nothing

#### Scenario: A multi-bench project with no selector is a select_bench choice

- **WHEN** `core.update` runs against a multi-bench project with no `bench` and no `bench_path`
- **THEN** it returns a `select_bench` `NEEDS_CHOICE` result and execs nothing

#### Scenario: The core prints nothing on any path

- **WHEN** `core/update.py` is imported and inspected
- **THEN** it imports no `rich`, `questionary`, or `typer`, and `tests/test_core_envelope.py`'s existing import ban covers it automatically

### Requirement: core.update is a plain function whose maintenance-mode cleanup is unconditional

`core.update` SHALL be an ordinary function, NOT a generator, and SHALL take an optional `on_event` callback for typed progress events.
The maintenance-mode `try/finally` SHALL remain inside a plain function so its cleanup is unconditional and cannot be skipped by a consumer's behaviour.
`core.update` SHALL NOT be reimplemented as `-> Iterator[UpdateEvent]`.

A `try/finally` inside a generator does NOT run when a consumer breaks early while holding a reference to the generator, or when the generator is caught in a reference cycle; cleanup is then deferred to the garbage collector.
A GUI pumping events from an event loop holds the iterator on `self` and lives in a widget reference cycle, which is exactly those cases, so a window closed mid-update would leave a site in maintenance mode until the collector happened to run.
Enabling that GUI is the rework's own goal, so the generator shape is rejected on the rework's most safety-critical invariant.

This is a deliberate reading of the locked decision "streaming operations return typed event iterators": that decision governs genuine streaming operations such as `logs` and raw exec output, which `core.exec_stream` already is and remains.
`update` is a state machine that emits progress and whose terminal value is a report, not a stream.
`contextlib.closing` restores determinism and is rejected because it relocates a safety-critical guarantee into every frontend's hands, including frontends not yet written.

#### Scenario: Cleanup runs even when a step raises mid-fan-out

- **WHEN** an unexpected exception unwinds out of `core.update`'s fan-out
- **THEN** maintenance mode is disabled for exactly the sites that were enabled, before the exception propagates

#### Scenario: An abandoned consumer cannot strand a site in maintenance mode

- **WHEN** a frontend stops consuming progress events part-way through an update
- **THEN** the maintenance-mode cleanup has still run, because it is not gated on a generator being resumed, closed, or collected

#### Scenario: The callback carries typed events only

- **WHEN** `on_event` is invoked
- **THEN** it receives dataclass events of builtins (`UpdateStepStart`, `UpdateOutput`, `UpdateStepEnd`, `UpdateAborted`) and no `rich`, `typer`, or `questionary` object

### Requirement: A lost stream continues the fan-out and is reported as unknown, never as a failure

When an exec's outcome cannot be established (`core.exec_stream` raising `CwcliError`), `core.update` SHALL record that item as UNKNOWN and SHALL CONTINUE the fan-out, rather than aborting it.
An unknown outcome SHALL NOT be folded into the corresponding `failed_*` list.
`UpdateReport` SHALL carry `unknown_apps`, `unknown_migrations`, and `unknown_builds` alongside their `failed_*` counterparts, and an unknown outcome SHALL make `ok` false.

Today the same real-world event produces two behaviours depending on whether Docker happened to record an exit code: a migration that returns non-zero is recorded and the fan-out continues, while a migration whose stream is lost aborts the fan-out.
Nothing about that difference is meaningful to a user or an agent, and this requirement makes them one behaviour.
The failed/unknown distinction is preserved because a lost stream means the command MAY STILL BE RUNNING: an agent branching on `axi apps update` will retry a failure, and retrying a live migration is harmful.

All exec-stream `CwcliError`s SHALL be treated as unknown, including `exec.start_failed`; that code SHALL NOT be split out as a definite failure, because it is a confident claim of "did not run" derived from an API call whose own outcome is uncertain, and the safe direction for a retry decision is unknown.
Each unknown outcome SHALL also append a `Message` to the envelope's `warnings`, so the report says what is unknown and the warnings say why.

#### Scenario: A lost migration stream does not stop the remaining sites

- **WHEN** a site's migration stream is lost during a multi-site fan-out
- **THEN** that site is recorded in `unknown_migrations`, the remaining sites are still migrated, and the report distinguishes it from a site in `failed_migrations`

#### Scenario: A stuck site keeps its remediation when a stream is lost

- **WHEN** a migration stream is lost AND a site cannot be taken back out of maintenance mode
- **THEN** the returned report names the stuck site in `failed_maintenance_disable`, and the frontend renders its manual `bench --site <site> set-maintenance-mode off` remediation

#### Scenario: An unknown outcome is never reported as success

- **WHEN** any item is recorded as unknown
- **THEN** `UpdateReport.ok` is false and the frontend exits non-zero

### Requirement: The load-bearing maintenance-mode gate and per-phase invariants are preserved

`core.update` SHALL migrate only the sites that actually entered maintenance mode (or every affected site when `skip_maintenance` is set), and SHALL apply the optional cache clear, website-cache clear, and lock clear to that SAME set.
A site that could not enter maintenance mode SHALL be recorded in `failed_maintenance_enable`, SHALL NOT be migrated or cleared, and SHALL make `ok` false.
Maintenance mode SHALL be enabled per site with each success recorded as it happens, so a mid-loop failure still leaves an accurate record of exactly which sites to disable.
The `finally` SHALL attempt to disable exactly the sites that were enabled, and any site that cannot be taken back out SHALL be recorded in `failed_maintenance_disable`, which SHALL make `ok` false.
`core.update` SHALL perform exactly ONE pull pass and ONE affected-site discovery pass per run.

#### Scenario: A site that never entered maintenance is never migrated

- **WHEN** enabling maintenance mode fails for one of several affected sites
- **THEN** that site is absent from `migrated_sites`, present in `failed_maintenance_enable`, is neither cache-cleared nor lock-cleared, and `ok` is false

#### Scenario: One pull and one discovery pass, in both presentations

- **WHEN** `core.update` runs, with a renderer attached or without one
- **THEN** each named app is pulled exactly once and affected-site discovery runs exactly once, regardless of whether the affected set is empty

#### Scenario: A stuck site is never silent

- **WHEN** a site cannot be taken back out of maintenance mode
- **THEN** it is named in `failed_maintenance_disable`, `ok` is false, and the frontend exits non-zero

### Requirement: The frappe fork stops hardcoding its consumption mode

`core.update` SHALL run the bench-wide `bench update --reset` path when any named app is `frappe` (case-insensitive), and SHALL NOT hardcode whether that command's output is rendered.
Consumption SHALL be the caller's choice via `on_event`, exactly as the exec-stream contract establishes that `verbose` decides whether to RENDER events rather than how to OBTAIN them.

`_run_frappe_update_reset` currently calls `_stream_command(..., verbose=True, ...)` with `verbose` hardcoded, so `cwcli apps update <project> --app frappe` writes bench output to stdout whatever the caller asked for.
That is the concrete blocker for an agent-facing verb, whose stdout must hold exactly one TOON document, and it is why this path is migrated first.

The frappe path SHALL continue to ignore `--site` and the per-app options, reporting them as ignored rather than silently dropping them, and SHALL continue to recache before checking the reset's exit code (preserved deliberately: a partially-applied reset genuinely changes the cache, the behaviour is untested either way, and a migration is not where that is decided).
The frappe path's own outcome SHALL ride in `failed_apps`/`unknown_apps` under the app name `frappe`, with `frappe_reset` set true and the per-site lists empty.

#### Scenario: The frappe reset writes nothing to stdout unless asked

- **WHEN** the frappe reset path runs without a renderer attached
- **THEN** no bench output reaches stdout

#### Scenario: The bench-wide path reports which options it ignored

- **WHEN** `core.update` runs the frappe path with `sites`, `clear_cache`, `clear_website_cache`, `build`, or `skip_maintenance` set
- **THEN** the ignored options are carried as warnings naming each one, because `bench update --reset` is bench-wide and they do not apply

### Requirement: core.update adds no new primitives and resolves no more than update does today

`core.update` SHALL be built from the primitives the foundation already provides (`core.docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench`, `resolvers.DEFAULT_BENCH_PATH`, `core.exec_stream`, the `Result`/`Choice`/`CwcliError` envelope) WITHOUT adding new ones.
`core.update` SHALL keep its own two-directory bench probe (`{bench_path}/apps` AND `{bench_path}/sites`) inside `core/update.py`, and SHALL NOT reuse or widen `resolvers.require_bench_dir`.
`resolvers.require_bench_dir` probes `{bench_path}/sites` only: using it as-is would silently drop update's `apps/` check, and widening it would add a check to `backup` and `unlock`, which this batch does not touch.
The probe SHALL adopt the shell-free `["test", "-d", path]` argv form that `require_bench_dir` already uses, rather than `_dir_exists`'s `["sh", "-c", ...]` form.
`core.update` SHALL NOT call `resolvers.validate_site_name` or `resolvers.resolve_default_site`: `update` uses neither today, and adding them would introduce failures `cwcli apps update` does not have.
The implementation SHALL report whether any existing primitive needed changing, widening, or special-casing.

#### Scenario: The two-directory probe is preserved exactly

- **WHEN** `core.update` runs against a bench path missing `apps/` but holding `sites/`
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` naming the bench directory, exactly as `update` does today

#### Scenario: The zero-new-primitives claim is reported either way

- **WHEN** the implementation of `core.update` is complete
- **THEN** the PR states explicitly whether any existing primitive had to be changed, and a primitive that needed bending is reported as a foundation signal rather than absorbed silently

### Requirement: An aborted run still reports its stuck sites

When an exception unwinds out of `core.update`'s fan-out, the report built in the `finally` SHALL be handed to `on_event` as a terminal `UpdateAborted` event before the exception continues to propagate.
A returned report cannot survive an unwinding `KeyboardInterrupt`, and today a Ctrl-C mid-update still prints the summary with its stuck-site remediation, because reporting happens in the `finally`.
This requirement preserves that property across the migration.
`UpdateReport.aborted` SHALL be true only on that path and false on every returned report, and it SHALL suppress any success banner.

#### Scenario: Ctrl-C mid-update still surfaces a stuck site

- **WHEN** a `KeyboardInterrupt` unwinds out of the fan-out and a site cannot be taken back out of maintenance mode
- **THEN** the frontend still receives the report naming the stuck site and its remediation, and the interrupt still propagates

#### Scenario: An abort is only reported once there was a fan-out to abandon

- **WHEN** `core.update` raises before any site has been selected for migration (a `--site` that matched nothing, or a failed pull)
- **THEN** the run is NOT dressed up as an interrupted update, while any failure already accumulated is still reported

### Requirement: Both frontends are reseated over one implementation and keep their interfaces

`cwcli apps update` and the deprecated `cwcli update` SHALL both become thin frontends over `core.update`, sharing ONE implementation so the two commands cannot drift.
`cwcli apps update` SHALL keep taking apps as a POSITIONAL argument; the deprecated `cwcli update` SHALL keep taking them as a repeatable `--app`/`-a` OPTION.
They are not interface-identical, and unifying the signatures would break every existing `cwcli update ... --app x` invocation.
The deprecated `cwcli update` SHALL keep its stderr-only deprecation warning, its full option signature, and its top-level registration, and SHALL remain a frontend rather than becoming a second core path.
Every flag of both commands SHALL behave exactly as before, and the `--site`-matched-nothing refusal SHALL keep exiting 1 on the human CLI.

#### Scenario: The deprecated alias keeps its option form

- **WHEN** `cwcli update <project> --app erpnext` is invoked
- **THEN** it updates `erpnext`, prints its deprecation warning to stderr, and behaves identically to `cwcli apps update <project> erpnext`

#### Scenario: The deprecation warning never touches stdout

- **WHEN** the deprecated `cwcli update` runs
- **THEN** its warning is written to stderr only
