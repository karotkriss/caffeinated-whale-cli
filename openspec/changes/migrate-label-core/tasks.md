## 1. Shared groundwork (do these first; each is behavior-preserving on its own)

- [ ] 1.1 Move the cached-benches read down a layer: add `resolvers.cached_benches(project_name) -> list[dict]` to `core/resolvers.py`, re-point its own inline copy at `resolvers.py:128`, re-point `commands/utils.py:183,211`, and DELETE `commands/utils.py:269`'s `_cached_benches`. The helper moves because `core/` cannot import `commands/`, not because four is a magic number.
- [ ] 1.2 Add `not_running_hint: str | None = None` to `resolvers.resolve_container_state`, used on the `NOT_RUNNING` raise at `resolvers.py:84-89` and defaulting to today's hardcoded string so every existing caller (`core/backup.py:54`, `core/unlock.py:75`, `commands/utils.py:66`) is byte-for-byte unchanged.
- [ ] 1.3 Extend `tests/test_core_resolvers.py` with a case pinning the default hint and a case pinning a caller-supplied one; `tests/test_core_resolvers.py:59` (the `offer_choice=False` raise) stays green unchanged.
- [ ] 1.4 Drop the dead `verbose` param from `bench_labels.read_label_marker` (`:133`), `write_label_marker` (`:168`), and `clear_label_marker` (`:190`) - accepted, never read in any body. Update the call sites at `commands/inspect.py:241` and `commands/inspect.py:610` for the signature ONLY; `inspect`'s own `--verbose` is real and stays untouched.
- [ ] 1.5 `tests/test_inspect_label_recovery.py` and `tests/test_bench_labels.py` stay green across 1.4. `inspect`'s marker recovery is what makes labels survive a cache wipe; this batch passes through it and must not disturb it.

## 2. core.label (the read and the two mutations)

