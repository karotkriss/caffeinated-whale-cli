## 1. Shared bench-op validation (extract on the second caller)

- [ ] 1.1 Extract the shell-metacharacter validation for site names and bench paths, plus the bench-directory and site-directory `test -d` probes, out of `core/backup.py:100-136` into ONE shared helper (leaning `core/resolvers.py`; a small `core/benchop.py` is the alternative, settled here).
- [ ] 1.2 Re-point `core/backup.py` at the shared helper and delete its inline copy, including the duplicated `_INVALID_CHARS` list.
- [ ] 1.3 Confirm every command the helper issues is an argv list, never a shell string (the injection guard is the argv discipline; validation is defense in depth on top).
- [ ] 1.4 Backup's existing unit and E2E coverage stays green unchanged (the extraction is behavior-preserving).

## 2. core.unlock (the generality proof: zero new primitives)

- [ ] 2.1 Implement `core.unlock(project, *, site=None, bench=None, bench_path=None) -> Result[UnlockOutcome]` using ONLY the existing primitives (`core.docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench` + `DEFAULT_BENCH_PATH`, `db_utils.get_default_site`, the shared validation helper, the envelope), in the order `core/backup.py:55-136` uses them. No print, no prompt, no `typer.Exit`.
- [ ] 2.2 Define the `UnlockOutcome` DTO (`site`, `bench_path`, `locks_path`, `removed`, `already_unlocked`).
- [ ] 2.3 Run the locks removal as ONE buffered `exec_run` and parse `removed` from its output. Build NO streaming or event-iterator machinery.
- [ ] 2.4 An absent locks directory returns `already_unlocked=True` as a clean OK, never a `NOT_FOUND`.
- [ ] 2.5 Return `confirm_start` / `select_bench` `NEEDS_CHOICE` on the two forks; raise `CwcliError` for hard failures (`NOT_FOUND` for absent project/bench/site, `PRECONDITION` for a failed removal); carry the default-site resolution as an envelope warning rather than a print.
- [ ] 2.6 **REPORT THE VERDICT.** If any existing primitive had to be changed, widened, or special-cased to fit `unlock`, say so explicitly in the PR rather than quietly bending it: that is the foundation-overfit signal this batch exists to detect. Extracting the shared helper (task 1) does not count.
- [ ] 2.7 Unit-test every branch against faked exec I/O: removal with a parsed `removed` list, already-unlocked, default-site resolution, `select_bench`, `confirm_start`, each typed error.
- [ ] 2.8 Re-point `tests/test_unlock.py`'s three argv-list injection guards (`test_unlock.py:55,65,77`) at `core.unlock`; they MUST stay green.

## 3. Reseat cwcli unlock

- [ ] 3.1 Reseat `commands/unlock.py` over `core.unlock`: keep the typer signature, `rich` output, the spinner, and CLI-side `NEEDS_CHOICE` resolution via the existing wrappers. Preserve flags, messages, and exit codes.
- [ ] 3.2 `--verbose` prints each removed path from `UnlockOutcome.removed` at completion (the disclosed nuance: at completion rather than incrementally).
- [ ] 3.3 Confirm `commands/unlock.py` no longer touches `sys.stdout` directly or the docker `api.exec_create`/`exec_start` streaming pair.

## 4. unlock E2E in both modes (the migration's safety floor)

- [ ] 4.1 Add a real-Docker `unlock` E2E on the existing harness, reusing the shared session instance (do NOT spin a second one).
- [ ] 4.2 Non-interactive leg: `--site <site> --yes` with stdin closed, no prompt, exit 0, and the locks directory genuinely gone from the container (a real outcome, not a string match).
- [ ] 4.3 Interactive leg: pty-driven, awaiting the `ESC[?2004h` raw-mode marker before each keystroke, proving the prompt is genuinely shown and genuinely collects input.
- [ ] 4.4 Seed a real locks directory in the container as the fixture, so the removal has something real to remove.

## 5. core.stop and the four callers

- [ ] 5.1 Implement `core.stop(project) -> Result[StopOutcome]`: resolve containers, stop the running ones, return `StopOutcome(project, stopped, already_stopped, containers=[names])`. No print, no prompt, no `typer.Exit`, no live Docker object across the boundary.
- [ ] 5.2 Raise `CwcliError(NOT_FOUND)` for a missing project, retiring `_stop_project`'s `None` sentinel into the taxonomy it was approximating.
- [ ] 5.3 Reseat `commands/stop.py` as a thin frontend: keep the variadic list, stdin piping, the trailing-`-v` recovery (`stop.py:76-82`) verbatim, the spinner, and `rich`. Preserve the honest per-project exit code.
- [ ] 5.4 Re-point `commands/restart.py:15,50` at `core.stop`.
- [ ] 5.5 Re-point `commands/start.py:135,144` at `core.stop`; port-conflict behavior unchanged.
- [ ] 5.6 Re-point `commands/axi.py:332-335` at `core.stop` and DELETE the "(running -> stdout-silent)" comment along with the assumption it encoded.
- [ ] 5.7 Re-point `commands/rm.py:1222,1229` at `core.stop` **last and most carefully**: it is cwcli's most destructive path, and its `None`-means-not-found branch MUST keep failing closed under the typed error.
- [ ] 5.8 Delete `_stop_project` once no caller remains.
- [ ] 5.9 Unit-test `core.stop`: stopped count, already-stopped, `NOT_FOUND`, names-not-objects; plus a test that the `rm` caller still fails closed on a missing project.

## 6. The axi verbs

- [ ] 6.1 Add `cwcli axi unlock <project> [--site] [--bench] [--yes]` on the existing `emit_result` / `emit_axi_error` / `emit_axi_choice_as_usage_error` / `exit_for` helpers; `NEEDS_CHOICE` becomes a usage error naming the flag, exit 2; never prompts.
- [ ] 6.2 Add `cwcli axi stop <project>`; already-stopped is a definitive success state (idempotent for an agent), a missing project is a structured error, never a traceback.
- [ ] 6.3 Unit-test both verbs: one TOON document on stdout, the exit-code mapping, and `removed` emitted as a structured list.
- [ ] 6.4 Verify stdout purity on the `axi start --yes` port-conflict path now that `core.stop` cannot print.

## 7. Validation

- [ ] 7.1 `uv run pytest` (fast unit tier; it already deselects E2E, which is the intended shape), `uv run black --check src/`, `uv run ruff check src/`, `uv run mypy src/` at zero errors.
- [ ] 7.2 Real-instance validation on EXACTLY ONE throwaway v16 instance, reused for all real testing, against the worktree's own editable install. Never the captain's instances. Never a broad Docker prune: scope every destructive operation to resources this run created.
- [ ] 7.3 Re-run the real-instance validation AFTER no-mistakes and after any review fixes, per the captain standard: review fixes frequently alter behavior that only unit tests then re-validate.
- [ ] 7.4 CI owns the E2E suite; do not hand-run it during validation.

## 8. Docs

- [ ] 8.1 `README.md`: the `cwcli axi unlock` and `cwcli axi stop` verbs.
- [ ] 8.2 `CHANGELOG.md`: one entry (user-facing: the two axi verbs; note the `unlock --verbose` timing nuance).
- [ ] 8.3 `cwcli-core-axi` skill: the shared validation helper, and the buffered-instead-of-streamed decision with its reason, so a later agent does not build an event iterator for `unlock`.
- [ ] 8.4 `cwcli-lifecycle` skill: `stop` on the core, and the closed `axi` stdout-purity hole with its root cause.
- [ ] 8.5 `CLAUDE.md`: update the core's migrated-command list.
