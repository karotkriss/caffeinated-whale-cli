## 1. Characterization first: the tier state machine pinned green BEFORE anything moves

- [x] 1.1 Run the existing inspect suites at the batch's base commit and record the baseline: `test_inspect_partial_refresh.py` (the tier contract: T2 cheapness, T2 passivity under `--yes`, `--no-refresh` verbatim + zero container calls, drift/carry-forward, vanished-bench index shift, drift-escalation degrade-without-write), `test_inspect_label_recovery.py`, `test_inspect_apps_error.py`, `test_db_security.py`, `test_restore_inspect_fixes.py`, `test_rm_safety.py`, `test_rm_stopped.py`, `test_auto_inspect.py`. (Baseline: 153 passed at `bd89d8c`.)
- [x] 1.2 Where a tier behavior is exercised only through the command surface, add characterization tests that pin it through a surface that will survive the migration, green against the UNMIGRATED code, committed separately (batch 4's discipline). (`tests/test_inspect_characterization.py`, committed as its own commit.)
- [x] 1.3 Byte-capture the human `--json` output shape and the cache dict shape for a representative multi-bench fixture; these are the before/after comparison anchors. (Same file: exact `--json` bytes per tier incl. the gathered-vs-cache-read key-order swap, and the persisted dict shape/key order.)

## 2. `core/inspect.py`: the helpers and the T2 pass move with their tests

- [x] 2.1 Move `_run_command` with the decode fixed to `errors="replace"` (`core/backup.py:_decode` idiom); the `--verbose` echoes become typed events via `on_event`. Disclosed hardening #1.
- [x] 2.2 Move `_is_bench_directory`, `_get_sites`, `_get_installed_apps` (honest `[]` on failure, never a sentinel - `test_inspect_apps_error.py` pins it), `_get_available_apps`, `_get_common_site_config`, `_get_site_config`, `_gather_bench_data` (marker-label recovery and currentsite.txt recording intact).
- [x] 2.3 Move `_find_bench_instances` as `discover_benches(container)`, `sorted(set(...))` byte-identical (the numeric-label order), `config_utils.load_config()` read intact.
- [x] 2.4 Move `partial_inspect_known_benches` as `partial_refresh(container, cached_benches) -> tuple[list[dict], bool]`, cache-shaped dicts kept, carry-forward / drop-vanished / never-write semantics unchanged; its docstring's contract moves with it.
- [x] 2.5 Re-point the moved tests: patches of `inspect_mod._find_bench_instances` / `partial_inspect_known_benches` / helper seams move to `core.inspect` paths, assertions untouched. Any test changed BY DESIGN is named here.

## 3. `core/inspect.py`: the tier machine and the write

- [x] 3.1 `InspectReport`/`BenchInfo`/`SiteInfo` frozen/slots/kw_only DTOs per design; serializable (`asdict` -> plain data), no live Docker object.
- [x] 3.2 `inspect(project_name, *, refresh="auto", auto_start=False, offer_choice=True, on_event=None) -> Result[InspectReport]` owning T1/T2/T3 exactly as measured: T1 serve-verbatim; T2 passive (`resolve_container_state` never prompts, never starts, even with `auto_start=True`), never writes, degrade-to-cache on any T2 exception; T3 full discovery + gather, `db_utils.cache_project_data` on success only.
- [x] 3.3 The `confirm_start` fork: T3 against a stopped project with `offer_choice=True` returns `NEEDS_CHOICE`/`confirm_start`; `offer_choice=False` raises `CwcliError(NOT_RUNNING)` (the spinner-borne contract).
- [x] 3.4 Drift escalation: remember the cached benches; if rediscovery finds nothing, degrade to them WITHOUT persisting, `degraded=True`; keep the hard `NOT_FOUND` error only for `--update`/cache-miss.
- [x] 3.5 Wrap raw docker exceptions escaping the fan-out into `CwcliError(DOCKER)`; the cache is untouched on that path (crash-without-corruption preserved, now typed). Disclosed hardening #2.
- [x] 3.6 Module docstring records the boundary: why the write lives in the core (design Decision 2), why this is a plain function (Decision 1), why T2 is passive by construction.

## 4. Reseat `commands/inspect.py` as a renderer

- [x] 4.1 Backup pattern: resolve the `confirm_start` choice interactively OUTSIDE the spinner (questionary confirm, capped retry), honoring `--yes` -> `auto_start=True` and `--no-prompt-start` -> `offer_choice=False`; then `core.inspect` under the `TipSpinner`.
- [x] 4.2 Render `on_event` as today's `VERBOSE:` stderr lines (small wording drift disclosed); render the list-apps failure warning as today's stderr warning.
- [x] 4.3 The `-i` loop stays byte-identical in the frontend, operating on the returned bench data, keeping today's write ordering (markers per bench, ONE bulk `cache_project_data` at the end).
- [x] 4.4 `--json` byte-identical against the task-1.3 capture; tree renderer unchanged including the "(default)" resolution order; `--show-apps` still declared, still dead (captain hold).
- [x] 4.5 Exit codes unchanged: not-running refusal exit 1, no-benches hard error exit 1, degrade paths exit 0.

## 5. Re-point the seven consumer edges

- [x] 5.1 `utils/cache.py:recache_project` body -> `core.inspect(refresh="full", offer_choice=False)`, `CwcliError(NOT_RUNNING)` -> `False`; signature and never-prompt / False-when-stopped contract preserved (`test_rm_stopped.py` pins it). `rm.py`/`apps.py`/`core/update.py` callers untouched; `core/update.py`'s CLI-layer import is GONE (assert it).
- [x] 5.2 `utils/auto_inspect.py:_inspect_project` -> `core.inspect(refresh="full", offer_choice=False)`; `test_auto_inspect.py` (the Windows job's suite) stays green.
- [x] 5.3 `commands/open.py:135` -> `core.inspect(refresh="auto")` + the unchanged `resolve_bench_path` re-resolve; `open.py:217` -> `core.partial_refresh`, match-by-path and degrade-on-error preserved (`test_inspect_partial_refresh.py`'s open tests pin them).
- [x] 5.4 `commands/update.py:475` fallback -> `core.inspect`; the `--json` stdout-purity skip stays frontend.
- [x] 5.5 `commands/restore.py:723/:1000/:1675` -> `core.inspect`, mechanical, three copies stay three copies (restore's own migration collapses them later).
- [x] 5.6 `commands/rm.py:804` -> `core.inspect.discover_benches`; `test_rm_safety.py:692` (live-discovery fallback backs up ALL benches) stays green.
- [x] 5.7 Grep proves no remaining import of the `inspect` COMMAND or its private helpers from any logic module; the only importer of `commands/inspect.py` left is `main.py`.

## 6. `axi inspect`

- [x] 6.1 `cwcli axi inspect <project> [--update] [--no-refresh]` in `commands/axi.py`: `core.inspect` with the flag mapping, `emit_result` on the `InspectReport`, one TOON document.
- [x] 6.2 Exit mapping: 0 for OK/WARNING (degrade-to-cache is a WARNING with the data served), 1 for `CwcliError`, 2 for `NEEDS_CHOICE`/`confirm_start` via `emit_axi_choice_as_usage_error` naming `cwcli start <project>`. NO `--yes` registered (assert it, per the `axi apps update` incident).
- [x] 6.3 Re-point `axi benches`' not-inspected hint (`axi.py:508-510` region) at `cwcli axi inspect`.
- [x] 6.4 Regenerate `skills/cwcli/SKILL.md` via `scripts/build_skill.py`; `tests/test_axi_skill.py --check` green.

## 7. Tests

- [x] 7.1 `tests/test_core_inspect.py`: every tier branch on container fakes (`bench_fakes.py`/`bench_fakes_mb.py` + `test_inspect_partial_refresh.py`'s recording fake as models); `confirm_start` NEEDS_CHOICE; `offer_choice=False` -> `NOT_RUNNING`; drift-degrade without persist; T2-never-writes pinned AT THE CORE; T2 passive even with `auto_start=True`; the `errors="replace"` decode; the `CwcliError(DOCKER)` wrap; "the core prints nothing at all"; asdict-is-plain-data.
- [x] 7.2 `tests/test_axi_inspect.py`: one-TOON-document (`assert_is_one_toon_document`), exit mapping 0/1/2, stopped-project -> exit 2 naming `cwcli start`, no `--yes` registered.
- [x] 7.3 Existing suites green with patch targets moved per §2.5; the §1 characterization tests green against the MIGRATED code unchanged.
- [x] 7.4 Full fast tier green (`uv run pytest`); black, ruff, mypy at zero. `tests/README.md` coverage map updated.

## 8. E2E on a real instance, BOTH modes (captain standard)

- [x] 8.1 One throwaway isolated instance (default frappe v16, `cwe2e-` prefix, `CWCLI_HOME` isolation per skill `cwcli-e2e-testing`), against the worktree's own editable install, reused throughout and torn down at the end. Never touch existing real instances; no broad Docker cleanups ever.
- [x] 8.2 Non-interactive: `inspect` (T3 cold), `inspect` again (T2 cache hit), `--no-refresh` (T1), `--update` (forced T3), `--json` byte-shape, install an app then plain `inspect` (drift escalation picks it up), stopped project + `--yes` (auto-start), stopped project non-TTY without `--yes` (refuses, exit 1), `axi inspect` (one TOON doc; stopped -> exit 2).
- [x] 8.3 Interactive via pty (awaiting `ESC[?2004h` before keystrokes): the T3 start prompt both accepted and declined; `inspect -i` labeling (set, blank-keep, invalid rejected) and label persistence to marker + cache.
- [x] 8.4 Re-run AFTER no-mistakes and after any review fixes (the standard: review fixes are a trigger, not an exemption). (Re-run done at 1ed5e06 after the review fix; see docs/e2e/inspect-core-migration-m9.md.)

## 9. Docs and memory

- [x] 9.1 CLAUDE.md ledger: `inspect` moves to the migrated list; `core/update.py`'s reach-back note settled (the one core-imports-CLI site is gone); `open`'s entry updated from "deferred on inspect" to unblocked/next.
- [x] 9.2 `cwcli-inspect-benches` skill: the migration incident recorded; the stale `start`-as-caller line corrected (greps to zero); the Typer-default trap note updated (the trap class is gone for inspect's callers).
- [x] 9.3 `cwcli-core-axi` skill: `core/inspect.py` added to the migrated-slice list; the `axi inspect` verb and the `axi benches` hint re-point recorded.
- [x] 9.4 Hazards board: the strict-decode and raw-escape entries resolved by this batch are pruned; `cache_project_data` non-transactionality stays reported as its own follow-up.
