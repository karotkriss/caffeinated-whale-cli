## ADDED Requirements

### Requirement: List apps available in a bench
The system SHALL provide `cwcli apps list <project>` that reports the apps available in the resolved bench, read live from the container (`ls apps/`), never from the possibly-stale cache.
The bench SHALL be resolved through the shared `resolve_bench_path` with `on_ambiguous="error"`, so `--bench <index|label>` selects a bench and a multi-bench project with no selector refuses with a non-zero exit rather than silently guessing.

#### Scenario: List available apps in a single-bench project
- **WHEN** a user runs `cwcli apps list myproject` on a project with one bench
- **THEN** the command lists every app directory under that bench's `apps/`, read live from the container, and exits zero

#### Scenario: Multi-bench project with no selector
- **WHEN** a user runs `cwcli apps list myproject` on a project with multiple benches and passes neither `--bench` nor `--path`
- **THEN** the command refuses, lists the available benches, and exits non-zero without reading any bench

### Requirement: List apps installed on sites
The system SHALL, when installed apps are requested for `cwcli apps list <project>` (via `--installed` or one or more `--site <site>`), report the apps installed on the target site(s), read live via `bench --site <site> list-apps`.
Consistent with the multi-site default, when no `--site` is given the target site set SHALL be ALL sites on the resolved bench (from the canonical `bench_sites.list_sites`); `--site` is repeatable and narrows to the named site(s).
The result SHALL be grouped by site so a reader can tell which app is installed where.

#### Scenario: List installed apps for an explicit site
- **WHEN** a user runs `cwcli apps list myproject --site dev.localhost`
- **THEN** the command lists the apps installed on `dev.localhost` via a live `bench --site dev.localhost list-apps` and exits zero

#### Scenario: List installed apps across all sites by default
- **WHEN** a user runs `cwcli apps list myproject --installed` on a bench with multiple sites and no `--site`
- **THEN** the command lists the installed apps for every site on the bench, grouped by site, and exits zero

