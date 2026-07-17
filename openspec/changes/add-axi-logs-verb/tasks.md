# Tasks: `cwcli axi logs` over a bounded `core.read_logs`

Implementation is sequenced AFTER `cwcli-axi-usage-error-toon-u3` lands (both touch `commands/axi.py`);
firstmate sequenced it. Implemented in phase B; the real-instance E2E (7.2) is the remaining validation,
run in the validation phase (this phase is unit-verified: fast tier green, lint/mypy clean).

## 1. Baseline pinned before anything moves

- [x] 1.1 Record the `tests/test_logs.py` + `tests/test_core_logs.py` baseline (pass count, `core/logs.py`
      coverage) so the resolve extraction is proven behaviour-preserving.
- [x] 1.2 Confirm the five exit-code regressions PR #83 pinned (`test_logs.py:178-217`) are green and stay
      green, unchanged in substance, at every step: extracting the shared resolve must not touch
      `cwcli logs`' tail, exit codes, or messages.

## 2. Extract the shared resolve (behaviour-preserving)

- [x] 2.1 Extract a private `_resolve_log_files(project_name, *, bench, bench_path, process, auto_start)`
      from `logs_plan`, returning the resolved `(container, bench_path, ordered log_files,
      not_cwcli_supervised, warnings)` or a `NEEDS_CHOICE` `Result` or a raise - everything `logs_plan`
      does today between its args and building `LogsPlan`.
- [x] 2.2 Reseat `logs_plan` to call `_resolve_log_files` and wrap the result into `LogsPlan` (adding
      `follow`/`lines`), byte-identical outputs. The two historical no-logs raises (`logs.none_yet`,
      `logs.no_manager`) stay reachable from `logs_plan` exactly as before.
- [x] 2.3 Run the step-1 baseline: `test_logs.py` + `test_core_logs.py` green, unchanged.

## 3. `core.read_logs`

- [x] 3.1 Add `ProcessLog` (`process`, `file`, `lines`) and `LogsRead` (`project`, `container_name`,
      `bench_path`, `lines_requested`, `not_cwcli_supervised`, `logs: list[ProcessLog]`) frozen/slots/
      kw_only dataclasses. No argv, no live Docker object (design Decision 1).
- [x] 3.2 `read_logs(project_name, *, bench=None, bench_path=None, process=None, lines=100,
      auto_start=False) -> Result[LogsRead]` calling `_resolve_log_files`, then one buffered
      `container.exec_run(["tail", "-v", "-n", str(lines), *log_files])`.
- [x] 3.3 Decode with the module's existing decoder (`supervision._decode`), split on the `==> <path> <==`
      headers, map each path back to its program via the resolve, build the `ProcessLog` groups
      oldest-first.
- [x] 3.4 Running-but-quiet (manager up, no files): return `Result(OK, LogsRead(..., logs=[]))` with a
      `logs.none_yet` warning, NOT a raise (design Decision 3, case 2). No-manager: raise
      `CwcliError(NOT_RUNNING, "logs.no_manager")` unchanged (case 3).
- [x] 3.5 PURE READ: never launch/install/restart. Stopped container -> `confirm_start` `NEEDS_CHOICE`
      via the shared resolve. Unknown `--process` -> `select_process` `NEEDS_CHOICE`.
- [x] 3.6 Docstring: state that the bounded tail runs in the core (the `backup` buffered-`exec_run`
      shape) while `logs_plan`'s follow tail stays in the frontend, and why `--follow` is refused here.

## 4. `cwcli axi logs` verb

- [x] 4.1 Add `axi_logs(project, bench=None, lines=100, process=None)` to `commands/axi.py`: `--bench`,
      `--lines/-n` (default 100), `--process/-p`. NO `--follow`, NO `--yes`.
- [x] 4.2 Call `core.read_logs`; on `CwcliError` -> `emit_axi_error` + `exit_for(kind)`; on `NEEDS_CHOICE`
      -> `emit_axi_choice_as_usage_error` + exit 2 (handles `select_bench`, `select_process`,
      `confirm_start` with the existing messages).
- [x] 4.3 On OK, render explicitly (NOT `emit_result`): the metadata head as `toon.kv` lines, then one
      `toon.block(process, lines)` per group so log lines are raw block rows, never re-parsed as
      key:value (design Decision 2). Emit the `not_cwcli_supervised` note and warnings to stderr; exit 0.
- [x] 4.4 Empty read (`logs == []`) still exits 0 with the metadata head and the `logs.none_yet` note.

## 5. Tests

- [x] 5.1 `tests/test_core_logs.py`: cover `read_logs` with an `exec_run`-interpreting container fake -
      combined multi-process, single `--process`, running-but-quiet empty-OK, no-manager raise, stopped
      `confirm_start`, unknown-process `select_process`, and the `tail -v` header parsing.
- [x] 5.2 `tests/test_axi_logs.py`: the verb emits one TOON document (metadata + per-process blocks),
      exits 0; usage errors exit 2 naming the flag / `cwcli start`; no-manager exits 1; stdout stays pure
      TOON (log lines carrying colons/commas do not corrupt the document).
- [x] 5.3 `tests/test_axi_logs.py`: assert `logs` IS registered in `axi_mod.app.registered_commands`
      (design Decision 4 - the presence assertion closing g4's logs half).
- [x] 5.4 Assert the shared resolve did not regress `cwcli logs`: the step-1 five regressions stay green.

## 6. Skill + docs

- [x] 6.1 Regenerate the installable skill (`build_skill.py`); confirm `axi logs` appears in the verb
      table with no hand-edit, and `tests/test_axi_skill.py --check` passes.
- [x] 6.2 Update `README.md`'s agent-surface section and `CLAUDE.md`'s logs entry: `axi logs` now ships
      (a bounded `core.read_logs`, no follow); note the g4 logs-half closure.

## 7. Gates

- [x] 7.1 `uv run pytest` (fast tier), `uv run black --check src/`, `uv run ruff check src/`,
      `uv run mypy src/` all green.
- [ ] 7.2 Real-instance E2E on a throwaway bench: `cwcli axi logs` in a supervised bench (combined and
      `--process`), a quiet bench (empty-OK exit 0), a stopped project (exit 2 naming `cwcli start`),
      a multi-bench project (exit 2 naming `--bench`) - both the supervised and honcho-fallback paths.
