## 1. PR #83's fix pinned BEFORE anything moves

- [ ] 1.1 Run `tests/test_logs.py` at `468d704` and record the baseline: 1035 passed, 69 deselected suite-wide; `commands/logs.py` 92 stmts / 15 miss / 83.70%.
- [ ] 1.2 Confirm the five exit-code regressions from PR #83 are green and untouched: `test_failing_tail_exits_non_zero`, `test_failing_tail_propagates_the_real_code`, `test_ctrl_c_through_dockers_tty_is_a_clean_exit`, `test_keyboard_interrupt_is_a_clean_exit`, `test_successful_tail_exits_zero` (`test_logs.py:178-217`).
- [ ] 1.3 These five assert through the public command surface and MUST stay green, unchanged in substance, at every step below. They are the acceptance gate on "the migration did not re-open the hole."

## 2. The pure helper and the two reads move first (0% -> covered)

- [ ] 2.1 Move `_program_log_matches` (`logs.py:48-65`) to `core/logs.py` unchanged; move `test_program_log_matches` (`test_logs.py:307`) to `tests/test_core_logs.py` unchanged.
- [ ] 2.2 Move `_existing_files` (`logs.py:15-25`) to `core/logs.py`, re-pointed from `subprocess.run(["docker","exec",...])` to `container.exec_run`, keeping the `shlex.quote` and the order-preserving semantics. Keep `sh -c` + the `if [ -f ... ]` probe: one exec, existing behaviour.
- [ ] 2.3 Move `_discover_bench_log_files` (`logs.py:28-45`) to `core/logs.py`, likewise re-pointed, keeping the `_PROC_LOG_SUFFIX` exclusion and the sort.
- [ ] 2.4 Cover both with a container fake in the `tests/bench_fakes.py` idiom (`exec_run`-interpreting, as `test_core_supervision.py` does). These are the two functions that were 0% because a shell-out can only be monkeypatched away; the point of moving them is that they become testable.

## 3. `core/logs.py`

- [ ] 3.1 `LogsPlan` frozen/slots/kw_only dataclass: `project`, `container_name`, `bench_path`, `log_files`, `follow`, `lines`, `not_cwcli_supervised`. No argv, no live Docker object (design Decision 3).
- [ ] 3.2 `logs_plan(project_name, *, bench=None, bench_path=None, process=None, follow=False, lines=100, auto_start=False) -> Result[LogsPlan]`.
- [ ] 3.3 Container resolve via `core/docker.py:get_frappe_container`; run-state via `resolvers.resolve_container_state(auto_start=..., offer_choice=True)` -> `NEEDS_CHOICE`/`confirm_start`.
- [ ] 3.4 Bench resolve via `resolvers.resolve_bench` + `DEFAULT_BENCH_PATH` fallback with the `bench.default_used` warning, exactly as `core/run.py:80-93` and `core/restart.py:90-104` do.
- [ ] 3.5 Program selection via `supervision.procfile_programs` + `program_for_label`; unknown/ambiguous `--process` -> `Result(NEEDS_CHOICE, Choice(kind="select_process", param="process", ...))` listing normalized valid labels, mirroring `core/restart.py:120-135`.
- [ ] 3.6 Existence probe, then the not-cwcli-supervised fallback via `supervision.discover_unsupervised_stack`; set `not_cwcli_supervised=True` when it fires. PURE READ: never launch, install, or restart (`CLAUDE.md:76`).
- [ ] 3.7 The two no-logs outcomes stay distinct: `CwcliError(NOT_FOUND, "logs.none_yet")` when a manager is up, `CwcliError(NOT_RUNNING, "logs.no_manager")` with the `cwcli start` hint when none is.
- [ ] 3.8 Resolve no MORE than today: no default site, no path metachar validation, no bench-dir probe (design Decision 4).
- [ ] 3.9 Module docstring states the boundary the way `core/run.py:1-33` does: why the tail stays in the frontend, why `exec_stream` is refused for `--follow`, with the measured costs (orphan per Ctrl+C; `exec.stream_lost` after ~10s) and the `tty`/`demux` conflict. Without it a later agent "finishes the job" and reintroduces the bug.

## 4. Reseat `commands/logs.py` as a renderer

