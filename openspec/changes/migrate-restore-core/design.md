## Context

Batch 11 of the logic-core rework, and one of the two commands (`restore`, `rm`) the plan named for the destructive-preview boundary it deferred.
No separate recon preceded this change: this design owns both the audit and the proposal, and every line reference below was read at current HEAD rather than inherited from prior prose (batch 1's Non-Goals error is the named anti-pattern).
`restore --receive` is the most destructive path in the codebase; the `cwcli-lifecycle` skill's `references/restore.md` holds the six shipped bugs this migration must preserve to the byte.

### Audit: what `restore` actually is at HEAD (2101 lines, three modes)

| Lines | What | Migrates? |
| --- | --- | --- |
| `25-82` | `parse_backup_filename`, `transform_site_name_to_backup_format` (pure) | **Yes.** Pure logic, move to core unchanged |
| `85-147` | `_prompt_mariadb_credentials` (questionary username/password, both modes) | **No.** TTY-coupled secret UX stays frontend; the core takes the resolved credentials as `restore_apply` params |
| `150-173` | `_resolve_default_site` (cache + LIVE `currentsite.txt` fallback) | **Yes, kept private.** Stricter than `resolvers.resolve_default_site`; the reported flat spot (Decision 8) |
| `176-250` | `_post_restore_migrate_and_restart` (`bench migrate` + `_start_project(restart=True)`) | **Yes.** Migrate is a buffered exec; the restart becomes `core.start(restart=True)` (Decision 5) |
| `253-410` | `scan_backups_for_all_sites`, `group_backup_sets`, `group_and_sort_backups` | **Yes.** Container-read + pure grouping, move to core |
| `413-525` | `_read_backup_installed_apps`, `check_missing_apps` (read the BACKUP's dump, live `ls apps`) | **Yes.** Move to core; `no_recache` stays a deprecated no-op param |
| `528-578` | `select_backup_set` (pure selector; one `console.print` note on multi-match) | **Yes.** Move to core; the print becomes a `Result.warnings` message (Decision 3) |
| `581-681` | `display_backup_selection_menu` (questionary select, separators, badges, the remote sentinel) | **No.** Rich menu UX stays frontend, rendered from the `select_backup` choice (Decision 3) |
| `684-922` | `restore_send_mode` (bench fallback, scan, menu, copy-out, sendme send, Ctrl-C wait) | **Split.** Scan/copy-out to the core; the sendme subprocess + menu + clipboard + Ctrl-C stay frontend (Decision 6) |
| `925-1461` | `restore_receive_mode` (ensure running, site, ticket prompt, sendme receive, copy-in, missing-apps, confirm, restore, migrate) | **Split.** Copy-in + missing-apps + confirm + restore + migrate to the core (`receive_plan` -> `restore_apply`); the ticket prompt + sendme receive stay frontend (Decision 6) |
| `1464-1548` | The Typer signature (13 params) | **No.** Frontend |
| `1570-1643` | Mutual-exclusion validation, `resolve_bench_path`, send/receive dispatch | **Split.** Flag UX stays frontend; the dispatch calls the core |
| `1645-1696` | Bench fallback (copy #3 of the no-cache populate) | **Yes.** The `open_plan` fallback-populate pattern, once in the core |
| `1697-1765` | Default site, site/bench metachar validation, bench/site `test -d` probes | **Yes.** `restore_plan`, over the shared validators + the kept default-site helper |
| `1767-1808` | Scan, `--latest`/`--backup-file` selection, non-TTY refusal, the menu | **Split.** Scan + selection in `restore_plan`; the menu + non-TTY-ness is the frontend's fact (`select_backup`) |
| `1810-1831` | The remote-restore sentinel diverting to receive | **No.** A frontend menu affordance; the core never sees it (Decision 3) |
| `1833-1932` | Missing-apps gate, destructive confirm | **Split.** The facts (missing apps, filename, contents, origin) ride the plan; the two gates render frontend and grant `consent` (Decision 4) |
| `1934-1965` | Credentials (after confirm), username metachar validation, dump `test -f` | **Split.** Credential prompts stay frontend; the metachar guard + `test -f` move to `restore_apply` |
| `1967-2101` | Build restore command (secrets in env), exec, encryption-key merge, migrate + restart, failure message | **Yes.** `restore_apply` (Decision 5) |

## Goals / Non-Goals

**Goals.**
Move restore's destructive core and all resolve logic behind typed envelopes; settle the plan/apply boundary the rework deferred here, justified from restore's actual behavior; preserve every observable behavior including the destructive confirm on both paths, the non-interactive selectors, secrets off the argv, the streamed copies, the encryption-key merge, and post-restore migrate + restart; keep the both-modes contract byte-identical.

**Non-Goals.**
Any behavior change to the confirms, selectors, secret transport, sendme flows, or exit codes.
Adding a `--ticket` flag so `--receive` runs fully non-interactively (a PRE-EXISTING both-modes gap this migration preserves; adding the flag is a behavior addition deferred to its own change).
Fixing `db_utils.cache_project_data`'s non-transactionality (the standing hazards-board item; restore only reads the cache and calls `clear_cache_for_project` via `core.inspect`).
An `axi restore` verb (Decision 9: deferred as the captain's own product decision, absence asserted).
Randomizing or changing any credential defaults.

## Decisions

### 1. The headline finding: restore is a plan/apply split, and why

The core-rework plan deferred the destructive-preview (plan/apply) boundary to `restore`/`rm`; this batch settles it for `restore`, and the answer is **yes, a plan/apply split**, justified from restore's actual behavior rather than from the prophecy.

`core.restore_plan(...) -> Result[RestorePlan]` resolves everything read-only (container, bench, site, scan, select, missing apps, origin) and returns a `RestorePlan` describing the destructive action.
`core.restore_apply(plan, *, mariadb_root_username, mariadb_root_password, admin_password=None, consent=False, no_migrate=False, on_event=None) -> Result[RestoreReport]` performs the single guarded destructive act.
Both are plain functions with the `OnEvent` callback (the `core.update`/`core.inspect` shape); neither is a generator.

**Why a split and not the single-function-with-consent shape (`apps uninstall` / `config cache clear`).**
The consent-parameter model collapses "preview then confirm" into one re-invoked function, and it is right when the resolve is CHEAP and PURE (uninstall: list apps; clear_cache: trivial). Restore's resolve is neither:

- It is a multi-step read-only PIPELINE - resolve container/bench/site, scan every backup across every site (N+2 execs), then `zcat -f | grep` the chosen dump for its installed apps and `ls` the bench's `apps/` - producing a rich destructive-action PREVIEW (filename, timestamp, contents, missing apps, origin mismatch).
- It is followed by SEVERAL frontend interactions before the single destructive apply: the interactive backup MENU (itself a decision the core cannot make), the missing-apps gate, the destructive gate, and the credential collection.

Folding all of that into one re-invoked `restore(..., consent=...)` re-runs the whole scan + missing-apps pipeline on every interaction round-trip (up to three times on the interactive path) and forces the selected backup plus all interaction state to round-trip as parameters.
More importantly, it welds the READ (scan/preview) and the DESTROY (`bench restore --force`) into one function that takes the site-dropping secrets even when a caller only wants to know what would happen.

**The split is a read/destroy safety separation.**
For the most destructive command in the codebase, separating the pure, repeatable, secret-free PREVIEW (`restore_plan`) from the single guarded DESTRUCTIVE act (`restore_apply`, which alone takes the credentials and the consent) is a virtue in its own right - it is exactly the "preview the destructive action before applying" the rework named. It also gives a future read-only `axi` verb (a "what would restore do") a safe function to call with no destructive capability, which is part of why no mutating `axi restore` ships (Decision 9).

**What it is NOT.**
Not `run_plan`/`run_stream`'s generator laziness (nothing here returns an iterator).
Not the removal of consent from the core: `restore_apply(consent=False)` returns `NEEDS_CHOICE`/`confirm_restore`, so a GUI/axi cannot bypass the destructive gate - the plan/apply split governs WHERE the expensive resolve lives (once, in `plan`), not whether the core enforces consent.

### 2. The interactive menu is a `select_backup` choice, resolved by re-invoking `restore_plan`

`restore_plan` resolves the backup set in this order: an explicit `selected_backup` (a database full-path handed back from the menu), then `latest`, then `backup_file`, then - if none is given - it returns `NEEDS_CHOICE`/`select_backup` carrying the target-site and other-site candidate sets (as serializable option dicts: value = the database full path, plus label/group/badges/timestamp so the frontend rebuilds today's rich menu).
The frontend renders the menu OUTSIDE any spinner (the spinner-over-questionary deadlock rule), lets the user pick, and re-invokes `restore_plan(selected_backup=<db path>)`; on a non-TTY with no selector it refuses with `USAGE` naming `--latest`/`--backup-file` (today's exact message), the `select_editor`/`confirm_reuse_bench` non-TTY precedent (the core returns the choice; TTY-ness is the frontend's fact).
The re-invoke re-scans - accepted, because it only happens on the human-interactive path where a sub-second re-scan is invisible; the selector paths (`--latest`/`--backup-file`) resolve `restore_plan` in one call.
The "Restore from remote source (via sendme)" menu entry is a FRONTEND affordance the frontend appends and, on selection, diverts to the receive flow; the core never models the old `{"_restore_from_ticket": True}` sentinel.

### 3. The two gates ride the plan; consent is one core parameter

`restore_plan` computes `missing_apps` and `origin_mismatch` into the `RestorePlan`.
The frontend renders BOTH of today's gates from those facts, byte-for-byte: the missing-apps warning + "continue anyway?" confirm (only when `plan.missing_apps` is non-empty) and then the destructive "⚠ This will replace all data in site '{site}'" warning + confirm, each honoring `--yes` (proceed), refusing on a non-TTY without `--yes` (exit 1, today's messages), and keeping `auto_enter=False` (the load-bearing trailing-Enter fix - `commands/utils.py:confirm_or_exit` still omits it, so it is NOT used here).
Both gates gate the SAME destructive act, so the core needs ONE consent: `restore_apply(consent=False)` returns `NEEDS_CHOICE`/`confirm_restore`; the frontend renders its two prompts and, only when both pass, calls `restore_apply(consent=True)`.
`select_backup_set`'s multi-match `console.print` note becomes a `Result.warnings` message the frontend renders (the print cannot live in the core).

### 4. The destructive execs stay buffered; secrets ride `environment=` byte-exactly

`restore_apply` runs `bench --site <site> restore <db-path> --mariadb-root-username <u> --mariadb-root-password "$CWCLI_MARIADB_ROOT_PASSWORD" --force [--admin-password "$CWCLI_ADMIN_PASSWORD"] [--with-public-files ...] [--with-private-files ...]` via `frappe_container.exec_run(["sh", "-c", cmd], workdir=bench_path, environment=restore_env)`, exactly as `restore.py:1970-2001` does today.
This is the `backup`/`unlock` buffered-exec shape, NOT the exec-stream contract: a buffered `exec_run` blocks to completion and returns a real `int` exit code, so it is already honest; the contract's polling exists for the STREAMING consumers (`run`/`update`/`init`) that read `exec_inspect` mid-flight, which restore never did.
The list form `["sh", "-c", cmd]` is REQUIRED (docker-py would `shlex.split` a bare string and skip the shell that expands `$CWCLI_*`); the secret is in `environment=`, never in `cmd`, so it never reaches the container process list (M5).
`bench migrate` stays a buffered `exec_run` under a spinner (today's shape), and a Docker/API exception from it is caught and treated as a failed-but-reported migrate (`exit_code=1`) that STILL restarts.
The post-restore restart becomes `core.start(project_name, bench_path=plan.bench_path, restart=True)` (the `_start_project(restart=True, bench_path_override=...)` -> `core.start` mapping, restarting the SAME bench that was restored, never bench 0).

### 5. `RestoreReport` and the migrate-failure exit code

`RestoreReport` (frozen/slots/kw_only, plain data, NO password field): `site`, `bench_path`, `restored` (True on the OK path), `included_files`, `encryption_key_updated: bool | None`, `migrate_ran: bool`, `migrate_ok: bool`, `restarted: bool`.
Today a failed migrate does NOT undo the restore, is surfaced, still restarts, but returns `migrate_ok=False` so the caller `raise typer.Exit(1)`.
In the core that is a `WARNING`-shaped envelope carrying `migrate_ok=False` (the restore succeeded; the schema may lag): `restore_apply` returns `Result(WARNING, report, warnings=[...])`, and the frontend exits 1 when `report.migrate_ran and not report.migrate_ok` - the `core.update` `report.ok`-drives-the-exit-code precedent, NOT `result.status` (a `WARNING` maps to exit 0 for every other verb).
A hard `bench restore --force` failure raises `CwcliError(PRECONDITION)` with today's failure banner in `detail`; the frontend renders today's "Common causes" list.
The encryption-key merge (read `site_config_backup`, merge `encryption_key` into `site_config.json`, write back via heredoc) is a best-effort core step whose failures are warnings, exactly as today (both paths' near-identical blocks fuse into one core helper).

### 6. sendme send/receive: container I/O to the core, the interactive subprocess stays frontend

The sendme SEND and RECEIVE subprocesses are interactive host I/O - send prints the ticket, copies it to the clipboard, and waits for Ctrl-C; receive prompts for the ticket and downloads - and belong in the frontend, the `commands/logs.py` `-it tail` precedent (interactive host I/O the core deliberately does not consume, disclosed and matched, not assumed).
What moves to the core is the CONTAINER I/O and logic around them:

- **Send**: `restore_plan`'s scan/grouping is reused (send groups all-as-other and has no selector, so it calls a thin `scan_backups` + the pure `group_and_sort_backups`), and a `copy_backup_files_out(container, file_paths, dest_dir)` core helper performs the streamed `get_archive` + tar extract. The frontend menus, runs sendme send, handles the clipboard and the Ctrl-C wait.
- **Receive**: the frontend prompts for the ticket, runs `sendme receive` into a tempdir, then calls `core.receive_plan(project_name, *, site=None, bench_path=None, downloaded_files=[...], on_event=None) -> Result[RestorePlan]`, which copies the files INTO the container's backup dir (streamed `put_archive`, the M4 checked-bool copy), identifies which is database/files/private/config (`parse_backup_filename`), resolves the default site, checks missing apps, computes the origin mismatch, and returns a `RestorePlan`. The frontend then renders the gates and calls `restore_apply` - the SAME apply as the normal path.

`receive_plan` and `restore_plan` share a private `_build_plan(container, project, site, bench_path, db_path, files_path, private_path, config_path)` tail (missing-apps + origin + DTO) so the two entry points cannot drift on what the plan carries.

### 7. Bench resolution and the no-cache fallback, once

All three modes' "cached bench? resolve; else populate via inspect, re-resolve; else default" collapses to the `core.open_plan` fallback-populate pattern inside `restore_plan`/`receive_plan`/`scan_backups`: `resolvers.resolve_bench(project, bench, bench_path)` -> `None` -> emit a notice + `core.inspect(refresh="auto", offer_choice=False)` for the cache side effect -> re-resolve -> `DEFAULT_BENCH_PATH` + a `bench.default_used` warning; a `select_bench` from a multi-bench project surfaces as a `NEEDS_CHOICE` the frontend resolves and re-invokes, exactly as `backup`/`open` do.

### 8. Zero new primitives, with two reported items

| Need | Existing primitive |
| --- | --- |
| frappe container | `core/docker.py:get_frappe_container` |
| run-state / race backstop | `resolvers.resolve_container_state` (both `offer_choice` modes) |
| bench resolve + default | `resolvers.resolve_bench` + `DEFAULT_BENCH_PATH` |
| the no-cache populate | `core.inspect(refresh="auto", offer_choice=False)` (the `open_plan` fallback) |
| the destructive exec + secrets | buffered `exec_run(..., environment=)` (the `backup` precedent) |
| the streamed copies | `container.put_archive` / `get_archive` (docker-py, used directly) |
| migrate + restart | `core.start(restart=True)` (the `_start_project` -> `core.start` precedent) |
| site/path validation | `resolvers.validate_site_name` / `validate_bench_path` / `require_bench_dir` / `require_site_dir` |
| the events | the `OnEvent` idiom (`core/update.py`, `core/inspect.py`) |
| the envelope + choices | `Result`/`Choice`; `select_backup` and `confirm_restore` are new tokens in the open `Choice.kind` set |

Two items REPORTED per the standing rule rather than absorbed:

1. **restore keeps its own default-site resolver.** `resolvers.resolve_default_site` reads only the cache and RAISES `NOT_FOUND`; restore's `_resolve_default_site` adds a LIVE `sites/currentsite.txt` read (via `bench_sites.read_current_site`) as a final fallback, because the destructive path must resolve the default against a cold/stale cache (the captain's `ners` bench had no `default_site` key). Three resolutions, one right: reuse the shared resolver and DROP the live read (a behavior change in the quiet, more-likely-to-error direction on the most destructive path); widen the shared resolver and ADD the live read to `backup`/`unlock` (the bending the standard exists to catch); or keep restore's own helper private to `core/restore.py` (changes nothing). The third. The Decision-8-of-batch-3 trap, applied again.
2. **`--receive` has no `--ticket` flag.** The sendme ticket is always a `questionary.text` prompt with no flag equivalent, so `--receive` cannot run fully non-interactively today - a pre-existing gap in the captain both-modes standard. This migration PRESERVES it byte-exactly (the ticket prompt stays frontend); adding `--ticket` is a behavior addition (a new flag, new non-TTY refusal) deferred to its own change. Reported so it is a known follow-up, not a silently-shipped regression.

### 9. No `axi restore` verb this batch, deferred as the captain's decision, absence asserted

The position, from the evidence both ways:

- **For a verb**: agents already drive `cwcli restore` non-interactively through the human CLI (the selectors + `--yes` + credential flags exist for exactly that); the plan/apply shape serializes cleanly and makes an `axi restore` thin; `restore_plan` is a safe read-only preview an agent could always call.
- **Against shipping it HERE**: `bench restore --force` DROPS AND RECREATES a live site's database - the single most destructive operation cwcli performs, strictly more destructive than `bench uninstall-app` (whose `axi` verb the captain LOCKED as not-built on its own evidence) and in the class of `axi init` (deferred as the captain's own product decision). Whether an agent may destroy a user's site data is a product decision the captain owns on its own evidence, not a rider on a refactor PR. It also forces agent-surface secret UX (`--mariadb-root-password` on the agent's argv) that deserves its own design.

So: no verb, a test asserts the `axi` registry has no `restore`, and the assertion's docstring records this as DEFERRED (the two-call core shape makes it thin whenever decided) rather than a structural refusal (`axi open`'s case). The `apps install`/`uninstall` and `init` precedents make the deferral legible and un-forgettable.

## Boundary discipline

`core/restore.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py`'s AST ban covers it automatically.
It never prints, prompts, exits, or hands over; all narration rides typed events; no live Docker object crosses either return boundary (`RestorePlan` and `RestoreReport` are plain strings/bools/lists; `asdict` yields plain data, asserted by test; the plan carries container-path strings, never a container handle).
No secret value appears in any event, warning, DTO field, or the command-echo trace (pinned by test, the `test_restore_safety.py::TestReceivePasswordNotOnArgv` precedent re-pointed at the core).
`commands/restore.py` no longer imports `core.inspect` for the fallback directly (the core owns it), keeps the sendme subprocess + prompts, and renders the typed results.

## Risks / Trade-offs

- **The interactive-menu re-invoke re-scans** -> sub-second, human-path only; the selector paths resolve in one `restore_plan` call. Pinned by characterization tests before and after.
- **The migrate-failure exit code moves from a `bool` return to `report.migrate_ok`** -> same exit 1, expressed on the returned fact (the `update` precedent).
- **The sendme subprocess stays frontend** -> disclosed non-consumption (the `logs` `-it` precedent), the interactive Ctrl-C-wait/clipboard/ticket-prompt half; the container copies and the destructive act are fully migrated.
- **The two restore suites are Typer-command-bound** (`restore_receive_mode`/`restore` called directly) -> characterization-first (batch 4's discipline): command-level tests pinning the confirms, selectors, secret-in-`environment=`, origin mismatch, streamed copies, and both-modes refusals are committed green against unmigrated HEAD, then helper-bound tests move with their subjects and every test changed BY DESIGN is named in tasks.
- **A destructive-delete E2E is required** -> the captain standard: one throwaway `cwe2e-` instance, real init -> seed data -> restore, both modes, against the worktree's editable install, memory-serialized against the sibling `rm` crew.

## Migration Plan

`tasks.md` order: characterization pinned green first, then `core/restore.py` under `tests/test_core_restore.py`, then `commands/restore.py` reseated and the two suites re-pointed, then the no-`axi restore` assertion and dead-code deletion, then the full-lifecycle E2E in both modes on one memory-serialized throwaway instance, then docs, ledger, and skills.

## Open Questions

None blocking. Two recorded:

1. **`axi restore`** is deferred as the captain's own product decision (Decision 9); the plan/apply shape makes it thin if approved.
2. **A `--ticket` flag for non-interactive `--receive`** (Decision 8, item 2) closes the one both-modes gap restore has; it is a behavior addition deferred to its own change.
