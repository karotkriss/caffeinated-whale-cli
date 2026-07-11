## Context

App state in cwcli today is split across four places, none of which can install or uninstall:

- **The raw escape hatch** `cwcli run <project> <bench args...>` (`commands/run.py:14`) - passes arbitrary bench args, exit code straight from bench, no per-bench/per-site abstraction, no structured output.
- **`cwcli where`** (`commands/where.py:152`) - a cross-project search of the SQLite cache (`_search_apps`), read-only and potentially stale (never queried live); has `--json`.
- **`cwcli inspect`** - populates the cache's `available_apps` / per-site `installed_apps`, but reading it back can be stale until `--update`.
- **`cwcli update --app`** (`commands/update.py:869`) - updates/pulls + migrate affected sites + optional `bench build --app`; bench/site aware, but cannot install or uninstall, and lives outside any app noun.
- **`cwcli open --app`** (`commands/open.py:30`) - opens an app dir in an editor; validates the name against the possibly-stale cache.
- **`cwcli init --install-erpnext`** (`commands/init.py:1045`) - the one place that runs `bench get-app` + `install-app`, hardcoded to erpnext during init only.

The building blocks for a cohesive command already exist: `resolve_bench_path`, `confirm_or_exit`, `ensure_containers_running` (`commands/utils.py`), `bench_sites.list_sites`/`read_current_site`, `db_utils.get_default_site`, `cache.recache_project`, and the streaming/capturing exec patterns in `run.py`/`update.py`.
The gap is that no command ties them together under an `apps` noun, and `cwcli update` is the only app-aware operation but sits at the top level.

## Goals / Non-Goals

**Goals:**
- A cohesive `cwcli apps` group (list / install / uninstall / update) built entirely from existing primitives, no new dependencies and no new data model.
- Full parity with the project's captain standards: interactive AND non-interactive modes, `--json`, honest non-zero exit codes, destructive-action gating, auto-start gated by `--yes`.
- **Multi-site by default** for install / uninstall / update, with honest aggregated exit codes across the fan-out.
- `apps update` is the canonical app-update path; `cwcli update` becomes a deprecated alias that keeps working.
- After a mutation, the cache is refreshed through the existing writer so `where`/`open`/`inspect` stop lying.

**Non-Goals:**
- Removing `cwcli update` (deprecated, not removed - existing scripts keep working).
- A new app-state data model or new cache columns (the existing `available_apps`/`installed_apps` cache is sufficient).
- A new "active vs disabled" site distinction (fan-out uses the one canonical `bench_sites.list_sites`, same site set everything else in the codebase uses).
- Reimplementing site detection, default-site logic, or the update flow (reuse `bench_sites` / `get_default_site` / `commands/update.py`).

## Decisions

### 1. A Typer sub-app (`add_typer`), not flat `apps-*` commands
`apps` is registered as its own `typer.Typer()` and mounted with `app.add_typer(apps_cmd.app, name="apps")`, exactly like `config`/`rm`/`start`.
Alternative considered: flat top-level commands (`apps-list`, `apps-install`). Rejected - the sub-app groups the noun cleanly, gives `cwcli apps --help`, and matches the established registration pattern in `main.py`.

### 2. Bench resolution via `resolve_bench_path(..., on_ambiguous="error")`
Every subcommand takes `--bench <index|label>` and `--path`, and resolves through the shared `resolve_bench_path`, falling back to `/workspace/frappe-bench` only when there is no cache (matching `run.py`).
`on_ambiguous="error"` (the data-op default) means a multi-bench project with no selector refuses instead of guessing.
Alternative considered: `on_ambiguous="first"` (like `start`). Rejected - installing/uninstalling into an arbitrary bench is a data-safety hazard; a data op must be explicit.

### 3. Multi-site fan-out by default; `--site` (repeatable) narrows
The mutating subcommands (`install`/`uninstall`) and `update` are multi-site by default: with no `--site` the target site set is ALL sites on the resolved bench, taken from the canonical `bench_sites.list_sites`. `--site` is a repeatable option that narrows to the named site(s).
`apps list` follows the same site-set model when installed apps are requested (grouped by site).
Alternative considered: the original "default site only" model via `get_default_site`. Rejected per the captain's decision - multi-site is the default, matching the (now-deprecated) `cwcli update`'s bench/site-aware fan-out. `get_default_site` is no longer the primary path, though `read_current_site`/`get_default_site` remain available if a future single-site default is wanted.
Disabled/inactive sites are NOT special-cased: the fan-out targets exactly `list_sites`, keeping one consistent site set across `rm`/`inspect`/`apps` (see Non-Goals).

### 4. Live reads for state, cache refresh through the existing writer for persistence
`apps list` reads live (`ls apps/`, `bench --site list-apps`) so it never lies. After a successful mutation the command calls `cache.recache_project(project)` - a full inspect that persists through `db_utils.cache_project_data`, so the `_redact_config_for_cache` whitelist chokepoint still fires and no secret is ever written.
Alternative considered: `partial_inspect_known_benches` for the refresh. Rejected for the persist step - it is READ-ONLY and never writes the cache, so it cannot make `where`/`open`/`inspect` honest; and it cannot refresh per-site `installed_apps` (which is exactly what an install/uninstall changes). A full recache is both correct and necessary here.

