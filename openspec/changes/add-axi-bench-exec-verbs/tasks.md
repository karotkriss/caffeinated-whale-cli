# Tasks: narrow agent verbs for `bench migrate` and `bench run-tests`

Proposed and reviewed as its own phase, so the naming and safety arguments got read before an implementation biased the review - the sequencing `add-axi-apps-checkout-verb` used and the captain approved.

**Captain approved 2026-07-20, every recommendation taken with no amendments**, and implemented in phase B. The ruling: Option A (two TOP-LEVEL verbs, 16 -> 18), both gates approved independently, S1 (`--site` AND `--app` required on `run-tests`, no default-site fallback), M1 (no `--skip-maintenance`), H1 (human verbs deferred - these are the first axi-only verbs, accepted knowingly). The real-instance E2E (6.2/6.3) is the remaining validation.

**One carried implementation note.** §0.7's promote-don't-copy ruling applied to a SECOND helper the proposal had not named: `core.apps._resolve`'s container+bench prologue, which `core.bench_ops` also needed. It was promoted to `resolvers.resolve_container_and_bench` on the same reasoning (a drifted resolve prologue resolves a DIFFERENT bench than the verb reports), with `core.apps._resolve` left as a one-line delegation. The cost, worth recording: `tests/test_apps.py` and `tests/test_apps_characterization.py` patched `core_apps.core_docker`, so once the import became genuinely unused and ruff removed it, 35 tests failed on `AttributeError`. Fixed by patching `core_docker` directly - the same module object. Moving a resolve seam moves every monkeypatch aimed through it.

## 0. Captain approval (blocks everything else)

The two verbs are deliberately given SEPARATE approval gates. `migrate` is a bounded state mutation; `run-tests` is unbounded execution of the repository's own code (`design.md` Decisions 3 and 4). Approving one on the other's evidence is the inherited-rationale error `add-axi-apps-checkout-verb` had to unwind, so it is made structurally impossible here.

- [x] 0.1 **Naming and shape** (`design.md` Decision 1). Rule between:
      **A** - two top-level verbs `cwcli axi migrate` / `cwcli axi run-tests` (recommended);
      **B** - one `cwcli axi bench <migrate|run-tests>` group, which additionally requires a WRITTEN boundary rule in the group docstring;
      **C** - `migrate` only, `run-tests` deferred.
- [x] 0.2 **`axi migrate` - approve or decline**, on its own evidence (proposal §"Safety posture: `axi migrate`").
- [x] 0.3 **`axi run-tests` - approve or decline**, on its own evidence (proposal §"Safety posture: `axi run-tests`"). A decline drops §3 and the `run-tests` requirements and leaves the rest shippable.
- [x] 0.4 **`run-tests` target resolution** (`design.md` Decision 4): required `--site` (recommended, a deliberate divergence from the surface convention), or convention-consistent default-site fallback with a mandatory `--app`.
- [x] 0.5 **`--skip-maintenance` on `axi migrate`** (`design.md` Decision 3): recommended NO. Confirm or overrule.
- [x] 0.6 **Human `cwcli migrate` / `cwcli run-tests`** (`design.md` Decision 6): recommended DEFERRED to their own change, which makes these the first axi-first verbs. Confirm or fold them in.
- [x] 0.7 Acknowledge the reported primitive-bending signal: `_set_maintenance` gains a second caller and is PROMOTED, not copied (`design.md` Decision 2).

## 1. The core

- [x] 1.1 Add `src/caffeinated_whale_cli/core/bench_ops.py` with `BenchOpResult` / `BenchOpReport` DTOs. Do NOT extend `AppsReport` (`add-axi-apps-checkout-verb` Decision 3 is binding: no new fields on the shared mutation report).
- [x] 1.2 Promote `core/update.py:273`'s `_set_maintenance` to a shared core helper, behaviour byte-identical, and re-point `core.update` at it. Assert `core.update`'s maintenance lifecycle and load-bearing migrate gate are unchanged.
- [x] 1.3 `migrate_site(project, *, site=None, bench=None, auto_start=False, on_event=None) -> Result[BenchOpReport]`: resolve ONE site, enable maintenance (refuse the migrate if it fails), run `bench --site <site> migrate` via `exec_stream`, disable in a `finally`, report `maintenance_left_on` on a failed disable.
- [x] 1.4 `run_tests(project, *, site, app, bench=None, auto_start=False, on_event=None) -> Result[BenchOpReport]`: `site` and `app` are non-optional parameters, not defaulted.
- [x] 1.5 Reuse the `_resolve` shape (`core/apps.py:155`), `exec_stream`, `Result`/`CwcliError`, and the EXISTING `confirm_start` / `select_bench` choice kinds. Introduce no new `Choice.kind` token and no new `ErrorKind`. If implementation finds a new one is needed, REPORT it rather than absorbing it.
- [x] 1.6 Confirm the core purity gate (`tests/test_core_envelope.py`) stays green for the new module.

## 2. The `migrate` verb

- [x] 2.1 Register per the §0.1 ruling. Flags: `--site`/`-s`, `--bench`. NO `--yes`, NO `--json`, NO `--verbose`, NO command-string parameter.
- [x] 2.2 `CwcliError` -> `emit_axi_error` + `exit_for(error.kind)`; `NEEDS_CHOICE` -> `emit_axi_choice_as_usage_error` + exit 2; OK/WARNING -> `emit_result` then exit `0 if report.ok else 1` (NEVER `result.status`).
- [x] 2.3 Narrator to stderr, forwarding bench's OWN bytes unparsed (`_checkout_narrate`'s reasoning, `commands/axi.py:998-1016`), plus the `$ bench ...` echo. stdout stays one TOON document.
- [x] 2.4 Docstring states the blast radius in plain words (irreversible schema change to a live site, recoverable only from a backup) and each guard against its named threat.
- [x] 2.5 Contextual disclosure (AXI §9): on failure, help lines pointing at `cwcli axi logs` and `cwcli axi backup`. On success, NO help line - the output answers the query.
- [x] 2.6 Confirm no recache epilogue is warranted (`design.md` Decision 7); if implementation finds a migrate changes something `inspect` reports, add one and say so.

