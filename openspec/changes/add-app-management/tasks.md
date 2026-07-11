## 1. Scaffold the apps command group

- [x] 1.1 Create `src/caffeinated_whale_cli/commands/apps.py` with a `typer.Typer()` sub-app and shared helpers (frappe-container fetch, streamed exec with honest exit code, captured exec, target-site resolution).
- [x] 1.2 Register the sub-app in `src/caffeinated_whale_cli/main.py` via `app.add_typer(apps_cmd.app, name="apps")`, alongside `config`/`rm`.
- [x] 1.3 Wire the shared auto-start guard: each subcommand calls `ensure_containers_running(require_running=True, auto_start=yes)` so a stopped instance non-TTY-without-`--yes` refuses.
- [x] 1.4 Add the shared site-set helper: `--site` (repeatable) narrows; no `--site` fans out to ALL sites from `bench_sites.list_sites` on the resolved bench.

## 2. apps list

- [x] 2.1 Implement `apps list <project>`: resolve the bench via `resolve_bench_path(on_ambiguous="error")`, read available apps live (`ls apps/`).
- [x] 2.2 Add `--site`/`--installed`: read installed apps live (`bench --site <site> list-apps`) across the resolved site set (all sites by default), grouped by site.
- [x] 2.3 Add `--json` output (mirror `where.py`), suppressing the decorative table.

## 3. apps install

- [x] 3.1 Implement `apps install <project> <app...>`: `bench get-app` (honor `--branch`) per app, accepting an app name OR a git URL; derive the installed app name from the fetched `apps/` dir.
- [x] 3.2 Fan out `bench --site <site> install-app` over the resolved site set (all sites by default); `--fetch-only` fetches without installing.
- [x] 3.3 Continue-and-report-all aggregation: run every (app, site) step, print a per-step report, honest non-zero exit if any failed, NO success banner on failure.
- [x] 3.4 On overall success, refresh the cache via `cache.recache_project` (routes through the redaction chokepoint); `--json` includes per-(app, site) results.

## 4. apps uninstall

- [x] 4.1 Implement `apps uninstall <project> <app...>`: single up-front destructive gate via `confirm_or_exit(assume_yes=yes, ...)` naming the target sites; fan out `bench --site <site> uninstall-app` over the resolved site set (all sites by default).
- [x] 4.2 Continue-and-report-all aggregation with honest non-zero exit and per-(app, site) report.
- [x] 4.3 Non-TTY-without-`--yes` refuses non-zero; on overall success refresh the cache and emit `--json` per-(app, site) results.

## 5. apps update + deprecate cwcli update

- [x] 5.1 Implement `apps update <project> <app...>` as the canonical path wrapping `commands/update.py`'s flow; refuse non-zero when no app is given; honor repeatable `--site` narrowing.
- [x] 5.2 Special-case the `frappe` app: run `bench update --reset` instead of the per-app flow (in the shared update logic so both paths get it).
- [x] 5.3 Make `cwcli update` a deprecated alias: emit a deprecation notice pointing to `cwcli apps update`, then run the same flow (keep it working).

## 6. Tests

- [x] 6.1 Add `tests/test_apps.py` with a fake frappe container (records exec calls, serves `ls apps`/`list-apps` output, honors the site probe patterns, per-site exit-code control).
- [x] 6.2 Cover both modes for every subcommand: interactive prompt + non-interactive flags; assert non-TTY-without-flag refuses non-zero; assert multi-bench-no-selector refuses; assert `--json` shape.
- [x] 6.3 Cover multi-site fan-out: all-sites default, `--site` narrowing, one-site-fails → non-zero with per-(app, site) report and no success banner; git-URL install target; `frappe` → `bench update --reset`; deprecated `cwcli update` still works and warns.
- [x] 6.4 Assert post-mutation cache refresh goes through `cache.recache_project` (no direct cache write, no secret persisted).

## 7. Docs and E2E

- [x] 7.1 Add a `## apps` section to `README.md` (including multi-site default, git-URL install, `frappe` update behavior), mark `cwcli update` as deprecated, and add a `CHANGELOG.md` entry (Keep a Changelog format, user-facing only).
- [x] 7.2 Run the real-instance E2E on Frappe v14 + v15, both interactive and non-interactive, exercising multi-site fan-out and the deprecation notice, per the CLAUDE.md recipe; capture evidence under `docs/e2e/`.
