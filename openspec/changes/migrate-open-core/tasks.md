## 1. Characterization first: open's behavior pinned green BEFORE anything moves

- [x] 1.1 Run the existing open coverage at the batch's base commit and record the baseline: `tests/test_open_inspect_fallback.py` (the fallback-abort contract) and `test_inspect_partial_refresh.py`'s open classes (`TestOpenAppInMemoryRefresh`, `TestOpenAppMatchesSelectedBench`).
- [x] 1.2 Add characterization tests for the currently-untested behaviors that must survive, green against the UNMIGRATED code, committed separately (batch 4's discipline): editor flag mutual exclusion (exit 1), requested-editor-not-installed (exit 1, install-URL message), no-flag single-option auto-pick of docker, no-flag multi-editor non-TTY refusal naming the flags (exit 1), questionary cancel -> "Operation cancelled." (exit 1), fallback-populate success-but-still-nothing -> default path + warning, non-`CwcliError` inspect failure -> default path + warning, and the `--docker` handover argument assertion (mocked `exec_into_container`).

## 2. `core/open.py`: the plan under its own tests

- [x] 2.1 `LaunchTarget` frozen/slots/kw_only DTO per design Decision 2: `project`, `container_name` (a NAME string), `working_dir`, `editor`; serializable via `asdict`, no live Docker object.
- [x] 2.2 `open_plan(project_name, *, bench=None, bench_path=None, app=None, editor=None, auto_start=False, on_event=None) -> Result[LaunchTarget]` resolving in today's order: `get_frappe_container` -> `resolve_container_state(offer_choice=True)` (confirm_start race backstop) -> `resolve_bench` -> fallback populate -> `--app` -> editor.
- [x] 2.3 The fallback populate per design Decision 4: `OpenNotice` event, `core.inspect(refresh="auto", auto_start=auto_start, offer_choice=False)` for the side effect, re-resolve, `DEFAULT_BENCH_PATH` + `bench.default_used` warning only when still unresolvable; a hard `CwcliError` PROPAGATES (the merged fallback-abort contract); a non-`CwcliError` exception degrades to the default with a warning. Disclosed hardening: the race-window `NOT_RUNNING` typed abort replaces the discarded-choice-then-default path.
- [x] 2.4 The `--app` pass verbatim per design Decision 5: no-cache `NOT_FOUND` naming `cwcli inspect`, match-by-path (never `[0]`), in-memory `core.partial_refresh` (degrade on any exception with a trace event, never persists), membership check listing available apps on failure, `working_dir = f"{bench_path}/apps/{app}"`.
- [x] 2.5 The editor resolution per design Decision 3: stdlib `shutil.which` detection run once; requested-not-installed -> `CwcliError(NOT_FOUND, "editor.not_installed")` with today's install-URL hint; unknown value -> `CwcliError(USAGE)`; `None` + nothing installed -> `"docker"`; `None` + >=1 installed -> `NEEDS_CHOICE` `select_editor` (`param="editor"`, options = installed editors + Docker with today's labels).
- [x] 2.6 `OpenEvent = OpenNotice | OpenTrace` + the `OnEvent` idiom; module docstring records the boundary (frontend owns the mechanism, the core never execs, no `axi open` and the o9 reason why).
- [x] 2.7 `tests/test_core_open.py`: every branch above on container fakes; all three `NEEDS_CHOICE` kinds; the fallback abort/degrade matrix; `asdict`-is-plain-data; "the core prints nothing at all".

## 3. Reseat `commands/open.py` as a renderer plus the handover