### Requirement: Install one or more apps
The system SHALL provide `cwcli apps install <project> <app...>` that fetches each app into the bench (`bench get-app`, honoring an optional `--branch`) and then installs it on the target site(s) (`bench --site <site> install-app`).
Each `<app>` SHALL be accepted as EITHER a known app name OR a git URL, passed straight through to `bench get-app` (so custom apps not in bench's registry can be installed).
Installs SHALL be multi-site by default: with no `--site` the app is installed on ALL sites on the resolved bench; `--site` is repeatable and narrows to the named site(s).
`--fetch-only` SHALL fetch without installing on any site.
The command SHALL run every (app, site) step, aggregate the results, and exit non-zero if ANY step fails, printing a clear per-step report and no success banner on any failure.
On overall success it SHALL refresh the bench's cached app lists through the existing cache-writing path so subsequent `where`/`open`/`inspect` reflect the new state.

#### Scenario: Install an app on the default (all) sites and refresh the cache
- **WHEN** a user runs `cwcli apps install myproject payments` on a bench whose sites all succeed
- **THEN** the command runs `bench get-app payments` then `bench --site <site> install-app payments` for every site on the bench, refreshes the cached app lists, prints a success message, and exits zero

#### Scenario: Install a custom app from a git URL
- **WHEN** a user runs `cwcli apps install myproject https://github.com/example/custom_app --site dev.localhost`
- **THEN** the command passes the git URL to `bench get-app` and then installs the fetched app on `dev.localhost`, and exits zero on success

#### Scenario: One site fails during a multi-site install
- **WHEN** a user runs `cwcli apps install myproject payments` (no `--site`) and `install-app` succeeds on one site but fails on another
- **THEN** the command reports the per-site outcome, prints no success banner, and exits non-zero

#### Scenario: Fetch only, no install
- **WHEN** a user runs `cwcli apps install myproject payments --fetch-only`
- **THEN** the command runs `bench get-app payments` and does NOT run any `install-app`, then exits zero

### Requirement: Uninstall one or more apps
The system SHALL provide `cwcli apps uninstall <project> <app...>` that removes an app from the target site(s) (`bench --site <site> uninstall-app`).
Uninstalls SHALL be multi-site by default: with no `--site` the app is removed from ALL sites on the resolved bench; `--site` is repeatable and narrows to the named site(s).
Because this destroys site data, it SHALL be gated by the shared destructive-confirmation contract (`confirm_or_exit`): `--yes` proceeds, an interactive TTY prompts, and a non-TTY without `--yes` refuses with a non-zero exit.
`--remove-from-bench` SHALL additionally remove the app directory from the bench after the site uninstalls.
The command SHALL run every (app, site) step, aggregate the results, exit non-zero if ANY step fails with a per-step report, and on overall success refresh the bench's cached app lists.

#### Scenario: Non-interactive uninstall without confirmation flag
- **WHEN** a user runs `cwcli apps uninstall myproject payments --site dev.localhost` from a non-TTY without `--yes`
- **THEN** the command refuses and exits non-zero without uninstalling anything

#### Scenario: Confirmed multi-site uninstall refreshes the cache
- **WHEN** a user runs `cwcli apps uninstall myproject payments --yes` (no `--site`) and the bench command succeeds on every site
- **THEN** the command runs `bench --site <site> uninstall-app payments` for every site on the bench, refreshes the cached app lists, and exits zero

### Requirement: Update one or more apps
The system SHALL provide `cwcli apps update <project> <app...>` as the CANONICAL app-update path, wrapping the existing update flow (`commands/update.py`) so app updates are reachable under the `apps` noun without duplicating logic.
Updating the `frappe` framework app SHALL run `bench update --reset` instead of the normal per-app update flow; other apps SHALL use the normal flow.
Updates SHALL honor `--site` (repeatable) to narrow the sites migrated; with no `--site` the existing all-affected-sites behavior applies.
When no app is given the command SHALL refuse with a non-zero exit.

#### Scenario: Update a normal app through the apps group
- **WHEN** a user runs `cwcli apps update myproject erpnext`
- **THEN** the command performs the normal per-app update and migrate flow for `erpnext` and exits with that flow's exit code

#### Scenario: Update the frappe framework app
- **WHEN** a user runs `cwcli apps update myproject frappe`
- **THEN** the command runs `bench update --reset` rather than the per-app flow, and exits with its exit code

#### Scenario: Update with no app named
- **WHEN** a user runs `cwcli apps update myproject` with no app argument
- **THEN** the command reports that at least one app is required and exits non-zero

### Requirement: Deprecate the top-level update command
The system SHALL keep `cwcli update` working as a deprecated alias for `cwcli apps update`.
When invoked, `cwcli update` SHALL emit a deprecation notice pointing the user to `cwcli apps update`, then perform the same update (including the `frappe` -> `bench update --reset` special-case), so no existing script breaks.

#### Scenario: Deprecated cwcli update still works and warns
- **WHEN** a user runs `cwcli update myproject --app erpnext`
- **THEN** the command prints a deprecation notice recommending `cwcli apps update`, performs the update, and exits with the update flow's exit code

### Requirement: Non-interactive output and auto-start contract
Every `cwcli apps` subcommand SHALL support `--json` for machine-readable output and SHALL honor the project's auto-start contract: a stopped instance is auto-started only with `--yes` (via `ensure_containers_running(auto_start=yes)`), and a non-TTY without `--yes` against a stopped instance refuses with a non-zero exit rather than hanging or silently proceeding.
For the multi-site mutating subcommands the `--json` output SHALL include the per-(app, site) results so automation can parse a partial failure.

#### Scenario: JSON output for a list
- **WHEN** a user runs `cwcli apps list myproject --json`
- **THEN** the command prints a JSON document describing the bench and its available (and, if requested, per-site installed) apps, and prints no decorative table

#### Scenario: JSON output reports per-site results on partial failure
- **WHEN** a user runs `cwcli apps install myproject payments --json` (no `--site`) and one site fails
- **THEN** the JSON output includes a per-(app, site) result list marking which succeeded and which failed, and the command exits non-zero

#### Scenario: Stopped instance, non-TTY, no --yes
- **WHEN** a user runs any `cwcli apps` subcommand against a stopped instance from a non-TTY without `--yes`
- **THEN** the command refuses to auto-start, reports how to start it, and exits non-zero
