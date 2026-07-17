## ADDED Requirements

### Requirement: core.init is two sequential plain functions behind typed envelopes

The system SHALL provide, in a new `core/init.py`, `core.init_instance(project_name, *, port=8000, auto_start=False, on_event=None) -> Result[InstanceUp]` (project directory, compose download, port/image customization, pull, up, and the bounded readiness poll) and `core.init_bench(project_name, *, bench_name, site_name, bench_parent, frappe_ref, db_root_password, admin_password, reuse_bench=None, install_erpnext=False, erpnext_branch, auto_start=False, on_event=None) -> Result[InitReport]` (bench resolve, version gating, `bench init`, bench configs, `bench new-site`, optional ERPNext, cache clear).

Both SHALL be plain functions (no generator, no iterator return), with progress riding the optional typed-event `on_event` callback.
`InstanceUp` and `InitReport` SHALL carry only plain serializable data (no live Docker object, no argv) and `InitReport` SHALL have NO password field.
`core/init.py` SHALL import no `rich`, no `questionary`, and no `typer`, and SHALL never print, prompt, or exit.

#### Scenario: A fresh non-interactive init resolves both stages to OK

- **WHEN** `init_instance("proj", port=8000)` succeeds and `init_bench(...)` runs with `reuse_bench=None` against a container with no existing bench
- **THEN** both return `Result(OK, ...)`, `InitReport.bench_created` and `site_created` are True, and no choice is surfaced

#### Scenario: The DTOs are plain data

- **WHEN** `asdict()` is applied to a returned `InstanceUp` or `InitReport`
- **THEN** the result contains only plain serializable values, with no Docker object and no password at any depth

#### Scenario: The core is silent

- **WHEN** any `init_instance` or `init_bench` path runs without an `on_event` callback
- **THEN** nothing is written to stdout or stderr, and `tests/test_core_envelope.py`'s import ban covers `core/init.py` automatically

### Requirement: The secret env-transport is preserved byte-exactly through exec_stream

`init_bench` SHALL execute `bench new-site` via `core.exec_stream` with the admin and MariaDB root passwords supplied through the `environment=` parameter and referenced in the command string ONLY as unexpanded `"$CWCLI_DB_ROOT_PASSWORD"` / `"$CWCLI_ADMIN_PASSWORD"`, exactly as the pre-migration `_exec_in_container` transport.
Non-secret interpolations SHALL keep `shlex.quote`.
No event, warning, DTO field, or command-echo trace SHALL carry a secret value the old path did not print: the command-echo trace carries the `$`-references, `InitOutput`/`ExecChunk` events carry only bench's own output bytes, and the environment dict is never emitted.

#### Scenario: Secrets ride the environment, never the command string

- **WHEN** `init_bench` creates a site with `admin_password="s3cret"` and `db_root_password="123"`
- **THEN** the exec's command string contains `$CWCLI_ADMIN_PASSWORD` and not `s3cret`, and the values appear only in the `environment=` passed to `exec_stream`

#### Scenario: The event surface is secret-free

- **WHEN** every event emitted during a site creation is captured
- **THEN** no event's fields contain the admin or db-root password value

### Requirement: Version gating is preserved

The version resolution and gating SHALL move to the core with behavior unchanged: `resolve_frappe_ref` resolves a bare major to a `version-N` branch and a full SemVer to a `vX.Y.Z` tag, raising `CwcliError(USAGE)` with today's message on any other shape; the default ref stays `version-16`; the MariaDB flag, Python version (15 -> 3.12, 14 -> 3.10, 13 -> 3.9), Node major (14 -> 16, 13 -> 14), and the `setuptools<82` pin (major 13 only) SHALL gate on the major parsed from the resolved ref, handling both branch and tag forms.
The `--frappe-branch`/`--version` mutual-exclusion error SHALL stay in the frontend.

#### Scenario: A SemVer tag gates like its branch equivalent

- **WHEN** `init_bench` runs with `frappe_ref="v14.80.0"`
- **THEN** `bench new-site` uses `--no-mariadb-socket` and the bench provisions Python 3.10 and Node 16, exactly as `version-14` does

#### Scenario: A malformed version is a typed usage error

- **WHEN** `resolve_frappe_ref("16.26")` is called
- **THEN** it raises `CwcliError(USAGE)` carrying today's message text

### Requirement: Three choice surfaces, resolved by the frontend in both modes

`init_instance` SHALL return `NEEDS_CHOICE`/`confirm_start` when the readiness poll times out with `auto_start=False`, and SHALL raise `CwcliError(NOT_RUNNING)` on timeout with `auto_start=True` (the structural cap; no core function performs a container start).
`init_bench` SHALL return `confirm_start` via `resolve_container_state(offer_choice=True)` as its race backstop, and SHALL return the NEW `Choice(kind="confirm_reuse_bench", param="reuse_bench")` when the bench exists and `reuse_bench is None`.
The tri-state SHALL be honored: `reuse_bench=True` reuses (bench init skipped); `reuse_bench=False` raises `CwcliError(CONFLICT)` with today's message.