- [ ] 2.1 Implement `core.list_benches(project) -> Result[BenchList]` in a new `core/label.py`: read via `resolvers.cached_benches`, return one `BenchInfo(index, path, label)` per bench in stable order. No container is touched. No cached benches raises `CwcliError(NOT_FOUND)` with a hint naming `cwcli inspect <project>` - NOT an empty list ("not inspected yet" and "zero benches" are different facts).
- [ ] 2.2 Define the `BenchInfo`, `BenchList`, and `LabelOutcome` DTOs (frozen, slots, kw_only; every field a builtin or None). `BenchList` wraps its list in a dataclass following `core/where.py:35`'s `WhereResult`, because `emit_result` serializes via `asdict` and needs a dataclass at the top. Document on `BenchInfo.index` that it is positional and NOT durable.
- [ ] 2.3 Implement `core.set_label(project, *, bench=None, label) -> Result[LabelOutcome]` and `core.clear_label(project, *, bench=None) -> Result[LabelOutcome]` as SEPARATE functions. Do NOT collapse them into one `label=None`-means-clear function: that is the sentinel batch 1 retired from `_stop_project`. `db_utils.set_bench_label` keeps its `None`-clears convention at the storage layer only.
- [ ] 2.4 Resolve the bench via `resolvers.resolve_bench(project, bench, None)` - the same primitive every other bench-scoped verb uses - and re-key the duplicate check from dict identity (`label.py:92`'s `other is not chosen`) to path (`other["path"] != chosen_path`). Path is the natural key: `db_utils.set_bench_label` keys on it and the marker lives at it.
- [ ] 2.5 Resolve run-state via `resolvers.resolve_container_state(..., auto_start=False, offer_choice=False, not_running_hint="Start the project first - the label marker is stored inside the bench.")`. This is the FIRST production caller of the `offer_choice=False` branch; a label change must never start a stopped project.
- [ ] 2.6 **PRESERVE the clear-path ordering verbatim** (`label.py:114-130`): remove the marker FIRST, touch the DB only on success. A failed marker removal raises and leaves the DB label intact; a failed DB update after a successful marker removal raises rather than reporting success. This is the invariant CLAUDE.md's captain standard names by name - do not "simplify" it.
- [ ] 2.7 **PRESERVE the set-path asymmetry verbatim** (`label.py:142`): the DB write's return value stays unchecked, unlike the clear path. It is asymmetric but defensible (on set, marker-wins is self-healing via `inspect`; on clear, marker-wins is a resurrection), the rationale is INFERRED not recorded, and changing it is a behavior change that does not belong in a not-breaking migration. See design Decision 4.
- [ ] 2.8 Return `select_bench` `NEEDS_CHOICE` on the multi-bench fork; carry the `bench.sole` warning on the single-bench fallback; raise `CwcliError(USAGE)` for an invalid or duplicate label BEFORE writing anything; raise `CwcliError(NOT_FOUND)` for an unknown selector WITHOUT stuffing a rendered bench list into the error's `detail` (that would be the primitive-bending this batch exists to detect - the frontend calls `core.list_benches` instead).
- [ ] 2.9 **REPORT THE VERDICT.** Zero new primitives is the claim. State explicitly in the PR whether any existing primitive had to be changed, widened, or special-cased beyond the two known items (the `not_running_hint` widening in 1.2, disclosed up front; the `cached_benches` move in 1.1, which is extraction and does not count per batch 1's Decision 1). A SECOND unplanned widening is a real foundation signal and belongs in the PR, not absorbed.
- [ ] 2.10 Unit-test every branch against faked container I/O and a temp DB: list mode, set, clear, both clear-failure modes, duplicate rejection, numeric-label rejection, unknown selector, `NOT_RUNNING`, no cached benches, multi-bench `select_bench`, single-bench `bench.sole`.

## 3. Reseat cwcli label

- [ ] 3.1 Reseat `commands/label.py` over the three core functions. Keep `_print_bench_list`, the `rich` markup, the spinner-free flow, and the exit codes in the frontend. Preserve the typer signature, arguments, flags, messages, and exit codes exactly.
- [ ] 3.2 On an unknown selector, the frontend renders the error AND the available-benches list as it does today (`label.py:78-79`), obtaining the list from `core.list_benches` rather than from data carried on the error.
- [ ] 3.3 Wire `cwcli label --verbose` to emit the envelope's warnings and the resolved marker path. It feeds ONLY the dead params today (`label.py:118,134`), so it is provably a no-op flag; keep it (removal would break a CLI surface for no gain) and make it honest. Disclosed nuance: `--verbose` starts emitting where it emitted none - additive and opt-in.
- [ ] 3.4 Re-point the 15 existing tests in `tests/test_bench_label_db_and_command.py`. `test_clear_marker_failure_leaves_db_label_intact:171` and `test_clear_db_failure_prints_error:190` MUST stay green - they pin the consistency invariant. `test_reject_duplicate_label:217` must stay green across the 2.4 re-key.
- [ ] 3.5 Confirm `commands/label.py` no longer reads the cache directly or resolves containers itself.

## 4. The axi verbs

- [ ] 4.1 Add `cwcli axi benches <project>` emitting `BenchList` as one TOON document. **This verb is the batch's justification**: it is the only structured answer to "multiple benches; pass --bench <index|label>" (`axi.py:79`), which every bench-scoped verb asks and neither `InstanceDTO` (`core/list.py:22`) nor `WhereMatch` (`core/where.py:22`) can answer. An uninspected project is a structured error naming `cwcli inspect`, not an empty list.
- [ ] 4.2 Add `cwcli axi label <project> [--bench <sel>] --set <label> | --clear`. `--set`/`--clear` mutually exclusive, exactly one required; neither is a `USAGE` error, NOT an implicit list. `--bench` stays OPTIONAL, falling through to `resolve_bench`'s family contract (sole bench resolves; multi-bench is a `select_bench` usage error naming the flag), matching `axi backup`/`axi unlock` rather than `axi restart`'s required `--process`.
- [ ] 4.3 Do NOT mode-switch a single verb on whether `argv[2]` exists. Listing and mutating are two verbs: mode-switching makes the output schema depend on argv and hides discovery inside a mutation verb's name.
- [ ] 4.4 Unit-test both verbs: exactly one TOON document on stdout, the exit-code mapping (0/1/2), the `select_bench` usage error listing options, and the `NOT_RUNNING` error carrying `label`'s own hint rather than the `--yes` default.

## 5. axi self-update --check (read-only; ~20 lines, no core work)

- [ ] 5.1 Add `cwcli axi self-update --check [--no-cache]` on the existing `emit_result`/`exit_for` helpers over `core.version.check`. No core changes: `core/version.py:56` already returns `Result[VersionInfo]` and `VersionInfo` (`:36-53`) is already serializable.
- [ ] 5.2 **Exit 0 when an update IS available**, carrying the fact in `is_outdated`. This deliberately diverges from `cwcli self-update --check`'s exit 1 (`self_update.py:81`), whose exit code is UNCHANGED. On the agent surface a non-zero exit means an error, and a successful read is not an error; the precedent is `axi status` (`axi.py:441`), which exits 0 reporting a fully offline project. See design Decision 5.
- [ ] 5.3 A fail-open PyPI lookup (`latest: null` + the `pypi.unreachable` warning) exits 0. A dev/editable or uvx install emits the DTO carrying that method and exits 0 - no special-case early return; the human CLI's prose returns (`self_update.py:59-73`) are rendering, which is the frontend's job.
- [ ] 5.4 Do NOT ship the mutating `cwcli axi self-update`. It stays deferred on the captain's ruling: the original rationale was never recorded, and an agent upgrading the tool it is executing from mid-session is the half that plausibly had a real reason.
- [ ] 5.5 Unit-test the verb: one TOON document, exit 0 on outdated, exit 0 on network failure, the dev/uvx case.

## 6. label E2E (non-interactive, ONE mode)

- [ ] 6.1 Add a real-Docker non-interactive `label` E2E on the existing harness, reusing the shared session instance (do NOT spin a second one).
- [ ] 6.2 Cover the consistency invariant end to end with REAL outcomes, not string matches: set a label -> assert the cache AND the in-container marker both carry it -> clear it -> assert both are genuinely gone -> run a full `inspect` -> assert the cleared label is NOT resurrected.
- [ ] 6.3 ONE mode, non-interactive, and say why in the test's docstring: `label` has no prompt (no `questionary`, no `isatty`, no confirm in `commands/label.py`, and it deliberately does not auto-start), so the captain's both-modes standard - whose trigger is a prompt - does not bite. Do NOT add an interactive leg; there is no prompt to drive and the ritual would prove nothing.

## 7. Validation

- [ ] 7.1 `uv run pytest` (fast unit tier; it already deselects E2E), `uv run black --check src/`, `uv run ruff check src/`, `uv run mypy src/` at zero errors.
- [ ] 7.2 Real-instance validation on EXACTLY ONE throwaway instance, reused for all real testing, against the worktree's own editable install. Never the captain's instances. NEVER a broad Docker prune: scope every destructive operation to resources this run created.
- [ ] 7.3 Re-run the real-instance validation AFTER no-mistakes and after any review fixes, per the captain standard: review fixes frequently alter behavior that only unit tests then re-validate.
- [ ] 7.4 CI owns the E2E suite; do not hand-run it during validation. The gate runs only tests relevant to the change.
- [ ] 7.5 Known box gotcha, NOT a cwcli bug and NOT to be fixed: `/tmp/pytest-of-cmckay` is root-owned on this machine and errors ~150 tests on a bare `pytest`. Work around it with `TMPDIR`.

## 8. Docs

- [ ] 8.1 `README.md`: the `cwcli axi benches`, `cwcli axi label`, and `cwcli axi self-update --check` verbs.
- [ ] 8.2 `CHANGELOG.md`: one entry (user-facing: the three axi verbs; `cwcli label --verbose` now reports something).
- [ ] 8.3 `cwcli-core-axi` skill: the `axi benches` discovery verb and the dead end it closes; the `not_running_hint` widening and why it was disclosed rather than absorbed; the `axi self-update --check` exit-0-on-outdated divergence and its reason.
- [ ] 8.4 `cwcli-inspect-benches` skill: `label` on the core, the preserved marker-before-DB clear ordering with its root cause, and the deliberate set-path asymmetry with its inferred rationale so nobody tidies it into symmetry.
- [ ] 8.5 `CLAUDE.md`: update the core's migrated-command list, and CORRECT the stale `self-update` line ("Human CLI only - no `cwcli axi` verb (deferred)") to say the read-only `--check` verb now exists and the mutating verb remains deliberately deferred.