- [ ] 4.1 Call `core.logs_plan`; render `NEEDS_CHOICE`/`confirm_start` and `select_process` the CLI's way. `select_process` must print the identical message plus valid-label list and exit 1 (`test_logs.py:130` pins it).
- [ ] 4.2 Render `not_cwcli_supervised` as today's `(bench not under cwcli supervision - tailing its raw log files)` stderr note.
- [ ] 4.3 Render both `CwcliError`s as today's two messages, including the "may not be running" hint appearing ONLY when no manager is up (`test_logs.py:295` pins the inverse).
- [ ] 4.4 Keep the tail block byte-for-byte: `-it` gating on `sys.stdin.isatty()`, the argv, `subprocess.run`, `except KeyboardInterrupt`, the 130 branch, `raise typer.Exit(code=result.returncode)`.
- [ ] 4.5 Update `test_logs.py:166-175`'s comment to point at this design rather than re-argue the non-consumer decision in a sentence.

## 5. Tests

- [ ] 5.1 `tests/test_core_logs.py`: the moved helper tests, the two reads against a container fake, the `NEEDS_CHOICE` forks (multi-bench `select_bench`, stopped `confirm_start`, unknown `select_process`), both `CwcliError` kinds, the fallback firing and NOT firing, and `not_cwcli_supervised`.
- [ ] 5.2 `tests/test_logs.py` keeps every behaviour it pins today, PR #83's five included. Re-wire its monkeypatches from `logs_mod._existing_files` to the core seam; the assertions do not change.
- [ ] 5.3 Assert `core/logs.py` never imports `subprocess` (the tail must not creep back into the core). The existing `tests/test_core_envelope.py` ban already covers `rich`/`questionary`/`typer`.
- [ ] 5.4 Coverage of what moved goes measurably up: `logs.py:17-25,38-45` are 0% today and are the only substantive misses in the file.
- [ ] 5.5 Full fast tier green: `uv run pytest` (baseline 1035 passed, 69 deselected). Lint, format, mypy at zero.

## 6. E2E on a real instance, BOTH modes

- [ ] 6.1 Per `CLAUDE.md`'s captain standard and skill `cwcli-e2e-testing`: interactive via a pty (awaiting `ESC[?2004h` before keystrokes) AND non-interactive via flags, on an isolated throwaway instance, against the worktree's own editable install.
- [ ] 6.2 Prove the migration preserved: `logs` and `logs -f` on a supervised bench; `--process` valid and invalid; the honcho/`bench start` fallback; a failing tail exits non-zero; an interactive Ctrl+C on `-f` exits 0 (the 130 path Probe D verified).
- [ ] 6.3 Re-run AFTER no-mistakes and after any review fixes (captain standard: review fixes are a trigger, not an exemption).

## 7. REPORTED, not fixed: the non-TTY orphan leak

- [ ] 7.1 Record the defect: today's `cwcli logs -f` under a pipe/agent leaks an orphan `tail -F` in the container on every Ctrl+C (Probe G: 1, 2, 3). Without `-it` there is no raw mode to forward `^C`, and nothing else signals `tail`. cwcli reports a clean stop; the container is left dirty.
- [ ] 7.2 It is PRE-EXISTING, orthogonal to this batch (which does not touch the mechanism), and not a one-liner: Docker has no kill-exec API, so reaping needs a uniquely-marked exec plus a targeted `pkill -f <marker>`, which is a real hazard (`CLAUDE.md`'s lessons warn about broad `pkill`) and a behaviour change on interrupt needing its own both-modes E2E.
- [ ] 7.3 Do NOT fix it here. Its own batch, its own evidence, following batch 5's `core/update.py:291` precedent.

## 8. Docs and memory

- [ ] 8.1 `CLAUDE.md:72`'s exec-stream entry: its `logs` non-consumer claim is CORRECT and is now measured. Replace the asserted reason with the evidenced one and point at this design. Note the bounded read is a legitimate future consumer (design Decision 6).
- [ ] 8.2 `CLAUDE.md:75`'s core entry: move `logs` from "a partial" to migrated, stating what migrated (the resolve) and what deliberately did not (the tail).
- [ ] 8.3 `CLAUDE.md`'s `open` entry (`:75`) is correctable on o9's evidence: it is deferred on `inspect` (`open.py:135,217`), not on handover. Adjacent and evidenced; propose it, do not smuggle it.
- [ ] 8.4 Skills: `cwcli-core-axi` (the new slice), `cwcli-lifecycle` (its `logs` section). Do NOT touch `.claude/skills/cwcli-core-axi/SKILL.md` if crew `cwcli-axi-pass-x9` still owns it: check first, escalate on collision.
- [ ] 8.5 `tests/README.md` coverage map: `core/logs.py` + `tests/test_core_logs.py`; refresh the test counts.