The CLI SHALL resolve these preserving today's behavior in BOTH modes (the captain standard): interactively, the reuse confirm and the decline-and-rename loop continue setup on a fresh bench, and a cancelled or blank prompt exits 0 with "No changes made."; non-interactively, an existing bench with no flag refuses naming `--reuse-bench`/`--no-reuse-bench` (exit 1), a missing `--admin-password` refuses (exit 1), and every prompt has a flag so the command runs to completion with no prompt.

#### Scenario: Declining reuse continues on a fresh bench

- **WHEN** the frontend resolves `confirm_reuse_bench` with a decline and a replacement name, re-invoking `init_bench` with the new `bench_name`
- **THEN** setup continues on the fresh bench (issue #20's flow), and an also-existing replacement surfaces the choice again

#### Scenario: A non-TTY without a reuse flag refuses instead of hanging

- **WHEN** `cwcli init` hits an existing bench on a non-TTY with `reuse_bench` unset
- **THEN** it exits 1 naming both flags, exactly as today, without entering any prompt

#### Scenario: The poll timeout fails closed after a claimed start

- **WHEN** `init_instance` is re-invoked with `auto_start=True` and the containers are still not running at the poll's end
- **THEN** it raises `CwcliError(NOT_RUNNING)` and the CLI exits non-zero rather than looping

### Requirement: init consumes the exec-stream contract with the honest exit code

The ten provisioning execs (bench init, four set-configs, new-site, two final configs, the ERPNext pair) SHALL flow through `core.exec_stream`, adopting its honest polled exit code: a lost stream or unknowable code raises the contract's typed `DOCKER` errors, and the pre-migration `Command failed with exit code None` message becomes unrepresentable.
A non-zero exit SHALL raise `CwcliError(PRECONDITION)` with today's failure message, or today's ENOSPC message when the drained output shows it (the ENOSPC hint stays exactly as alive as today: present on drained execs, absent on streamed ones).
The buffered probe/install helpers SHALL keep buffered argv `exec_run` calls.
Verbose rendering SHALL preserve the raw stdout/stderr split and carriage returns.

#### Scenario: A lost stream is a typed error, not a fabricated exit code

- **WHEN** the connection drops during a streamed `bench init` and the exec's code is unknowable
- **THEN** a `CwcliError(DOCKER)` surfaces (exit non-zero) instead of a message claiming the command failed with exit code None

#### Scenario: ENOSPC on a drained exec keeps its actionable message

- **WHEN** a non-verbose `bench new-site` fails and its drained output contains `no space left on device`
- **THEN** the error rendered is today's disk-space message, not the generic failure line

### Requirement: The add_path frontend edge dies

`core/init.py` SHALL register the bench path via `utils/config_utils.py:add_custom_path` and emit the added/already-present outcome as a typed event; `commands/init.py` SHALL no longer import `commands/config.py`, and no module under `core/` SHALL import `commands/` at runtime.

#### Scenario: The search path is registered without a command-to-command call

- **WHEN** `init_bench` resolves a bench (fresh or reused)
- **THEN** `add_custom_path` records the bench path, the outcome rides an event the CLI renders as today's stdout line, and grep finds no `commands.config` import in `commands/init.py`

### Requirement: There is no axi init verb this batch, and the deferral is asserted

The `axi` surface SHALL NOT gain an `init` verb in this change: whether an agent may create instances is a product decision deferred to the captain, decoupled from the migration.
A test SHALL assert the `axi` Typer registry has no `init` command, and its docstring SHALL record the deferral (not a structural refusal) so it can be deliberately revisited.

#### Scenario: The verb cannot slip in unnoticed

- **WHEN** the axi verb registry is inspected by the test suite
- **THEN** no `init` command is registered, and the assertion records the deferred-decision reason

### Requirement: The human CLI preserves today's observable behavior

`commands/init.py` SHALL become a renderer over the two core calls: every exit code preserved (validation and refusals 1, cancel paths 0, conflicts 1); messages pinned by the existing suites byte-identical; fail-fast ordering preserved by up-front validator calls; the generated admin password printed once and ONLY when `InitReport.site_created` is true; idempotent re-runs (existing bench and site) skipping `bench init` and `bench new-site` exactly as today.
Two drifts are named and disclosed: verbose compose output becomes plain streamed lines (captured, not TTY-inherited), and the lost-stream failure message becomes the contract's typed error.

#### Scenario: An idempotent re-run sets and prints no password

- **WHEN** `cwcli init` runs against an existing project, bench, and site with no `--admin-password`
- **THEN** `bench new-site` is skipped, `InitReport.site_created` is False, and no generated password is printed

#### Scenario: The characterization suite survives the migration unchanged

- **WHEN** the pre-migration characterization tests (exec order and command strings, skip-on-exists gating, refusal messages, port-conflict rendering) run against the migrated code
- **THEN** they pass without assertion changes
