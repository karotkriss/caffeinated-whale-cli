## ADDED Requirements

### Requirement: apps list, install, and uninstall are UI-pure core functions

The system SHALL provide `core.list_apps`, `core.install_apps`, and `core.uninstall_apps` in `core/apps.py`, each returning the typed `Result` envelope and raising `CwcliError` for hard failures.
They SHALL import no `rich`, `questionary`, or `typer`, SHALL never prompt, and SHALL never write to stdout.
No live Docker object SHALL cross their return boundary.
`commands/apps.py` SHALL become a renderer over them, preserving every flag, message, and exit code byte-for-byte.

#### Scenario: The core renders nothing itself

- **WHEN** any of the three core functions runs against a fake container
- **THEN** nothing is written to stdout or stderr, and the outcome is carried entirely by the returned `Result`

#### Scenario: A decision the core cannot make is returned, not prompted

- **WHEN** `core.list_apps` targets a multi-bench project with no `--bench` selector
- **THEN** it returns `NEEDS_CHOICE` with a `select_bench` choice and does not act

#### Scenario: The human CLI contract is unchanged

- **WHEN** the existing `tests/test_apps.py` suite runs against the re-seated `commands/apps.py`
- **THEN** it passes unchanged, including every `--json` shape and exit code

### Requirement: A partial fan-out failure exits non-zero

`core.install_apps` and `core.uninstall_apps` SHALL fan out over the target sites, collect a per-(app, site) result, and set `AppsReport.ok` to false if ANY step failed.
The frontend's exit code SHALL be derived from `report.ok`, NOT from `result.status`: a partial failure is a `WARNING`-shaped envelope, and `WARNING` maps to exit 0 everywhere else.
`core.list_apps` SHALL apply the same rule via `AppsListing.ok`, which is false if any site's installed-apps read failed.

#### Scenario: Half a fan-out failing is not success

- **WHEN** an uninstall removes an app from one site and fails on another
- **THEN** the report names both outcomes, its `ok` is false, and the command exits 1

#### Scenario: A failed site read is reported and still emits its document

- **WHEN** `cwcli apps list --installed --json` cannot read installed apps for one site
- **THEN** that site's entry is null, the JSON document is emitted on stdout, and the command then exits 1

### Requirement: The core does not refresh the cache

`core.install_apps` and `core.uninstall_apps` SHALL NOT call `cache.recache_project`, and `core/apps.py` SHALL NOT import from `commands/`.
The post-mutation cache refresh SHALL be performed by the frontend after the core returns, gated on any result having succeeded.

#### Scenario: The core-to-CLI reach stays confined to core/update.py

- **WHEN** `core/apps.py` is imported
- **THEN** it pulls in nothing from the `commands` package, and `core/update.py` remains the only core module that reaches back into the CLI layer

#### Scenario: A successful mutation still refreshes the cache

- **WHEN** `cwcli apps install` installs an app on at least one site
- **THEN** the frontend refreshes the cache, and a refresh failure degrades to a warning rather than failing the command

### Requirement: The destructive uninstall gate is a returned choice

`core.uninstall_apps` SHALL take auto-start and destructive consent as separate parameters, and SHALL return `NEEDS_CHOICE` with a `confirm_uninstall` choice when consent has not been given.
`cwcli apps uninstall --yes` SHALL retain its current fused meaning (skip the confirmation AND auto-start containers), unchanged.

#### Scenario: The core refuses to destroy data without explicit consent

- **WHEN** `core.uninstall_apps` is called without destructive consent
- **THEN** it returns `NEEDS_CHOICE`/`confirm_uninstall` and no `uninstall-app` command is executed

#### Scenario: JSON mode refuses rather than prompting

- **WHEN** `cwcli apps uninstall --json` runs without `--yes`
- **THEN** it refuses with a non-zero exit and no prompt is shown
