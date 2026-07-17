## 1. Characterization first: rm's behavior pinned green BEFORE anything moves

- [x] 1.1 Run the four existing rm suites at the batch's base commit and record the baseline: `tests/test_rm_safety.py` (C1/H5/M11 backup gate, multi-bench, streamed copy), `test_rm_truth.py` (volume/dir removal, end-to-end, arg-order), `test_rm_stopped.py` (recache-skip, fail-closed backstop), `test_rm_stopped_backup.py` (transient-start orchestration).
- [x] 1.2 Add `tests/test_rm_characterization.py` for the command-level behaviors, green against UNMIGRATED code, committed separately (batch 4's discipline), through migration-surviving seams (`get_project_containers`/`get_project_volumes` patches, fake containers, tmp filesystem, `sys.stdin.isatty`): the confirm text + stopped-project disclosure, the EARLY C1 abort-before-any-container-removal (exit 1, nothing deleted, cache preserved), the H5 `rm ..` refusal (exit 1, sentinel survives), the M11 partial-failure exit 1 (no green line), the genuinely-not-found exit-0 no-op, the multi-bench per-bench backup fan-out, `--no-volumes` proceeding without a backup, and `--no-backup` deleting an orphan.

## 2. `core/rm.py`: the single-call slice under its own tests

- [x] 2.1 `RemovalOutcome` frozen/slots/kw_only DTO per design (`found`, `orphan`, `containers_removed`, `volumes_removed`, `dir_removed`, `backup_ok`, `failures`; serializable via `asdict`, no live object).
- [x] 2.2 The typed event family (`RmStep`/`RmNotice`/`RmWarning`/`RmError`/`RmTrace` + `OnEvent`) and a `_noop` drain, with the module docstring recording the single-call plan/apply-declined rationale and the no-`axi rm` deferral.
- [x] 2.3 The public H5 guards `is_valid_project_name`/`is_safe_project_dir`; `core.remove` re-checks its param and raises `CwcliError(USAGE)` on an invalid name.
- [x] 2.4 The core-private data-destruction helpers moved verbatim: `_backup_sites`, `_stream_container_file` + `_ChunkStreamReader`, `_bench_archive_slug`, `_archive_project_config`, `_archive_project_directory`, `_delete_project_directory`, `_remove_named_volumes`, `_list_sites` - `console`/`stderr_console` prints become `on_event` emissions, the streamed multi-GB-safe copy preserved.
- [x] 2.5 `remove(project_name, *, remove_volumes=True, no_backup=False, on_event=None) -> Result[RemovalOutcome]`: the whole destructive state machine - name guard, containers via `get_project_containers` (`DOCKER` raise on None), orphan detection, archive dir, per-bench backup fan-out (cache -> `discover_benches` -> default, `all(...)` aggregation), the EARLY backup gate abort, the container-removal loop, the LATE gate, volume/dir removal, cache-clear on `not failures`; `status.update` -> `RmStep`, all prints -> events; NOT routed through `resolve_container_state`/`get_frappe_container` (Decision 6, the Decision-8 trap).
- [x] 2.6 `tests/test_core_rm.py`: every branch above on fake containers/tmp filesystem; the C1 EARLY abort (nothing removed, cache preserved); the not-running backstop; the multi-bench fan-out (both-ok, one-fails-blocks-all, single-bench flat layout, cache-fail live discovery, cache-fail default fallback); the config-archive-warning-not-gate; the streamed copy (chunked, exact reassembly, truncated fail-closed); the invalid-name `USAGE` raise; the Docker-error `DOCKER` raise; `asdict`-is-plain-data; "the core prints nothing at all".

## 3. Reseat `commands/rm.py` as a renderer

- [x] 3.1 Frontend keeps: the Typer signature, `_recover_trailing_flags`, piped input, the up-front name pre-filter loop (exit 1 if any rejected), the recache-skip-for-stopped, the confirm + stopped-project disclosure, `_project_run_state`/`_frappe_container_running`/`_wait_for_db_ready`/`_transient_start_for_backup`/`_stop_after_transient_start`, the aggregate reporting. (`handle_docker_errors` was scoped to the moved `_remove_project`; the daemon-down case now surfaces as `core.remove` raising `CwcliError(DOCKER)`, caught per-project - the common daemon-down message "Could not connect to Docker to inspect '{name}'." is preserved, since it always came from `get_project_containers` returning None, not the decorator.)
- [x] 3.2 An `_RmRenderer` wrapping the live `status` handle renders the typed events in today's exact styling/streams (`RmStep` -> `status.update`; `RmNotice` -> dim stdout; `RmWarning` -> yellow stderr + optional dim hint; `RmError` -> red stderr; `RmTrace` -> dim stderr under `-v`), and a `_render_error` mapping `CwcliError(DOCKER)`/`USAGE` to today's per-project lines.
- [x] 3.3 The per-project loop: transient-start a stopped project outside the spinner (unchanged), call `core.remove` under the removal spinner, catch `CwcliError` per-project (record failure, return-to-stopped if started), read `outcome.failures` for the exit code (Decision 7, NOT `result.status`), render green/orphan/not-found/failure exactly as today.
- [x] 3.4 Re-point the four suites: destructive-helper tests (`_remove_project`, `_backup_sites`, `_stream_container_file`, `_remove_named_volumes`, `_delete_project_directory`, `_archive_project_directory`, `_is_valid_project_name`) move with their subjects to `core/rm.py`, assertions untouched except the ONE named BY DESIGN (`TestProjectNameValidation::test_remove_project_rejects_invalid_name` -> expects the `USAGE` raise); the transient-start/orchestration tests (`_project_run_state`, `_wait_for_db_ready`, `_transient_start_for_backup`, `_stop_after_transient_start`, `_recover_trailing_flags`, the `rm()`-level orchestration) stay pointed at the frontend.
- [x] 3.5 Grep proves `commands/rm.py` no longer defines the destructive helpers and no module under `core/` imports `commands/` at runtime.

## 4. Asserted absences and dead code

- [x] 4.1 `tests/test_core_rm.py::TestNoAxiRmVerb` asserts the `axi` Typer registry has no `rm` command, its docstring recording DEFERRED (the captain's follow-up decision) per Decision 5.
- [x] 4.2 Delete the moved helpers from `commands/rm.py`; grep proves no caller remains for each and the frontend imports the core's public guards where it pre-filters.

## 5. Suite health

- [x] 5.1 The §1 characterization tests green against the MIGRATED code unchanged.
- [x] 5.2 Full fast tier green (`uv run pytest`); black, ruff, mypy at zero; `tests/README.md` coverage map updated.

## 6. E2E on a real instance, BOTH modes (captain dangerous-delete standard)

- [ ] 6.1 MEMORY-SERIALIZE first: `docker ps --format '{{.Names}}' | grep cwe2e` - if any non-mine `cwe2e-` container exists, WAIT (paused) until it clears. One throwaway isolated instance (`cwe2e-` prefix, `CWCLI_HOME` isolation per skill `cwcli-e2e-testing`), against the worktree's own editable install, torn down immediately after; no broad Docker cleanups; disk pressure means stop and report blocked.
- [ ] 6.2 Real full lifecycle: real `cwcli init` -> seed real data -> `cwcli rm` with backup, verifying the host backup is genuinely present/restorable AND the deletion is honest (named volumes + project dir actually gone); the stopped-project start -> back up -> delete leg; a forced-failure abort keeps all data and returns to stopped; `--no-backup` deletes honestly. BOTH modes (interactive confirm via pty awaiting `ESC[?2004h`; non-interactive `--yes`).
- [ ] 6.3 Re-run AFTER the no-mistakes pipeline and after any review fixes (the standard: review fixes are a trigger, not an exemption).

## 7. Docs and memory

- [x] 7.1 CLAUDE.md ledger: `rm` moves to the migrated list with the single-call plan/apply-declined rationale, the preserved C1/H5/M11 gates, and the deferred-`axi rm` assertion; the un-migrated list restated from the code (`restore`, `self_update`'s mutating half).
- [x] 7.2 `cwcli-core-axi` skill: the batch section (the plan/apply decision, the copy-out-vs-`core.backup` judgment, the flat-spots verdict, the deferral); `cwcli-lifecycle` skill's `references/rm.md`: re-pointed at the core seams, every root-cause "why" preserved.
- [x] 7.3 `docs/technical/README.md` core module list + `tests/README.md` updated; README checked for stale rm internals prose (user-facing behavior unchanged, so likely no edit).