### 5. Exec pattern + honest aggregated exit (continue-and-report-all)
`list` uses `container.exec_run` and captures output (like `inspect._get_available_apps`).
`install`/`uninstall` stream bench's live output (like `run.py`/`update.py`'s `exec_create`/`exec_start`) and read the real `ExitCode`.
Across the fan-out the model is **continue-and-report-all** (the captain's aggregation discipline): the command runs every (app, site) step, collects each outcome, prints a per-step report, and exits non-zero if ANY step failed - never a success banner over a partial failure. (Exception: if `bench get-app` for an app fails at the bench level, its per-site installs are skipped and that app is recorded as failed, but the run continues to the next app.)
Alternative considered: stop-on-first-failure. Rejected - it hides the state of sites never attempted; aggregate-and-report is both honest and more useful, and is what "run per site, aggregate the results, per-site report" means.

### 6. `apps update` is canonical; `cwcli update` is a deprecated alias; `frappe` is special-cased
`apps update` wraps the existing update implementation in `commands/update.py` (one source of truth for the maintenance-mode/migrate/build flow). `cwcli update` keeps working but emits a deprecation notice pointing to `apps update`, then runs the same flow - so no script breaks.
Updating the `frappe` framework app runs `bench update --reset` (a bench-wide reset+update+migrate+build) instead of the per-app git-pull flow; other apps use the normal flow. The special-case lives in the shared update logic so both `apps update` and the deprecated `cwcli update` get it.
Alternative considered: keeping `update` first-class with no deprecation, and no frappe special-case. Rejected per the captain's decision.

### 7. `--json` mirrors `where`
`--json` prints `json.dumps(..., indent=2)` via `typer.echo` and suppresses decorative tables, exactly like `where.py`. For the multi-site mutating subcommands, `--json` emits the per-(app, site) result list (app, site, action, ok) so automation can parse a partial failure.

### 8. Destructive gate via `confirm_or_exit`
`uninstall` is the only destructive subcommand; it calls `confirm_or_exit(..., assume_yes=yes, refuse_message=...)` once up front (the confirmation names the sites it will fan out over), inheriting the `--yes`/non-TTY/decline contract verbatim.
Auto-start for all subcommands uses `ensure_containers_running(require_running=True, auto_start=yes)`, so a stopped instance non-TTY-without-`--yes` refuses (already the shared contract).
`install` is additive (adds an app to a site), so it is not destructive-gated even though it fans out by default.

### 9. `<app>` is an app name OR a git URL
Each `<app>` argument is passed straight through to `bench get-app`, which already accepts either a registered app name or a git URL. No client-side parsing or validation of the URL - bench owns that. The installed-name used for the per-site `install-app` and the cache refresh is derived from bench's fetch result (the app dir that appears under `apps/`), not assumed equal to the URL.

## Risks / Trade-offs

- **`bench --site list-apps` is slow (boots Frappe)** → scope it to when the user actually asks for installed apps; plain `apps list` only does the cheap `ls apps/`. Fanning it across all sites is opt-in (via `--installed`/`--site`).
- **Multi-site-by-default install/uninstall touches every site** → the captain's explicit choice; uninstall's `confirm_or_exit` names the sites so an interactive user sees the blast radius, and `--site` narrows it. Install is additive so it is not gated, but its `--json`/report shows exactly what happened per site.
- **Partial multi-site (or multi-app) mutation leaves the bench half-changed** → continue-and-report-all + honest non-zero exit + no success banner + per-step report, so the user knows exactly which (app, site) pairs need attention; no rollback (bench has no transactional install).
- **Cache refresh must not bypass secret redaction** → refresh only through `cache.recache_project` → `cache_project_data` → `_redact_config_for_cache`; never write app state to the cache directly.
- **Uninstall is irreversible (drops app tables from the site DB)** → hard destructive gate via `confirm_or_exit`; a non-TTY must pass `--yes` deliberately.
- **Deprecating `cwcli update` could surprise scripts** → it is deprecated, not removed: it still runs and just warns, so existing automation keeps working while users migrate.
- **`bench update --reset` for `frappe` is heavier than a per-app update** → it is the correct way to update the framework app; it is only triggered when the user explicitly names `frappe`.

## Migration Plan

Additive plus one deprecation (no removal). No data migration, no schema change, no config change.
`cwcli update` keeps working (with a notice), so nothing breaks on upgrade.
Rollback is deleting `commands/apps.py`, reverting the `commands/update.py` deprecation/frappe changes, and removing the one registration line in `main.py`.

## Open Questions

- When (if ever) to fully REMOVE `cwcli update` after the deprecation period? Deferred - out of scope; this change only deprecates.
- Whether `apps list` (non-installed) should also gain a multi-bench fan-out. Deferred - `--bench` selection is sufficient for now.
