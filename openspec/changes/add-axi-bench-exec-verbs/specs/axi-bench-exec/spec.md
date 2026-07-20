## ADDED Requirements

> The verb STRINGS below are written against `design.md` Decision 1's recommendation (Option A: top-level `cwcli axi migrate` and `cwcli axi run-tests`).
> If the captain rules Option B, every requirement holds unchanged and only the invocation becomes `cwcli axi bench migrate` / `cwcli axi bench run-tests`.
> If the captain declines `run-tests`, the requirements naming it are dropped and the rest stand.

### Requirement: cwcli axi migrate runs bench migrate against exactly one site

The system SHALL provide `cwcli axi migrate <project>` with flags `--site`/`-s` and `--bench`, resolving EXACTLY ONE site (the explicit `--site`, else the bench's default site) and running `bench --site <site> migrate` against it.

The verb SHALL NOT fan out across multiple sites under any flag combination.
The verb SHALL NOT provide `--yes`, `--json`, or any parameter carrying a free-form command string.
The verb SHALL NOT prompt on any path.

#### Scenario: A successful migrate emits one TOON document and exits 0

- **WHEN** `cwcli axi migrate myproj --site site1.localhost` completes successfully
- **THEN** stdout is one TOON document carrying `project`, `bench_path`, the RESOLVED `site`, `ok: true`, and one result row per step, and the process exits 0

#### Scenario: No --site resolves the bench default site and reports it

- **WHEN** `cwcli axi migrate myproj` runs against a bench with a default site
- **THEN** the default site is migrated, and the resolved site name appears in the emitted document

#### Scenario: A migrate never fans out

- **WHEN** `cwcli axi migrate myproj` runs against a bench holding several sites
- **THEN** exactly one site is migrated, and no `bench migrate` is issued for any other site

#### Scenario: A no-op migrate is a success

- **WHEN** the site has no pending patches
- **THEN** the document reports `ok: true` and the process exits 0

### Requirement: axi migrate enables maintenance mode and refuses to migrate without it

The verb SHALL enable maintenance mode on the resolved site before running `bench migrate`, and SHALL disable it in a `finally` so an exception cannot leave the site down silently.

When maintenance mode CANNOT be enabled, the verb SHALL NOT run the migrate.
When maintenance mode cannot be disabled afterwards, the verb SHALL report that fact in the document and SHALL exit non-zero.

#### Scenario: A failed maintenance enable refuses the migrate

- **WHEN** enabling maintenance mode on the resolved site fails
- **THEN** no `bench migrate` is issued, the document reports the refusal, and the process exits non-zero

#### Scenario: A site left in maintenance mode is reported and fails the verb

- **WHEN** the migrate runs but maintenance mode cannot be disabled afterwards
- **THEN** the document carries `maintenance_left_on` naming the site, and the process exits non-zero

#### Scenario: Maintenance mode is disabled even when the migrate raises

- **WHEN** the migrate step raises rather than returning a non-zero code
- **THEN** maintenance mode is still disabled before the verb returns

### Requirement: cwcli axi run-tests requires both --site and --app

The system SHALL provide `cwcli axi run-tests <project> --site <site> --app <app>` with an optional `--bench`, running `bench --site <site> run-tests --app <app>` inside the bench.

Both `--site` and `--app` SHALL be REQUIRED; the verb SHALL NOT fall back to the bench's default site and SHALL NOT run every installed app's suite.
The verb SHALL NOT provide `--yes` and SHALL NOT prompt on any path.

#### Scenario: A missing --site is a usage error, not a default

- **WHEN** `cwcli axi run-tests myproj --app myapp` runs with no `--site`
- **THEN** it emits a structured usage error naming `--site`, exits 2, and runs no tests

#### Scenario: A missing --app is a usage error

- **WHEN** `cwcli axi run-tests myproj --site site1.localhost` runs with no `--app`
- **THEN** it emits a structured usage error naming `--app`, exits 2, and runs no tests

#### Scenario: A passing suite exits 0 with the resolved target in the document

- **WHEN** the suite passes
- **THEN** stdout is one TOON document carrying `project`, `bench_path`, `site`, `app`, and `ok: true`, and the process exits 0

#### Scenario: A failing suite exits 1

- **WHEN** the suite fails
- **THEN** the document reports `ok: false` and the process exits 1

### Requirement: The command's own output is forwarded to stderr unparsed

Both verbs SHALL forward the underlying bench command's own stdout and stderr bytes to stderr, verbatim and unparsed, alongside the `$ bench ...` echo and any phase narration.

Neither verb SHALL summarize, re-format, or parse that output into the emitted document.
stdout SHALL carry exactly ONE TOON document and nothing else.

#### Scenario: A failing patch's traceback reaches the agent

- **WHEN** a migrate fails inside a patch
- **THEN** the patch's own error output appears on stderr, and stdout still parses as exactly one TOON document

#### Scenario: A failing assertion reaches the agent

- **WHEN** a test suite fails
- **THEN** the runner's own output appears on stderr in full, and the document reports only the honest pass/fail outcome

#### Scenario: stdout purity holds on every path

- **WHEN** either verb runs on any path, including every error path
- **THEN** stdout parses as exactly one TOON document

### Requirement: Neither verb starts containers, and both refuse ambiguous targets as usage errors

Both verbs SHALL resolve with `auto_start=False`.
A stopped project SHALL be a TOON usage error (exit 2) naming `cwcli start`.
A multi-bench project with no `--bench` SHALL be a TOON usage error (exit 2) listing the benches and naming `--bench`.

#### Scenario: A stopped project is a usage error naming cwcli start

- **WHEN** either verb runs against a stopped project
- **THEN** it emits a TOON usage error whose help names `cwcli start`, exits 2, and starts nothing

#### Scenario: A multi-bench project with no selector is a usage error naming --bench

- **WHEN** either verb runs against a multi-bench project with no `--bench`
- **THEN** it emits a TOON usage error listing the benches and naming `--bench`, and exits 2

#### Scenario: An unknown site is a typed error, not a silent success

- **WHEN** `--site` names a site that does not exist in the bench
- **THEN** a typed error is emitted with a help line pointing at `cwcli axi inspect`, and the process exits non-zero

### Requirement: The exit code reads the report, never the envelope status

Both verbs SHALL exit 0 when `report.ok` is true and 1 when it is false, and SHALL NOT derive the exit code from `Result.status`.

#### Scenario: A WARNING-shaped envelope does not report success

- **WHEN** the core returns `Status.WARNING` because a step failed
- **THEN** the verb still exits 1, because the exit code reads `report.ok`

#### Scenario: A lost exec stream is not reported as success

- **WHEN** the exec stream is lost and the exit code is genuinely unknown
- **THEN** the `exec_stream` contract's typed `DOCKER` error surfaces, and the verb SHALL NOT report the operation as successful

### Requirement: The core gains bench_ops and the maintenance helper is promoted, not copied

The system SHALL add `src/caffeinated_whale_cli/core/bench_ops.py` exposing `migrate_site` and `run_tests`, each returning `Result[BenchOpReport]`, importing no `rich`/`questionary`/`typer` and returning no live Docker object across its boundary.

`core/update.py`'s private `_set_maintenance` SHALL be PROMOTED to a shared core helper with byte-identical behaviour and `core.update` re-pointed at it.
A second copy of the maintenance-mode lifecycle SHALL NOT be created.

#### Scenario: The core purity gate stays green

- **WHEN** `tests/test_core_envelope.py` walks the intra-package load-time import graph from `core/bench_ops.py`
- **THEN** no path reaches `rich`, `questionary`, or `typer`

#### Scenario: There is exactly one maintenance-mode implementation

- **WHEN** the source is searched for the maintenance-mode enable/disable logic
- **THEN** exactly one implementation exists, and both `core.update` and `core.bench_ops` call it

#### Scenario: core.update's behaviour is unchanged by the promotion

- **WHEN** `core.update` runs after the promotion
- **THEN** its maintenance-mode enable/disable behaviour, its load-bearing migrate gate, and its report are unchanged

### Requirement: The arbitrary-passthrough deferral is preserved and asserted

`cwcli axi run` and `cwcli axi exec` SHALL remain absent, at the top level and under every subapp, and `tests/test_axi.py::TestNoAxiRunVerb` SHALL remain green and unmodified in substance.

Neither new verb SHALL accept any parameter carrying a free-form command string, shell fragment, or additional bench subcommand.

#### Scenario: The absence assertion still holds after the new verbs land

- **WHEN** the axi registry is enumerated at the top level and under every subapp
- **THEN** `run` and `exec` are absent

#### Scenario: No parameter accepts a command string

- **WHEN** the two new verbs' Typer parameters are enumerated
- **THEN** every one is a typed, individually-quoted value (project, site, bench, app), and none is a variadic or free-form command argument

### Requirement: The new verbs appear in the generated installable skill without a hand-edit

The installable skill's verb table SHALL be produced by `scripts/build_skill.py` walking the live Typer registry, and SHALL list the new verbs with no hand-edit.
`tests/test_axi_skill.py`'s `--check` unit test SHALL keep a stale skill from merging, and its existing absent-verb parametrization (`axi apps install`, `axi apps uninstall`, `axi restore`) SHALL be unchanged.

#### Scenario: The regenerated skill lists the new verbs

- **WHEN** `build_skill.py` regenerates the skill from the Typer registry
- **THEN** the verb table includes the new verbs, the absent-verb notes are unchanged, and the `--check` unit test passes
