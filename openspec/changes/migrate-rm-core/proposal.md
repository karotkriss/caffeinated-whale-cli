## Why

`rm` is batch 12 of the logic-core rework and the most safety-critical command in the codebase: its backup gate fails closed and a regression there silently destroys data.
It is one of two commands still off the core (`restore` is the other, migrated by a sibling crew), and its 1628 lines weld the destructive decisions to their `rich` rendering.
Three costs of its un-migrated state, all measured at current HEAD:

**1. The whole data-destruction decision tree lives in `commands/`, unreachable by any non-CLI frontend.**
`_remove_project` (`rm.py:686-1053`) owns the fail-closed backup gate (C1), the container-removal loop, the named-volume + project-dir deletion (issue #19), the per-bench backup fan-out, the path-traversal name guard (H5), the honest-exit-code accounting (M11), and the cache-clear-on-clean-removal rule - every one of them interleaved with `console.print`/`stderr_console.print` and driven by a `rich` `status` spinner handle.
A GUI or `axi`-shaped consumer cannot reach any of it, and the invariants that keep the databases safe are re-derivable only by reading print statements.

**2. The verified copy-out backup is rm's own logic, distinct from `core.backup`, and stranded in the frontend.**
`_backup_sites` + `_stream_container_file` + `_ChunkStreamReader` (`rm.py:152-352`) do something `core.backup` does NOT: they run `bench backup --with-files`, then STREAM each artifact out of the container and VERIFY the database dump landed non-empty on the host archive, scoped to this run's timestamp token - because the volume the backup lives in is about to be deleted.
`core.backup` leaves its dump inside the container; rm's backup is a copy-out-and-verify gate.
That gate is the load-bearing half of C1 and it has no home on the core.

**3. Every warning, notice, and spinner label is a bare print, so the decisions cannot be tested apart from their rendering.**
The backup fan-out, the archive step, the volume/dir removal, and the refusal paths all emit `rich` markup inline; the tri-state outcome (found / orphan / partial-failure / clean) is a plain dict the frontend inspects.
The typed-envelope architecture carries exactly that outcome as data and exactly that narration as events.

## What Changes

- **`core/rm.py`, ONE plain function** - `core.remove(project_name, *, remove_volumes=True, no_backup=False, on_event=None) -> Result[RemovalOutcome]` - the migrated `_remove_project` and its data-destruction helpers (`_backup_sites`, `_archive_project_config`, `_archive_project_directory`, `_delete_project_directory`, `_remove_named_volumes`, `_stream_container_file`, `_ChunkStreamReader`, `_bench_archive_slug`).
  `RemovalOutcome` carries the tri-state result (`found`, `orphan`, `containers_removed`, `volumes_removed`, `dir_removed`, `backup_ok`, `failures`) as plain data; progress and warnings ride the optional typed-event `on_event` callback (the `core.update`/`core.init` shape), never a print.
- **plan/apply is DECLINED here, settled on rm's real behavior** (design Decision 1) - see design; the destructive-preview two-call shape the batch-3 note deferred to `restore`/`rm` is NOT what rm needs.
  rm's confirmation is a FRONTEND concern that fires BEFORE any core call, on generic warning text plus a per-project run-state disclosure list; it never computes a manifest of the volumes/sites/dirs it will delete, and manufacturing one would ADD a preview capability rm has never had - a behavior change wearing a migration's clothes (the `run_plan` "resolve exactly what it resolves today" and `config` "migrate byte-identical" disciplines).
  `_remove_project` is already the `core.backup`/`core.update` single-function shape; `core.update`'s locked reasoning ("a plain function has no laziness to work around, so the forcing function is absent; a plan phase would be structure without a job") applies verbatim.
- **The fail-closed backup gate is preserved byte-exactly**: the EARLY abort (`backup_failed = remove_volumes and not no_backup and not backup_ok`) evaluated BEFORE any container is removed, the per-bench `all(...)` aggregation, the copy-out verification scoped to this run's timestamp token, the DB-dump-present-and-non-empty check, the not-running fail-closed backstop (`backup_ok = False` on the `--volumes`-without-`--no-backup` path), and the orphan refusal on that path.
  `--no-backup` stays the escape hatch; `--no-volumes` stays outside the gate.
- **The stopped-project start -> back up -> delete flow stays FRONTEND orchestration, unchanged** (`_transient_start_for_backup`, `_wait_for_db_ready`, `_project_run_state`, `_frappe_container_running`, `_stop_after_transient_start` stay in `commands/rm.py`): it reuses `commands/start.py:_check_port_conflicts`, which PROMPTS (the spinner-over-questionary deadlock), so it must run outside the removal spinner and cannot enter the core.
  `_stop_after_transient_start` keeps calling the already-migrated `core.stop`; the transient start is the frontend's job by the `cwcli-lifecycle` skill's own design ("the transient start lives in the CLI, not `_remove_project`").
- **The H5 path-traversal guards become public core functions** (`is_valid_project_name`, `is_safe_project_dir`), so the CLI keeps its up-front pre-filter loop byte-for-byte (validate names, reject with today's message, exit 1 if any rejected) and `core.remove` re-checks its own param, raising `CwcliError(USAGE)` (a core function must not trust its caller's discipline).
- **`commands/rm.py` thins to a renderer**: the trailing-flag recovery, piped input, up-front name pre-filter, the recache-skip-for-stopped, the confirm + stopped-project disclosure, the transient-start orchestration, choice-free per-project loop, and the aggregate exit-code decision (`outcome.failures`, the `core.update` precedent - NOT `result.status`).
- **There is deliberately NO `axi rm` verb, and the absence is asserted** (design Decision 5): whether an agent may DELETE an instance's data (named volumes, the whole bench) is a product decision the captain owns on its own evidence - the `axi apps install`/`uninstall` and `axi init` locked precedent.
  A test pins the registry absence so "deferred" can never read as "forgotten".

## Impact

- **New:** `src/caffeinated_whale_cli/core/rm.py`, `tests/test_core_rm.py`, the no-`axi rm` assertion, `tests/test_rm_characterization.py` (committed green against unmigrated HEAD first, batch 4's discipline).
- **Changed:** `commands/rm.py` (renderer), the four existing rm suites (`test_rm_safety.py`, `test_rm_truth.py`, `test_rm_stopped.py`, `test_rm_stopped_backup.py` - the destructive-helper tests re-point with their subject to the core; the transient-start/orchestration tests stay pointed at the frontend; the `_remove_project`-rejects-invalid-name test changes BY DESIGN to expect the `USAGE` raise), CLAUDE.md ledger, `cwcli-lifecycle` (`references/rm.md`) + `cwcli-core-axi` skills, `docs/technical/README.md` core module list, `tests/README.md` coverage map.
- **Unchanged:** every user-visible behavior (what deletion removes, the C1/H5/M11 gates, the multi-bench per-bench backup, the stopped-project transient-start flow, the abort-keeps-all-data-and-returns-to-stopped path, orphan refusal, `--no-backup`, `--no-volumes`, exit codes, the confirm text + disclosure, the streamed multi-GB-safe copy), `core.stop`, `core.inspect.discover_benches`, `cache.recache_project`, `commands/start.py:_check_port_conflicts`, the cache schema, the `axi` surface (no new verb), `skills/cwcli/SKILL.md` (no verb change, no regeneration).
- **Zero new primitives, as a falsifiable claim**: the envelope, `CwcliError`, the `OnEvent` idiom (`core.update`/`core.init`), `core.inspect.discover_benches`, `core.stop`, and the `utils/` building blocks (`get_project_containers`/`get_project_volumes`/`bench_sites.list_sites`/`db_utils`) cover every need.
  `core.remove` deliberately does NOT route through `resolvers.resolve_container_state` or `get_frappe_container` (the Decision-8 trap): rm is project-wide, must handle an orphan with no frappe service (the `core.stop` precedent), and a stopped project is NORMAL for rm - reaching for `resolve_container_state` would ADD a `confirm_start` fork rm does not have, since rm's stopped-project handling is the frontend transient-start plus the fail-closed backstop, not a prompt.
  The rm-specific backup/archive helpers are core-PRIVATE to `core/rm.py`, not new primitives.
  If implementation finds a genuine bend, it is reported per the standing rule.
