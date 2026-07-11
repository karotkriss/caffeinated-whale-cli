## Why

cwcli has no first-class way to manage Frappe apps.
To install, uninstall, or even reliably list apps a user must drop to the raw `cwcli run <project> bench get-app ... && bench --site <site> install-app ...` escape hatch (`commands/run.py:14`), which offers no per-bench/per-site abstraction, no `--json`, and no honest exit codes beyond whatever bench happens to return.
The only structured view of app state is a cross-project search of a possibly-stale SQLite cache (`cwcli where`, `commands/where.py:152`) or the `cwcli inspect` tree - neither is queried live, so both lie after any mutation until a manual `cwcli inspect --update`.
Meanwhile `cwcli update` is a bench/site-aware app operation that lives outside any app noun.
This violates the project's own captain standard that every command must work, and be honest about exit codes, in BOTH interactive and non-interactive modes.

## What Changes

- Add a new `cwcli apps` command group (a Typer sub-app registered in `main.py`, mirroring `config`/`rm`) with four subcommands.
  Each resolves its target bench through the existing shared `resolve_bench_path` (multi-bench safe, `--bench <index|label>`, `--path` escape hatch), each has a `--json` machine-readable mode, and each returns honest non-zero exit codes:
  - `cwcli apps list <project>` - available-in-bench apps (live `ls apps/`) and, when installed apps are requested, apps installed on the target site(s) (live `bench --site <site> list-apps`).
  - `cwcli apps install <project> <app...>` - fetch (`bench get-app`, optional `--branch`) then install on the target site(s) (`bench --site <site> install-app`); each `<app>` is EITHER a known app name OR a git URL passed straight through to `bench get-app`; `--fetch-only` stops after fetching.
  - `cwcli apps uninstall <project> <app...>` - `bench --site <site> uninstall-app`; destructive, gated by the shared `confirm_or_exit`/`--yes` contract; `--remove-from-bench` also deletes the app directory from the bench.
  - `cwcli apps update <project> <app...>` - the CANONICAL app-update path (wraps the existing update flow in `commands/update.py`); updating the `frappe` framework app runs `bench update --reset` instead of the per-app flow.
- **Multi-site by default** for `install` / `uninstall` / `update`: when no `--site` is given the command applies to ALL sites on the resolved bench (fan-out); `--site` is repeatable and narrows to the named site(s).
  The fan-out runs per site, aggregates results, and exits non-zero if ANY site fails, printing a clear per-site report - a partial failure is never hidden behind a success banner.
- **DEPRECATION**: `cwcli update` becomes a deprecated alias for `cwcli apps update`.
  It keeps working but emits a deprecation notice pointing users to `apps update`.
  This change is therefore NOT purely additive - it deprecates existing top-level behavior (no removal yet).
- Every subcommand honors the non-interactive contract: a non-TTY without the required flag refuses with a non-zero exit; auto-start of a stopped instance is gated by `--yes` (via `ensure_containers_running(auto_start=yes)`); the destructive `uninstall` is gated by `confirm_or_exit`/`--yes`.
- After a successful mutation (`install`/`uninstall`/`update`), refresh the bench's cached app lists through the existing cache-writing path (`cache.recache_project`, which routes through `db_utils.cache_project_data` and its `_redact_config_for_cache` chokepoint) so `where`/`open`/`inspect` stop reporting stale data.
- **BREAKING**: none (nothing is removed; `cwcli update` still runs).

## Capabilities

### New Capabilities
- `app-management`: list / install / uninstall / update Frappe apps per bench and (multi-site by default) per site, with app-name-or-git-URL install targets, non-interactive flags, `--json` output, honest aggregated exit codes, post-mutation cache refresh, and a deprecated `cwcli update` alias - reusing the existing bench resolver, confirmation, site-detection, default-site, and container-start primitives.

### Modified Capabilities
- (none - this is the first OpenSpec change in the repo; no existing `openspec/specs/` capabilities change their requirements. The `cwcli update` deprecation is a code change to `commands/update.py`, captured in this new capability's requirements and the Impact below.)

## Impact

- **New**: `src/caffeinated_whale_cli/commands/apps.py`; register the sub-app in `src/caffeinated_whale_cli/main.py` (alongside the `config`/`rm` `add_typer` calls).
- **Modified**: `src/caffeinated_whale_cli/commands/update.py` - emit a deprecation notice on the top-level `cwcli update`, add the `frappe` -> `bench update --reset` special-case, and accept the repeatable `--site` narrowing shared with `apps update`.
- **Reuses (no changes)**: `commands/utils.py` (`resolve_bench_path`, `confirm_or_exit`, `ensure_containers_running`), `utils/bench_sites.py` (`list_sites`, `read_current_site`), `utils/db_utils.py` (`get_default_site`), `utils/cache.py` (`recache_project`), `utils/docker_utils.py` (`get_frappe_container`, `handle_docker_errors`).
- **Left intact**: `commands/open.py`, `commands/where.py`, `commands/run.py`, `commands/init.py` (the erpnext-during-init path).
- **Dependencies**: none added; built entirely from primitives already in the repo.
- **Docs/tests**: new `## apps` section in `README.md`, a note that `cwcli update` is deprecated, and a `CHANGELOG.md` entry; `tests/test_apps.py` covering both interactive and non-interactive modes plus multi-site fan-out aggregation; a real-instance E2E on Frappe v14 + v15.
