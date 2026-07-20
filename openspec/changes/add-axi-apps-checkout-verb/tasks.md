# Tasks: `cwcli axi apps checkout` over the unchanged `core.checkout_app`

Proposed and reviewed as its own phase, so the deferral argument got read before any code existed to bias the review.
Captain approved 2026-07-20 and implemented in phase B; the real-instance E2E (6.2) is the remaining validation.

## 0. Captain approval (blocks everything else)

- [x] 0.1 Captain rules on design Decision 1: **approved as-is, build the verb** (2026-07-20).
- [x] 0.2 Captain rules on design Decision 3: **C1 - defer the resulting-commit read to a read verb on `axi apps list`; keep this change at ZERO core delta and add NO commit field to the shared mutation report.** Binding.
- [x] 0.3 Captain rules on `--reset` (design Decision 5): **R1 - it STAYS on the agent surface as an explicit opt-in, reported as its own row, with no consent axis in the core.** Binding.
- [x] 0.4 Carried correction: `proposal.md` said "eight of the eighteen verbs mutate" while its own table and the live `cwcli axi --help` both show NINE (`backup`, `unlock`, `stop`, `start`, `restart`, `label`, `init`, `setup`, `apps update`). Fixed in `proposal.md` and `design.md`; the corrected count is what the docs and the flipped guard's docstring now carry.

## 1. The verb

- [x] 1.1 Add `axi_apps_checkout(project, app, ref, bench=None, reset=False)` to `commands/axi.py` under the existing `apps_app` Typer group. Flags: `--bench`, `--reset`. NO `--yes`, `--path`, `--json`, `--verbose`.
- [x] 1.2 Call `core_apps.checkout_app(project, app, ref, bench=bench, reset=reset, auto_start=False, on_event=<stderr narrator>)`. Change NO `core/` file.
- [x] 1.3 On `CwcliError` -> `emit_axi_error` + `exit_for(error.kind)`. Covers `NOT_FOUND`/`app.no_checkout`, `PRECONDITION`/`app.no_remote`, and `DOCKER`.
- [x] 1.4 On `NEEDS_CHOICE` -> `emit_axi_choice_as_usage_error` + exit 2. Covers `confirm_start` (stopped project, naming `cwcli start`) and `select_bench` (naming `--bench`). No new choice kinds.
- [x] 1.5 On OK/WARNING -> `emit_result(report, warnings=result.warnings)`, then exit `0 if report.ok else 1` (design Decision 4 - read `report.ok`, NEVER `result.status`).
- [x] 1.6 Wire the `on_event` narrator to stderr only (phase lines and the `$ git ...` echo), so stdout stays one TOON document.
- [x] 1.7 Docstring: state the safety posture (no auto-start, git's own dirty-tree refusal, what `--reset` discards, the inherited credential bridge) and the known gap that the report does not carry the resolved commit.

## 2. Recache epilogue

- [x] 2.1 After a successful call, gate on `any(r.ok for r in report.results)` and call `cache.recache_project(project)`, mirroring `commands/apps.py:472-475`.
- [x] 2.2 A failed recache writes a stderr warning and does NOT change the exit code (design Decision 6), matching `commands/axi.py:1249-1255`.

## 3. Flip the guard

- [x] 3.1 In `tests/test_axi_apps_list.py`, replace `assert "checkout" not in registered` with a presence assertion and rewrite the docstring to record this decision and its evidence.
- [x] 3.2 Leave `install` and `uninstall` asserted absent, and leave `tests/test_axi_skill.py`'s absent-verb parametrization (`axi apps install`, `axi apps uninstall`, `axi restore`) unchanged.

## 4. Tests

- [x] 4.1 `tests/test_axi_apps_checkout.py`: a successful checkout emits one TOON document with `fetch` + `checkout` rows and exits 0; `--reset` adds the `reset` row.
- [x] 4.2 A failed git step exits 1 with `ok: false`, including the WARNING-shaped-envelope case (proves the exit code reads `report.ok`, not `result.status`).
- [x] 4.3 Stopped project exits 2 naming `cwcli start` and starts nothing; multi-bench with no `--bench` exits 2 naming `--bench`.
- [x] 4.4 An app that is not a git checkout emits `app.no_checkout` with a `cwcli apps list` help line and installs nothing.
- [x] 4.5 Stdout purity: the `$ git ...` echo and all narration land on stderr; stdout parses as exactly one TOON document.
- [x] 4.6 The recache runs on success and a failed recache leaves the exit code at 0.
- [x] 4.7 Assert `checkout` IS in `axi_mod.apps_app.registered_commands` (the flipped guard).
- [x] 4.8 Assert the human `cwcli apps checkout` is unregressed: its existing tests stay green, unchanged in substance.

## 5. Skill + docs

- [x] 5.1 Regenerate the installable skill (`scripts/build_skill.py`); confirm `axi apps checkout` appears with no hand-edit and `tests/test_axi_skill.py --check` passes.
- [x] 5.2 Update `CLAUDE.md`'s `apps checkout` entry: it currently reads "NO `axi apps checkout` verb - a HUMAN verb only". Flip it to the shipped verb, and record WHY the install/uninstall deferral did not carry (data destruction vs a non-destructive fetch) so the next reader does not re-derive it.
- [x] 5.3 Update `README.md`'s agent-surface section, the `cwcli-apps-update` and `cwcli-core-axi` skills, and `tests/README.md`'s coverage map.

## 6. Gates

- [x] 6.1 `uv run pytest` (fast tier), `uv run black --check src/`, `uv run ruff check src/`, `uv run mypy src/` all green.
- [ ] 6.2 Real-instance E2E on a throwaway bench: `cwcli axi apps checkout` against a real app for a public repo (success, and an unknown ref failing with exit 1), a dirty tree refused without `--reset` and succeeding with it, a stopped project (exit 2 naming `cwcli start`), and a multi-bench project (exit 2 naming `--bench`). Both `upstream`-remote (bench-installed) and `origin`-remote (hand-cloned) apps, since the remote is auto-detected.
- [ ] 6.3 Re-run 6.2 after any review fixes (the captain standard: review fixes are a trigger to re-validate end to end, not just to re-run units).

## 7. NOT this change

Recorded as tasks, not prose, so a later reader can tell "deliberately not built" from "forgotten".
These are NOT blockers and must NOT be checked off by this change.

- [ ] 7.1 **`cwcli axi apps install`** - the sibling gap, filed separately as `cwcli-axi-apps-install-verb`. A different decision on different evidence: it clones a NEW app into the bench and installs it onto sites. Its absence assertion stays.
- [ ] 7.2 **`cwcli axi apps uninstall`** - unchanged, captain-locked 2026-07-15 on the data-destruction rationale (`migrate-apps-core/tasks.md` §7.1), which this change does not touch.
- [ ] 7.3 **Per-app git state as a read** - the resulting-commit gap from design Decision 3. The right home is `axi apps list` (with the `--fields` question from `migrate-apps-core/tasks.md` §7.3), serving every app rather than only the one just checked out.