- [x] 3.1 Flag fusion (four booleans -> `editor`) and the mutual-exclusion error stay frontend; `ensure_containers_running` prologue + capped `confirm_start` once-retry loop mirror `run.py:54-89`.
- [x] 3.2 Render `select_bench` as `run.py:91-96` does (bench list + "Pass --bench" hint, exit 1); the wording alignment off `resolve_bench_path`'s rendering is the one named drift.
- [x] 3.3 Resolve `select_editor` as today: questionary select on a TTY (cancel -> "Operation cancelled.", exit 1), the non-TTY refusal naming the flags (exit 1, today's message), then ONE re-invoke of `open_plan` with `editor` filled.
- [x] 3.4 Render `OpenNotice` unconditionally, `OpenTrace` under `-v`, warnings unconditionally (today's yellow lines); prompts stay outside the spinner; `handle_docker_errors` stays on the command.
- [x] 3.5 The four-way handover switch stays the frontend's final lines: `docker` -> `exec_into_container(target.container_name, working_dir=target.working_dir)`; editors -> `vscode_utils.open_in_vscode(target.editor, target.container_name, target.working_dir, verbose=verbose)`. Exit codes unchanged throughout.
- [x] 3.6 Re-point the existing suites' patch targets to their moved subjects (`core.open` seams instead of `open_mod` internals), assertions untouched; any test changed BY DESIGN is named here.

## 4. The asserted absences and the dead helpers

- [x] 4.1 A test asserts the `axi` Typer registry has no `open` command (the `axi apps install`/`uninstall` non-verb precedent), so the exclusion cannot slip in unnoticed.
- [x] 4.2 Grep proves `vscode_utils.select_vscode_editor` and `is_vscode_installed`/`is_vscode_insiders_installed`/`is_cursor_installed` have no remaining caller after the reseat; delete them in their own commit. If any caller remains, they stay and this task records why.
- [x] 4.3 Grep proves `commands/open.py` no longer imports `db_utils`, `core.inspect`'s helpers, or `resolve_bench_path` for resolution (only rendering/handover imports remain).

## 5. Suite health

- [x] 5.1 The §1 characterization tests green against the MIGRATED code unchanged.
- [x] 5.2 Full fast tier green (`uv run pytest`); black, ruff, mypy at zero. `tests/README.md` coverage map updated.

## 6. E2E on a real instance, BOTH modes (captain standard)

- [x] 6.1 One throwaway isolated instance (default frappe v16, `cwe2e-` prefix, `CWCLI_HOME` isolation per skill `cwcli-e2e-testing`), against the worktree's own editable install, reused throughout and torn down at the end. Never touch existing real instances; no broad Docker cleanups ever.
- [x] 6.2 Non-interactive: `open --docker` under a pty driving the handed-over shell (verify the working dir, then `exit`; also with `--app` and with `--path`); `open --code` on a host without VS Code (not-installed error, exit 1); multi-editor-flag conflict (exit 1); non-TTY without an editor flag while an editor stub is on PATH (refusal, exit 1); empty cache -> fallback populate -> open (cache populated, correct bench dir); stopped project non-TTY without `--yes` (refuses, exit 1); stopped project with `--yes` (auto-starts, opens).
- [x] 6.3 Interactive via pty (awaiting `ESC[?2004h` before keystrokes): the start prompt accepted and declined; the editor select prompt (with a stub editor on PATH) navigated and cancelled.
- [ ] 6.4 Re-run AFTER no-mistakes and after any review fixes (the standard: review fixes are a trigger, not an exemption).

E2E record (2026-07-16, pre-pipeline run): one throwaway `cwe2e-open-o2` (genuine frappe `version-16` via `cwcli init`, `CWCLI_HOME`-isolated, driven against the worktree's `.venv/bin/cwcli`), 15 legs green. Non-interactive: truly-fresh-home fallback inspect hard abort (exit 1, shell never opened - the PR #93 contract end to end), config-but-no-cache fallback populate -> opened `/workspace/frappe-bench` AND `axi benches` proved the cache write, warm-cache `--docker`/`--app frappe`/`--path` pty shells each verified by in-shell `pwd`, `--code` with no VS Code on PATH (exit 1 + install URL), `--code --docker` conflict (exit 1), non-TTY no-flag with a stub editor on PATH (refusal naming all four flags, exit 1), stopped non-TTY without `--yes` (refusal, exit 1), stopped `--yes` (auto-start then shell). Interactive via pty awaiting `ESC[?2004h`: start prompt declined (`Operation cancelled.`, exit 1) and accepted (started -> shell), editor select navigated to Docker (arrow + Enter -> shell) and cancelled with Ctrl-C (`Operation cancelled.`, exit 1).

## 7. Docs and memory

- [x] 7.1 CLAUDE.md ledger: `open` moves to the migrated list; its "UNBLOCKED, next" entry becomes the migrated entry recording the settled shape, the no-`axi open` assertion, and the fallback abort/degrade contract; the un-migrated list restated from the code (`restore`, `rm`, `config`, `init`, `self_update`'s mutating half).
- [x] 7.2 `cwcli-core-axi` skill: `core/open.py` added to the migrated-slice list; the `select_editor` choice kind and the handover boundary recorded.
- [x] 7.3 `docs/technical/README.md` core module list + `tests/README.md` updated; README checked for stale `open` internals prose (user-facing behavior is unchanged, so likely no edit).