## 3. The `run-tests` verb (only if §0.3 approves)

- [x] 3.1 Register per the §0.1 ruling. `--site` and `--app` per the §0.4 ruling; `--bench` optional. NO `--yes`, NO command-string parameter.
- [x] 3.2 Same error / choice / exit-code wiring as §2.2.
- [x] 3.3 Forward the runner's output to stderr in FULL and UNPARSED. Do not summarize it into pass/fail counts - cwcli does not own that format.
- [x] 3.4 Docstring states the blast radius plainly (arbitrary Python from the repository under test, executed against a live site), why that is nevertheless not the `axi run` passthrough (the agent selects, it does not author), and that a dedicated test site is the practice cwcli cannot enforce.

## 4. Tests

- [x] 4.1 `tests/test_core_bench_ops.py`: one-site resolution, the maintenance enable/migrate/disable order, a failed enable refusing the migrate, a failed disable reported and failing, and the `finally` running when the migrate raises.
- [x] 4.2 A migrate NEVER fans out: a bench holding several sites yields exactly one `bench migrate`.
- [x] 4.3 `tests/test_axi_bench_ops.py`: success emits one TOON document carrying the RESOLVED site and exits 0; a failure exits 1 including the WARNING-shaped-envelope case (proves the exit code reads `report.ok`).
- [x] 4.4 Stopped project exits 2 naming `cwcli start` and starts nothing; multi-bench with no `--bench` exits 2 naming `--bench`; an unknown site is a typed error.
- [x] 4.5 `run-tests` with a missing `--site` or `--app` exits 2 naming the flag and runs nothing.
- [x] 4.6 Stdout purity on EVERY path, including every error path: stdout parses as exactly one TOON document and the command's own bytes are on stderr.
- [x] 4.7 `tests/test_axi.py::TestNoAxiRunVerb` stays green and unmodified in substance; add an assertion that neither new verb has a variadic or free-form command parameter, and record in its docstring that `run-tests` does not weaken the exact-name assertion.
- [x] 4.8 `core.update` is unregressed by the `_set_maintenance` promotion: its existing tests stay green, unchanged in substance.

## 5. Skill + docs

- [x] 5.1 Regenerate the installable skill (`scripts/build_skill.py`); confirm the new verbs appear with no hand-edit and `tests/test_axi_skill.py --check` passes. Leave the absent-verb parametrization unchanged.
- [x] 5.2 Update `CLAUDE.md`: add the `bench_ops` entry, record WHY these two cleared the bar that `axi run`/`axi exec` do not (typed parameters vs an agent-authored command string), and refresh the mutating-verb count in the `apps checkout` entry, which currently reads "NINE of the eighteen live `axi` verbs mutate" - it is TEN of NINETEEN on this branch and will change again here.
- [x] 5.3 Update `README.md`'s agent-surface section, the `cwcli-core-axi` and `cwcli-apps-update` skills, and `tests/README.md`'s coverage map.

## 6. Gates

- [x] 6.1 `uv run pytest` (fast tier), `uv run black --check src/`, `uv run ruff check src/`, `uv run mypy src/` all green.
- [ ] 6.2 Real-instance E2E on a throwaway bench (`CWE2E_PORT_BASE=12000`): a successful migrate on a single-site bench; a multi-site bench proving exactly one site migrates; a stopped project (exit 2 naming `cwcli start`); a multi-bench project (exit 2 naming `--bench`); a failing migrate exiting 1 with the patch's output on stderr; and the full `apps checkout` -> `migrate` composition that this change exists to unblock.
- [ ] 6.3 If §0.3 approved `run-tests`: a passing suite (exit 0) and a failing suite (exit 1) with the runner's output on stderr, plus missing `--site` / `--app` each exiting 2.
- [ ] 6.4 Re-run 6.2/6.3 after any review fixes (the captain standard: review fixes are a trigger to re-validate end to end, not just to re-run units).

## 7. NOT this change

Recorded as tasks, not prose, so a later reader can tell "deliberately not built" from "forgotten". These are NOT blockers and must NOT be checked off by this change.

- [ ] 7.1 **`cwcli axi run` / `cwcli axi exec`** - unchanged. The deferral stands and this change strengthens it by closing the two concrete needs that drove people to the escape hatch.
- [ ] 7.2 **Any third bench command** (`bench build`, `bench backup`, `bench console`, ...). Each is its own decision on its own evidence. Under Option B this is the boundary rule §0.1 requires be written down.
- [ ] 7.3 **Human `cwcli migrate` / `cwcli run-tests`** - deferred per `design.md` Decision 6, subject to the §0.6 ruling. Nearly free once `core/bench_ops.py` exists; they carry their own UX design.
- [ ] 7.4 **`axi apps install` / `uninstall`, `axi restore`, `axi rm`** - absence assertions untouched.
- [ ] 7.5 **Per-app git state on `axi apps list`** - `add-axi-apps-checkout-verb` §7.3's deferred read. Still the right home for the resulting-commit question; not reopened here.
