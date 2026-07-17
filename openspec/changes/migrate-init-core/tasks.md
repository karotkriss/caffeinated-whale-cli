## 1. Characterization first: init's behavior pinned green BEFORE anything moves

- [x] 1.1 Run the four existing init suites at the batch's base commit and record the baseline: `tests/test_init_reuse_bench.py` (45-test resolver/poll coverage), `test_init_admin_password.py` (env-transport + generation + refusals), `test_init_frappe_version.py` (ref shapes, mutual exclusion, gating), `test_init_mariadb_flag.py`.
- [x] 1.2 Add `tests/test_init_characterization.py` for the currently-untested command-level behaviors, green against UNMIGRATED code, committed separately (batch 4's discipline), through migration-surviving seams (subprocess mock, container fakes, questionary, `isatty`): the exec ORDER and exact command strings (4 set-configs, new-site with `$CWCLI_*` refs, 2 final configs, ERPNext pair), skip-on-exists gating (`bench_exists` skips `bench init`; `site_exists` skips new-site AND the generated-password print), the port-conflict three-line error (exit 1), the non-TTY no-`--admin-password` refusal (exit 1), the `--no-reuse-bench` existing-bench refusal (exit 1), the ENOSPC message on a failed drained exec, the cache-clear call, and the add-path stdout line on both its outcomes.

## 2. `core/init.py`: the two-call slice under its own tests

- [x] 2.1 `InstanceUp` and `InitReport` frozen/slots/kw_only DTOs per design (plain strings/bools; NO password field; serializable via `asdict`).
- [x] 2.2 `init_instance(project_name, *, port=8000, auto_start=False, on_event=None) -> Result[InstanceUp]`: slug validation, port-conflict `CONFLICT`, project dir + compose download (skip when present), port/image customization (Docker Hub fail-open to the pinned fallback), compose pull + up via captured subprocess (`DOCKER` on failure), the bounded silent readiness poll; timeout -> `confirm_start` choice (`auto_start=False`) or typed `NOT_RUNNING` (`auto_start=True`, the structural cap).
- [x] 2.3 `init_bench(...) -> Result[InitReport]` per design Decisions 2-5: race-backstop `resolve_container_state(offer_choice=True)`, parent mkdir, the bench probe + tri-state `reuse_bench` (True reuse / False `CONFLICT` / None-and-exists `confirm_reuse_bench`), `add_custom_path` + its outcome event, version gating verbatim (pyenv/nvm/yarn buffered, soft-fail warnings; setuptools pin at major 13), `bench init` / 4 set-configs / site probe / `bench new-site` (secrets via `exec_stream(environment=)`) / 2 final configs / ERPNext pair through `exec_stream`, cache clear, the report facts (`bench_created`, `site_created`, `erpnext_installed`).
- [x] 2.4 The moved version resolvers public on the core: `resolve_frappe_ref` (`ValueError` -> `CwcliError(USAGE)`, same message), `_frappe_major_version`, `_select_mariadb_flag`, `DEFAULT_FRAPPE_BRANCH`; the public name validators (`validate_project_slug`, `validate_bench_slug`, `validate_new_site_name`) with today's exact messages.
- [x] 2.5 The typed event family (`InitStepStart`/`InitOutput`/`InitStepEnd`/`InitNotice`/`InitTrace` + `OnEvent`), the command-echo trace carrying `$`-refs never values, and the module docstring recording the two-call seam's motivation and the no-`axi init` deferral.
- [x] 2.6 `tests/test_core_init.py`: every branch above on container/subprocess fakes; all three choice surfaces; the tri-state matrix; secrets ride `environment=` and appear in NO event/DTO/warning (the Decision 3 audit, pinned); exec-order preservation; `asdict`-is-plain-data; "the core prints nothing at all"; the honest lost-stream `DOCKER` error where `exit code None` used to print.

## 3. Reseat `commands/init.py` as a renderer

- [x] 3.1 Frontend keeps: the Typer signature, `--frappe-branch`/`--version` fusion + mutual-exclusion error, the project-name prompt (Ctrl-C exit 0), admin-password generate-or-refuse + print-once gated on `generated and report.site_created`, up-front validator calls (fail-fast ordering byte-identical), elapsed time, the success block, `handle_docker_errors`.
- [x] 3.2 Event-driven rendering collapses the four dual verbose/non-verbose blocks: verbose writes `InitOutput` raw (stdout/stderr split, carriage returns preserved) and renders traces; non-verbose drives the `TipSpinner` labels from step events; the add-path line renders to stdout as today.
- [x] 3.3 Resolve `confirm_start` (both stages) via `ensure_containers_running(auto_start=<--auto-start>)` then one re-invoke (`auto_start=True` on stage 1; the capped once-retry on stage 2); resolve `confirm_reuse_bench` as today's confirm + rename loop (re-invoking `init_bench` per round; cancel/blank -> exit 0 "No changes made."), and on a non-TTY as today's refusal naming both flags, exit 1.
- [x] 3.4 Re-point the four suites: pure helper tests move with their subjects to the core; the resolver-loop tests split BY DESIGN into core-choice tests and frontend-prompt-loop tests (named here when done); `test_init_admin_password.py`'s env-ref assertions re-point at the core's command construction; assertions otherwise untouched.
  Named, per the discipline: the core-choice half is `test_core_init.py::TestInitBenchTriState` + `TestInitBenchChoicesAndErrors` + `TestInitInstance::test_readiness_poll_is_bounded`; the frontend-prompt half is `test_init_reuse_bench.py::TestReusePromptLoop` (driven through the real command body) + `TestInitContainerReadiness` (prompts-after-spinner-close + the `auto_start=True` re-invoke); `test_init_frappe_version.py::TestResolveFrappeRef::test_malformed_raises` changed BY DESIGN (`ValueError` -> `CwcliError(USAGE)`, same message) and `TestBenchInitRefEndToEnd` now asserts the ref crossing the frontend->core seam (the command construction is pinned by `test_core_init.py::TestExecOrderAndSecrets`); the two `test_exec_stream_decode.py` init cases re-pointed at `core._run_exec` feeding the CLI's verbose renderer (the batch 4/5 precedent), assertions untouched.
- [x] 3.5 Grep proves `commands/init.py` no longer imports `commands.config`, `utf8_stream_decoder`, or `db_utils`; `utf8_stream_decoder`'s only consumer is `core/exec_stream.py`.

## 4. Asserted absences and dead code

- [x] 4.1 A test asserts the `axi` Typer registry has no `init` command, its docstring recording DEFERRED (the captain's follow-up decision) rather than never, per design Decision 9.
- [x] 4.2 Delete with their callers: `InitInputs`, `_exec_in_container`, `_run_host_command` (including its dead `use_spinner` branch), the frontend copies of the validators/resolvers/gating helpers; grep proves no caller remains for each.

## 5. Suite health

- [x] 5.1 The §1 characterization tests green against the MIGRATED code unchanged.
- [x] 5.2 Full fast tier green (`uv run pytest`); black, ruff, mypy at zero; `tests/README.md` coverage map updated.

## 6. E2E on a real instance, BOTH modes (captain standard)

- [x] 6.1 One throwaway isolated instance (`cwe2e-` prefix, `CWCLI_HOME` isolation per skill `cwcli-e2e-testing`), against the worktree's own editable install, reused throughout and torn down at the end; no broad Docker cleanups; disk pressure means stop and report blocked.
- [x] 6.2 `tests/e2e/test_init_e2e.py` green UNCHANGED against the migrated code (it drives the real binary in both modes: non-interactive flag-driven init, the interactive generated-password pty leg, version gating on the leg's major).
- [x] 6.3 Targeted legs the suite does not cover, on the same instance: the interactive reuse-bench flow via pty (`ESC[?2004h` awaited) - reuse-Yes, decline-and-rename continuing on a fresh bench, cancel exit 0; the non-TTY existing-bench refusal without a flag (exit 1); `--reuse-bench` and `--no-reuse-bench` non-interactive; an idempotent re-run printing no password.
- [ ] 6.4 Re-run AFTER the no-mistakes pipeline and after any review fixes (the standard: review fixes are a trigger, not an exemption).

## 7. Docs and memory

- [x] 7.1 CLAUDE.md ledger: `init` moves to the migrated list with the two-call seam, the secret-transport preservation, the `confirm_reuse_bench` kind, and the deferred-`axi init` assertion; the un-migrated list restated from the code (`restore`, `rm`, `config`, `self_update`'s mutating half); the exec-stream entry's "init is the one consumer NOT yet on it" note retired.
- [x] 7.2 `cwcli-core-axi` skill: the batch section (two-call motivation, the flat spots, the deferral); `cwcli-lifecycle` skill's `references/init.md`: re-pointed at the core seams, root-cause "why"s preserved.
- [x] 7.3 `docs/technical/README.md` core module list + `tests/README.md` updated; README checked for stale init internals prose (user-facing behavior unchanged, so likely no edit).
