## 1. Characterization tests FIRST (green before anything moves)

- [x] 1.1 Cover the five untested aggregation branches in `tests/test_apps.py` against current `develop`: `failed_apps` (`update.py:526-528`), `failed_migrations` (`:539-543`), `failed_builds` (`:546-550`), `failed_cache_clears` (`:553-557`), `failed_website_cache_clears` (`:560-564`). Only `failed_maintenance_enable` and `failed_maintenance_disable` are covered today - **2 of 7**, re-measured at `46c9c83`, not inherited from the recon.
- [x] 1.2 Cover the flags with zero coverage: `--build` (`_build_apps` at `:416-441` is entirely uncovered, plus its call at `:783`), `--skip-maintenance` (`:769`), `--clear-website-cache` (`:799`), `--no-recache` (`:298-304`, `:234`).
- [x] 1.3 Cover **multi-app fan-out**: every existing `_update_project(...)` test call passes exactly ONE app, so the multi-app loop has no coverage at all.
- [x] 1.4 These tests MUST pass **before** the migration and **after** it, **unchanged**. That is what makes refactor-under-green true rather than aspirational.
- [x] 1.5 **State the boundary of "unchanged" explicitly** (design Decision 3): the tests that must not change are the ones pinning behaviour this batch PRESERVES. The stream-loss tests change **by design**, because Decision 2 changes what a stream loss does. Do not silently rewrite a test to match new behaviour and call it characterization.
- [x] 1.6 Re-measure coverage and report it. Baseline re-measured first-hand: `update.py` **387 stmts, 112 miss, 71.06%** (the recon's 376/113/69.95% predates PR #81); `apps.py` 195/17/91.28%; suite **870 passed**. Coverage of the moved state machine must be measurably up at the end.
- [x] 1.7 Note for whoever runs the suite: `/tmp/pytest-of-cmckay` is owned by `root` on this host, so every `tmp_path` test errors in setup (169 errors) unless `TMPDIR` is redirected. Environmental, outside the worktree, NOT a real failure and not caused by this change.

## 2. The frappe fork FIRST (it is what makes the verb possible)

- [x] 2.1 Move `_run_frappe_update_reset` (`update.py:209-240`) onto the core and **drop the hardcoded `verbose=True` at `:229`**. Verified first-hand: with `verbose=False` it still writes bench stream output to **stdout**. Consumption becomes the caller's choice via `on_event`, exactly as batch 3 established that `verbose` decides whether to RENDER, not how to OBTAIN.
- [x] 2.2 **This is the direct blocker for `axi apps update`**, not a tidy-up: an axi verb whose stdout must hold exactly one TOON document cannot call a function that unconditionally writes bench output to stdout. Smallest, most isolated (32 lines, outside the state machine, three tests) and highest-leverage piece in the batch.
- [x] 2.3 The three existing frappe tests (`tests/test_apps.py:557,567,576`) all pass `verbose=True` and therefore **cannot see this bug**. Add one that pins the property: the frappe path writes nothing to stdout when no renderer is attached.
- [x] 2.4 **PRESERVE the recache-before-exit-code-check** (`:232-239`): a failed `bench update --reset` still recaches and only then reports failure. Defensible and untested either way. Note it; do NOT "fix" it in a migration (design Decision 4).
- [x] 2.5 Preserve the ignored-options announcement (`:687-706`), carried as warnings from the core rather than printed by it.
- [x] 2.6 **Do NOT repeat the claim that this path runs inside the maintenance-mode `try/finally`.** Batch 3's shipped `design.md` Risks and the batch-3 recon §4c both say so; both are false. Re-verified a third time at `46c9c83`: call `:707`, **return `:708`**, `try` `:723`, `finally` `:818`.

## 3. `core/update.py`

- [x] 3.1 Add `core/update.py` with `core.update(project_name, apps, *, bench=None, bench_path=None, sites=None, clear_cache=False, clear_website_cache=False, build=False, skip_maintenance=False, no_recache=False, auto_start=False, on_event=None) -> Result[UpdateReport]`. **ONE call**, matching `core/backup.py`'s shipped shape - see design Decision 6 and confirm the captain's ruling before building it.
- [x] 3.2 **It is NOT a generator** (design Decision 1). Plain function, optional typed-event callback, maintenance-mode `finally` unconditional. Re-probed: a `try/finally` inside a generator does NOT run when a consumer breaks early holding a reference (CASE 3) or lands in a reference cycle (CASE 4) - a GUI pumping events is exactly that, and the GUI is what this rework exists to enable. **Do not "elegantly" refactor this to `-> Iterator[UpdateEvent]` later without arguing with that probe.**
- [x] 3.3 Define `UpdateReport` (frozen, slots, kw_only; every field a builtin). The seven-way aggregation IS the DTO - it already exists as seven lists and has simply never been returned. `failed_maintenance_disable` is the field that justifies the verb: the only outcome that leaves the user's site DOWN, reachable today only by scraping `rich` markup off stdout.
- [x] 3.4 Define the event vocabulary (`UpdateStepStart`, `UpdateOutput`, `UpdateStepEnd`, `UpdateAborted`). Typed events only: no `rich`, no `typer`, no `questionary`. The exact field list is tuned in implementation against what the two renderers actually need (design Open Questions).
- [x] 3.5 **A lost stream continues the fan-out, recorded as UNKNOWN** (design Decision 2): catch the exec-stream `CwcliError` per exec, record in `unknown_apps`/`unknown_migrations`/`unknown_builds`, continue. **Do NOT fold unknown into `failed_*`** - an agent retries a failure, and retrying a live migration is harmful. Three unknown lists, not seven: only pull, migrate and build stream inside the state machine.
- [x] 3.6 Treat ALL exec-stream `CwcliError`s as unknown, including `exec.start_failed`. Rejected alternative recorded in Decision 2: it is a confident "did not run" derived from an API call whose own outcome is uncertain.
- [x] 3.7 Append a `Message` to the envelope's `warnings` per lost stream, so the report says WHAT is unknown and the warnings say WHY.
- [x] 3.8 Preserve the load-bearing gate: `sites_to_migrate = sorted(maintenance_sites)` (`:771`). A site that could not enter maintenance is never migrated, never cache-cleared, never lock-cleared. Preserve per-site maintenance recording as each site succeeds (`:343-346`), and the `finally` disabling exactly the sites enabled.
- [x] 3.9 **Re-verify the `finally` by TEST, not by inspection.** This is the single most safety-critical thing in the batch, and inspection is how #81's bug shipped past a review in the first place.
- [x] 3.10 Emit `UpdateAborted(report=...)` from the `finally` when unwinding (design DTOs). A returned report cannot survive an unwinding `KeyboardInterrupt`, and today a Ctrl-C still prints the summary with its stuck-site remediation. **Do not lose #81's property in the migration.**
- [x] 3.11 Keep update's **own two-directory bench probe** (`apps/` AND `sites/`) inside `core/update.py`. Do NOT reuse `resolvers.require_bench_dir` (probes `sites/` only - silently drops a check) and do NOT widen it (changes `backup`/`unlock`, untouched by this batch). **This is the near-miss; report it, do not resolve it by bending the primitive** (design Decision 7).
- [x] 3.12 Adopt the shell-free `["test", "-d", path]` argv form for the probe, rather than `_dir_exists`'s `["sh", "-c", ...]`. A simplification, not a behaviour change.
- [x] 3.13 **Resolve no MORE than `update` does today**: no `validate_site_name`, no `resolve_default_site`. Both are one import away and `update` uses neither; adding them would ADD failures `cwcli apps update` does not have (batch 3's Decision 8 trap).
- [x] 3.14 `_fail_if_site_filter_matched_nothing` (`:190-206`) raises `CwcliError(USAGE)` from the core. It cannot live in a resolve phase: it needs the discovered affected sites.
- [x] 3.15 **Report the zero-new-primitives verdict** either way (design Decision 7). Batch 1 came back zero bent, batch 2 parameterized one string, batch 3's own new primitive needed correcting and its test suite caught it. The signal is the point.
- [x] 3.16 Core unit tests: every aggregation branch, the maintenance gate, the unconditional `finally`, unknown-vs-failed, the frappe fork, `NEEDS_CHOICE`, one-pull-one-discovery, and that no field of `UpdateReport` holds a live Docker object.

## 4. Reseat the frontends over ONE implementation

- [x] 4.1 Reseat `commands/update.py` as a renderer over the report. The 46 stdout `console.print` calls and 6 stdout `console.status` spinners become rendering driven by events.
- [x] 4.2 **Keep BOTH interfaces byte-for-byte.** `cwcli apps update` takes apps **positionally** (`apps.py:482`); the deprecated `cwcli update` takes them as **`--app`/`-a`** (`update.py:907`). They are **NOT** interface-identical. Anyone assuming a pure passthrough will unify the signatures and break every existing `cwcli update ... --app x` invocation.
- [x] 4.3 Keep the deprecated alias's stderr-only deprecation warning and its full option signature. It stays a FRONTEND, never a second core path. `test_deprecated_update_warns_and_delegates` (`tests/test_apps.py:730`) must stay green in substance.
- [x] 4.4 The `--site`-matched-nothing refusal keeps **exit 1** on the human CLI. On `axi` it maps through `exit_for(USAGE)` to 2, which is correct for a new agent surface - a difference between surfaces, not a regression on either (design Decision 5).
- [x] 4.5 The two b8 tests (`test_update_pulls_and_discovers_once`, `test_update_empty_affected_set_performs_neither_second_pass`, both parametrized over `verbose`) must survive **in substance**, re-pointed at the core, still asserting one pull and one discovery pass. **The b8 bug is NOT live** - `f252a69` fixed it and the comment (now `:729-733`, not the recon's `:625-629`) is a regression guard. The migration removes the conditions that made it possible: the presentation fork stops being bound to a data condition. Say so, or "the migration deletes the code the b8 tests pin" reads as coverage loss at review.
- [x] 4.6 Re-point `test_update_stream_loss_mid_fanout_still_reports_stuck_site_remediation` (parametrized over `verbose`) to Decision 2's behaviour: the stream loss no longer raises, the fan-out continues, the remaining site is still migrated, the site lands in `unknown_migrations`, and the summary + BOTH remediation lines still survive. Its substance is strengthened, not dropped.
- [x] 4.7 Keep the frontend's interactive prologue (resolve container/bench BEFORE any spinner), matching `commands/backup.py`'s shipped pre-resolve-then-spinner pattern and the known spinner-over-questionary deadlock.

## 5. Structured output on both surfaces

- [x] 5.1 Add `--json` to `cwcli apps update`, matching its three siblings (`apps.py:244,316,413`). Stdout carries ONLY the JSON document; progress to stderr; bench output never on stdout - **including on the `--app frappe` path**, which is exactly what section 2 unblocks.
- [x] 5.2 Add `cwcli axi apps update`: one TOON `UpdateReport` on stdout, no prompts, no business logic of its own.
- [x] 5.3 **Exit on `report.ok`, NOT on `result.status`** (design Decision 5). The shipped `axi_backup` pattern (`axi.py:253`) maps `WARNING` -> 0, and a partial update failure is a `WARNING`-shaped envelope with `ok=False`. Copying it verbatim would ship a fail-open on the surface this batch exists to make honest.
- [x] 5.4 `NEEDS_CHOICE` -> TOON usage error -> exit 2, via the existing `emit_axi_choice_as_usage_error`.
- [x] 5.5 **No `axi update` verb** on the deprecated spelling. One verb, canonical spelling; batch 2's `self-update` discipline applies.
- [x] 5.6 Tests: `--json` stdout purity (including the frappe path), the TOON document, exit 0/1/2, and unknown-distinguishable-from-failed in the emitted report.

## 6. E2E on a real instance (there is none today)

- [x] 6.1 Add `apps update`'s first E2E. `tests/e2e/` holds backup, harness_safety, init, label, per_process_supervisor, run, start_status ×2, status_unsupervised, unlock - and **nothing for `apps` or `update`**.
- [x] 6.2 Both modes per the captain standard: pty-driven interactive (await prompt_toolkit's `ESC[?2004h` raw-mode marker before each keystroke), `--yes` non-interactive, and a non-TTY without `--yes` refusing non-zero.
- [x] 6.3 Include `apps update --app frappe`: it runs `bench update --reset` gated by **nothing** - no `--yes`, no confirmation - and is the path that most needs real coverage.
- [x] 6.4 Load the `cwcli-e2e-testing` skill first (isolation recipe, harness contract, teardown).
- [x] 6.5 **Re-run the E2E AFTER no-mistakes and after any review fixes** (captain standard). Unit tests plus a green pipeline are not sufficient proof for a behaviour change, and this batch has one (Decision 2).

## 7. Docs and skills

- [x] 7.1 **Delete `README.md:839`'s "`apps update` delegates to the streaming update flow and has no `--json`"** - the line this batch exists to remove - and document `apps update --json` and `axi apps update`.
- [x] 7.2 `CHANGELOG.md` entry (user-facing: `apps update` gains `--json`, `cwcli axi apps update` ships, a lost stream no longer abandons the fan-out).
- [x] 7.3 Update the `cwcli-apps-update` skill: the state machine now lives in `core/update.py`; record Decision 1 (not a generator, with the probe) and Decision 2 (unknown is not failed) so neither is silently re-broken.
- [x] 7.4 Update the `cwcli-core-axi` skill and `CLAUDE.md`'s core-migrated list: `update` leaves the un-migrated list; `apps` (list/install/uninstall) stays on it for batch 5.
- [x] 7.5 Record in the skill that `core.update` is a plain function **deliberately**, against locked decision 4's letter, with the probe - a later agent reading decision 4 alone will otherwise reach for the generator and reopen a data-safety hole.
