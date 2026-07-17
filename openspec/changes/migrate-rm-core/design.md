## Context

Batch 12 of the logic-core rework. `rm` is the delete-with-backup-gate command and the most safety-critical code in the repo: `_remove_project`'s backup gate fails closed and a regression there silently destroys databases.
This design owns both the audit and the proposal; every line reference below was read at current HEAD (`c4966d2`), not inherited from a prior proposal's prose (batch 1's Non-Goals error is the named anti-pattern).
`rm` already leans on two migrated cores - `core.stop` (`rm.py:1222`, the return-to-stopped path) and `core.inspect.discover_benches` (`rm.py:804`, the cache-fail live discovery) - and on `cache.recache_project` (which since batch 7 calls `core.inspect`), so the migration builds on those rather than re-doing them.
The brief's framing that "rm already calls `core.backup`" is inaccurate and worth stating precisely: rm has its OWN backup (`_backup_sites`), which is a copy-out-and-verify gate, not `core.backup`'s in-container dump (Decision 2).

### Audit: what `rm` actually is at HEAD (1628 lines)

| Lines | What | Migrates? |
| --- | --- | --- |
| `46-85` | `_is_safe_project_dir` / `_is_valid_project_name` (H5 path-traversal guards) | **Yes**, as PUBLIC core functions (`is_safe_project_dir`/`is_valid_project_name`); the frontend keeps its up-front pre-filter loop calling them (Decision 4) |
| `88-102` | `_list_sites` (thin wrapper over `bench_sites.list_sites`) | **Yes.** Core-private wrapper, same fail-safe site detection |
| `105-119` | `_bench_archive_slug` (per-bench archive namespacing) | **Yes.** Core-private, moves verbatim |
| `122-196` | `_ChunkStreamReader` + `_stream_container_file` (streamed multi-GB-safe copy-out) | **Yes.** Core-private; the load-bearing copy primitive (Decision 3) |
| `199-352` | `_backup_sites` (the verified copy-out backup gate, per-run timestamp scoping, DB-dump-non-empty check) | **Yes.** Core-private; the C1 gate's data half (Decision 3) |
| `354-468` | `_archive_project_config` (docker-compose.yml + site_config.json snapshot) | **Yes.** Core-private; warning-not-gate on failure |
| `471-533` | `_remove_named_volumes` (named compose volumes, the issue #19 fix) | **Yes.** Core-private; `failures` collector |
| `536-621` | `_archive_project_directory` (conf/-only archive, symlinks-safe copytree) | **Yes.** Core-private; the LATE gate's archive half |
| `624-683` | `_delete_project_directory` (rmtree the local instance dir) | **Yes.** Core-private; H5-guarded before rmtree |
| `686-1053` | `_remove_project`: the whole destructive state machine (name guard, containers, archive dir, per-bench backup fan-out, EARLY backup gate, container-removal loop, LATE gate, volume/dir removal, cache clear) | **Yes.** Becomes `core.remove` (Decisions 1, 3, 6, 7) |
| `1056-1075` | `_frappe_container_running` (recache-skip read) | **No.** Frontend orchestration read (Decision 6) |
| `1078-1100` | `_project_run_state` (running/stopped/orphan/error classification) | **No.** Frontend orchestration read (Decision 6) |
| `1103-1140` | `_wait_for_db_ready` (credential-free TCP probe after a cold start) | **No.** Frontend, part of the transient start (Decision 6) |
| `1143-1211` | `_transient_start_for_backup` (start -> back up -> delete, reuses `_check_port_conflicts`) | **No.** Frontend orchestration; prompts, so it stays outside the spinner (Decision 6) |
| `1214-1234` | `_stop_after_transient_start` (return-to-stopped via `core.stop`) | **No.** Frontend; already on `core.stop` |
| `1237-1269` | `_recover_trailing_flags` (variadic-argument flag recovery) | **No.** Frontend argv parsing |
| `1272-1400` | Typer signature, piped input, up-front name pre-filter | **No.** Frontend |
| `1401-1429` | Recache-skip-for-stopped (spinner-deadlock guard) | **No.** Frontend |
| `1430-1492` | Confirm prompt + stopped-project disclosure | **No.** Frontend UX |
| `1494-1617` | Per-project loop (transient start, `_remove_project`, return-to-stopped, aggregate exit) | **No.** Frontend renderer + orchestration; reads `outcome.failures` for the exit code (Decision 7) |

The split is clean: the destructive DECISIONS and their I/O move to the core; the frontend keeps argv parsing, the confirm UX, the transient-start orchestration (which prompts), and the run-state reads that drive it.

## Goals / Non-Goals

**Goals.**
Move rm's data-destruction decision tree onto the core behind a typed envelope; preserve the C1 fail-closed backup gate, the H5 path guards, the M11 honest-exit accounting, the multi-bench per-bench backup, the stopped-project transient-start flow, orphan refusal, and the streamed multi-GB-safe copy-out byte-for-byte; settle the plan/apply question on rm's real behavior and disclose it.

**Non-Goals.**
Any behavior change to what deletion removes, the gates, the confirm text + disclosure, exit codes, or the transient-start flow.
Building a destructive-preview `plan` phase rm does not have today (Decision 1).
Migrating `_check_port_conflicts` (start's un-migrated concern; rm's transient start reuses it as today).
An `axi rm` verb (Decision 5: deferred as the captain's own product decision, absence asserted).
The `restore` migration (the sibling crew's) and `self_update`'s mutating half (the un-migrated list, restated from the code).
Fixing `db_utils.cache_project_data`'s non-transactionality (the standing hazards-board item; rm only calls `clear_cache_for_project`).

## Decisions

### 1. plan/apply is DECLINED, settled on rm's real behavior

The batch-3 note reserved the plan/apply two-call shape ("previews a DESTRUCTIVE action for confirmation") for `restore`/`rm`, and the brief names rm "the natural home." Read against rm's actual behavior at HEAD, it is not.

- **rm's confirmation is a FRONTEND concern that fires BEFORE any per-project work**, on generic warning text (`rm.py:1433-1477`) plus a per-project run-state disclosure list (`rm.py:1452-1468`, "these stopped projects will be started to back up first"). It does NOT enumerate the volumes, sites, or directories it will delete. A `plan` phase computing that manifest would ADD a preview capability rm has never had - a behavior change wearing a migration's clothes, exactly the trap `run_plan` (resolve exactly what it resolves today) and `config` (migrate byte-identical, rework separately) were disciplined against.
- **The three settled two-call forcing functions do not apply.** No generator laziness (nothing returns an iterator; progress rides `on_event`, the `core.update` precedent). No mid-flow decision needing a prior stage's product (init's motivation): rm's one prompting step is the transient start, which is FRONTEND orchestration around `_check_port_conflicts`, happens BEFORE the core call, and is not a `NEEDS_CHOICE` the core returns. No destructive preview the core must compute for the confirm - the confirm is already rendered from data the frontend holds.
- **`_remove_project` is already the `core.backup`/`core.update` single-function shape** - a multi-step operation returning a report. `core.update`'s locked reasoning transfers verbatim: "a plain function has no laziness to work around, so the forcing function is absent," and "the tell that a plan phase would have been structure without a job."

So: `core.remove(...) -> Result[RemovalOutcome]`, one call, the report returned rather than printed.
This does NOT settle plan/apply for `restore` - a site-clobbering restore genuinely previews (delete-then-recreate), and the sibling crew settles it on `restore`'s own evidence.
rm's honesty comes from its gate being FAIL-CLOSED and evaluated at apply time against live state, not from a preview.

### 2. rm's backup is a copy-out-and-verify gate, distinct from `core.backup`

`core.backup` runs `bench --site <site> backup` and locates the dump INSIDE the container.
rm's `_backup_sites` runs `bench backup --with-files` and then STREAMS each artifact OUT of the container, verifying the database dump landed non-empty on the host archive (`rm.py:265-336`) - because the volume the in-container dump lives in is about to be deleted.
They answer different questions ("is a dump on disk in the container?" vs "is a verified dump safely on the host before I destroy the volume?") with opposite trust models, so `core.remove` keeps rm's own `_backup_sites`/`_stream_container_file` as core-private helpers rather than calling `core.backup`.
This is the `update`/`apps` "inverse questions, do not unify" judgment applied to backup: reusing `core.backup` would DROP the copy-out verification that IS the C1 gate.
The streamed copy (`_ChunkStreamReader` feeding `tarfile` in `r|` mode, fixed-size writes, truncated-stream fail-closed) moves verbatim - it exists so a multi-GB `--with-files` artifact is never buffered whole in host RAM, and that property is pinned by `TestStreamedCopy`.

### 3. The C1 fail-closed backup gate is preserved byte-exactly, in the core

`core.remove` reproduces `_remove_project`'s two gates exactly where they sit today:

- **The EARLY backup gate**: `backup_failed = remove_volumes and not no_backup and not backup_ok`, evaluated BEFORE the container-removal loop. When true, `core.remove` records the refusal in `RemovalOutcome.failures` and RETURNS immediately - no container stopped or removed, no volume/dir touched, the cache preserved (`failures` non-empty). This closes the retry-orphan data-loss path (a naive retry on a torn-down project finds no live DB to back up).
- **The not-running fail-closed backstop**: when no frappe container is running and `remove_volumes and not no_backup`, `backup_ok` is set False so the EARLY gate aborts. The frontend transiently starts a stopped project BEFORE calling `core.remove`, so reaching this branch on the backup path means the start did not take or it is an orphan - either way, refuse. `--no-backup` and `--no-volumes` proceed unchanged.
- **The per-bench fan-out**: `backup_ok = all(...)` across every cached bench (falling back to live `discover_benches`, then to the single default `/workspace/frappe-bench`), so one bench's failed backup blocks volume deletion for the whole instance. Per-bench archive namespacing (`_bench_archive_slug`) applies only when `len(bench_paths) > 1`.
- **The LATE gate**: `container_removal_failed or archive_failed`, evaluated AFTER container removal (containers are recreatable; the named volumes hold the data), protects the volume + dir deletion.
- **Cache-clear keyed on `not failures`**: a half-removed or gate-blocked instance keeps its cache entry so it stays visible in `ls`/`inspect` and retryable.

The `status.update(...)` spinner-label calls become `RmStep` events; the `console.print`/`stderr_console.print` calls become `RmNotice`/`RmWarning`/`RmError`/`RmTrace` events (Decision 8). No decision changes.

### 4. The H5 path guards become public core functions; the frontend keeps its pre-filter

`is_safe_project_dir(project_dir)` and `is_valid_project_name(name)` move to the core public surface (they are the guard against `cwcli rm ..` resolving `PROJECTS_DIR/..` to the cwcli-state root and rmtree'ing every project + cache + config).
The frontend keeps its up-front pre-filter loop (`rm.py:1380-1399`) byte-for-byte: validate every name, print today's per-name rejection message, and exit 1 if any was rejected even when valid names remain.
`core.remove` re-checks its own `project_name` at entry and raises `CwcliError(USAGE, "project.invalid_name")` - a core function must not trust its caller's discipline, and the raise is pure defense-in-depth (the frontend never passes an invalid name).
The `_delete_project_directory`/`_archive_project_directory` last-line `is_safe_project_dir` hard-guards move with those helpers.
**Changed by design**: `test_rm_safety.py::TestProjectNameValidation::test_remove_project_rejects_invalid_name` calls the destructive core directly and today asserts a `found=False` + `failures` dict; re-pointed at `core.remove` it asserts the `CwcliError(USAGE)` raise. Named here per the discipline.

### 5. No `axi rm` verb this batch, deferred as the captain's decision, absence asserted

An agent that runs `rm --volumes` destroys an instance's databases, sites, and files.
Whether an agent may do that is a resource-and-product decision of the `axi apps install`/`uninstall` and `axi init` class - the captain owns those on their own evidence, not as a rider on a refactor.
The fail-closed backup gate does not change the calculus: it protects against ACCIDENT, not against an agent that deliberately means to delete.
So: no verb, and `tests/test_core_rm.py::TestNoAxiRmVerb` asserts the `axi` Typer registry has no `rm` command, its docstring recording DEFERRED (the captain's follow-up decision) rather than a structural refusal - the single-function core shape makes the verb thin whenever it is decided (it would carry the destructive consent separately, the `apps uninstall` precedent).

### 6. The stopped-project transient-start flow stays FRONTEND, unchanged

`_transient_start_for_backup`, `_wait_for_db_ready`, `_project_run_state`, `_frappe_container_running`, and `_stop_after_transient_start` stay in `commands/rm.py`:

- The transient start reuses `commands/start.py:_check_port_conflicts`, which PROMPTS at a TTY (the spinner-over-questionary deadlock), so it must run OUTSIDE the removal spinner and cannot enter a core that must never prompt. The `cwcli-lifecycle` skill fixes this as the design ("the transient start lives in the CLI, not `_remove_project`").
- `_project_run_state` / `_frappe_container_running` are pure reads, but their ONLY consumers are frontend orchestration (the disclosure list, the transient-start decision, the recache skip). Moving them to the core while their sole callers stay frontend is the "one caller is not a shared concern" anti-pattern (the `resolvers` extract-on-second-caller rule); `core.remove` derives its own `frappe_running` internally and does not need them.
- `_stop_after_transient_start` already calls the migrated `core.stop`; it is best-effort by design (runs only after an abort that already kept every byte) and must never raise - unchanged.

So `core.remove` is always called against an already-running project (the frontend started a stopped one first), an orphan, or a `--no-backup`/`--no-volumes` run - and its not-running branch (Decision 3) is the fail-closed backstop for the case the frontend's start silently did not take.

### 7. Exit codes read `outcome.failures`, never `result.status` - the update precedent

`core.remove` returns `Result[RemovalOutcome]`.
Status maps: a clean full/orphan/`--no-volumes` removal is `OK`; a partial failure, a gate-blocked removal, or a genuinely-not-found project is `WARNING` (a completed operation with a note - the closed `Status` set has no `ERROR` by design).
The frontend decides the exit code from `outcome.failures` being non-empty, NOT from `result.status` - copying the `raise typer.Exit(0 if status in (OK, WARNING) else 1)` pattern here would map a partial failure's `WARNING` envelope to exit 0 and report success for a removal that half-failed (batch 4's trap, live here for the same reason).
The genuinely-not-found project is `found=False` with empty `failures`: the frontend renders today's "Project not found" error and preserves the exit-0 no-op aggregate (rm treats an already-absent project as a no-op, exit 0 - pinned).
A Docker connection error (`get_project_containers` returns None) raises `CwcliError(DOCKER)` (the `core.stop` precedent); the frontend catches it per-project, records a failure, and exits 1 - same observable outcome as today's append-to-failures.

### 8. The typed-event family carries every narration line

`core.remove` emits, via `on_event`:

- `RmStep(label)` - a spinner-label update (was `status.update(...)`).
- `RmNotice(text)` - a dim stdout progress line (was `console.print("  [dim]...[/dim]")`): "Backed up N site(s)", "Configuration archived to X", "Removed N named volume(s)", "Removed project directory X".
- `RmWarning(text, hint=None)` - a yellow stderr warning (was `stderr_console.print("[yellow]Warning:[/yellow] ...")`), with the optional dim follow-up line the backup-refusal carries.
- `RmError(text)` - a red stderr error (was `stderr_console.print("[bold red]Error:[/bold red] ...")`), for the per-container removal failure.
- `RmTrace(text)` - a verbose-only dim stderr diagnostic (was `stderr_console.print("[dim]VERBOSE: ...[/dim]")`), rendered only under `-v`.

The frontend renderer wraps the live `status` handle and reproduces each line's exact styling and stream; verbose-gating stays a frontend fact (which lines were `if verbose:` today).
The events carry PLAIN text only (no rich markup, no secret - rm handles no secrets); `RemovalOutcome` carries no live Docker object and `asdict` yields plain data, asserted by test.

## Boundary discipline

`core/rm.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py`'s AST ban covers it automatically.
It never prints, prompts, exits, or starts a container; all narration rides typed events, and no live Docker object crosses the `RemovalOutcome` boundary.
`core/rm.py` imports `core.inspect` (`discover_benches`) and `core.stop` is untouched (the frontend keeps calling it); no module under `core/` imports `commands/` at runtime (grep-checked).

## Risks / Trade-offs

- **The most safety-critical gate in the codebase moves layers** -> characterization-first (batch 4's discipline): command-level tests pinning what-deletes-what, the C1 abort-before-removal, the H5 refusals, the M11 exit codes, the multi-bench fan-out, and the orphan/stopped paths are written through migration-surviving seams (`get_project_containers`/`get_project_volumes` patches, fake containers, tmp filesystem) and committed green against unmigrated HEAD; the destructive-helper tests then move with their subjects, and every test changed BY DESIGN is named in tasks.
- **`_remove_project`'s invalid-name path changes from a dict to a raise** -> disclosed in Decision 4; observationally identical from the CLI (the frontend pre-filters), pinned before and after.
- **A real full-lifecycle E2E is mandatory** (the captain's dangerous-delete standard): real init -> seed data -> `rm` with backup on a throwaway `cwe2e-` instance, verifying the host backup is genuinely restorable AND the deletion is honest (volumes + dir gone), in BOTH modes, against the worktree's own editable install - never mocks, never PyPI, never the captain's instances, memory-serialized against the sibling `restore` crew.

## Migration Plan

`tasks.md` order: characterization pinned green first, then `core/rm.py` under `tests/test_core_rm.py`, then the frontend reseated and the four suites re-pointed, then the no-`axi rm` assertion and dead-code deletion, then E2E in both modes on one throwaway instance, then docs, ledger, and skills.

## Open Questions

None blocking. One recorded: **`axi rm`** is deferred as the captain's own product decision (Decision 5); the single-function core shape makes it thin if approved, and it would wire the destructive consent separately (the `apps uninstall` precedent).
