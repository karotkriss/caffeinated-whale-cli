## Why

**`cwcli apps update` is the last `apps` subcommand with no structured output, and the reason on file is now false.**
`README.md:839` states it verbatim: "The `list`, `install`, and `uninstall` subcommands also support `--json` machine-readable output; `apps update` delegates to the streaming update flow and has no `--json`."
Streaming was the blocker, and batch 3 removed it.
That line is what this batch deletes.

But "the blocker is gone" is not the same as "the verb is possible", and the audit found the concrete thing still standing in the way.

**1. `apps update --app frappe` writes bench output to stdout whatever the user asked for.**
`_run_frappe_update_reset` (`update.py:209-240`) calls `_stream_command(..., verbose=True, ...)` with `verbose` **hardcoded** at `:229`, ignoring its own `verbose` parameter (which it uses only for the recache at `:233`).
Verified first-hand at `46c9c83`, driving the real code path with `verbose=False`:

```
invoked with verbose=False
BENCH STREAM output on STDOUT?: True
BENCH STREAM output on STDERR?: False
raw STDOUT: "Updating the frappe framework with 'bench update --reset'\n\n
             Updating apps...\n✓ frappe updated\nMigrating site...\n
             ✓ Frappe framework updated\n"
```

An `axi` verb whose stdout must hold exactly one TOON document **cannot call this function**.
`axi apps update --app frappe` is not merely untidy today; it is impossible.
This is why the frappe fork moves FIRST (design Decision 4): it is 32 lines, it sits outside the state machine, and it is what makes the verb possible at all.
Note also that the three existing frappe tests (`tests/test_apps.py:557,567,576`) all pass `verbose=True`, which is exactly why the hardcoding is invisible to them.

**2. The same real-world event produces two behaviours, depending on whether Docker happened to record an exit code.**
A migration that *returns* non-zero is recorded in `failed_migrations` and the fan-out continues.
A migration whose stream is *lost* aborts the fan-out.
Both mean "we do not have a good outcome for this site", and nothing about the difference is meaningful to a user or an agent.
This batch unifies them (design Decision 2), and because that is a **behaviour change** it is argued here rather than slipped in.

**3. The reporting is correct today, but correct by careful placement rather than by construction.**
PR #81 fixed the gap the recon led with: `_report_summary` is now called from inside the `finally` (`update.py:841`), so a raise can no longer jump the summary.
**That gap is CLOSED on `develop`, and this proposal does not claim otherwise** (see Corrections 1).
What it cost to close is the argument that survives: a `finally`-placed report, a print-only discipline (raising from a `finally` would replace the in-flight exception), an `aborted` flag gated on `bool(sites_to_migrate)`, and roughly 25 lines of comment at `:491-497` and `:818-851` telling the next agent not to tidy any of it back.
Every one of those is load-bearing, and every one is a convention a refactor can silently break.
A returned `Result[UpdateReport]` does not need the convention: the report is a **value**, and a frontend that renders what it is handed cannot skip a summary the way an unwinding exception could.
The envelope does not fix a live bug here; it makes a fixed one unrepresentable.
That is a weaker claim than the recon made, and it is the honest one.

**4. The safety net does not cover the state machine the migration must preserve.**
Re-measured first-hand at `46c9c83` (the recon predates #81, which moved these numbers):

```
commands/update.py   387 stmts   112 miss   71.06%     (recon, pre-#81: 376 / 113 / 69.95%)
commands/apps.py     195 stmts    17 miss   91.28%     (unchanged)
```

Still **2 of the 7** aggregation branches are covered: `failed_maintenance_enable` (`:530-536`) and `failed_maintenance_disable` (`:566-575`).
`failed_apps` (`:526-528`), `failed_migrations` (`:539-543`), `failed_builds` (`:546-550`), `failed_cache_clears` (`:553-557`) and `failed_website_cache_clears` (`:560-564`) have none.
`--build` (`:418-441`, `:783`), `--skip-maintenance` (`:769`), `--clear-website-cache` (`:799`), the no-cache auto-inspect path (`:625-664`) and multi-app fan-out are entirely uncovered, and there is **no E2E for `apps` or `update` at all** (`tests/e2e/` holds backup, harness_safety, init, label, per_process_supervisor, run, start_status ×2, status_unsupervised, unlock).
"Refactor under green" is a real principle, but this green does not cover what moves.
Characterization tests therefore land FIRST, in this PR, before the migration (design Decision 3).

## What Changes

- **`core/update.py`, the UI-pure state machine.** `core.update(project_name, apps, *, bench=None, bench_path=None, sites=None, clear_cache=False, clear_website_cache=False, build=False, skip_maintenance=False, no_recache=False, auto_start=False, on_event=None) -> Result[UpdateReport]`. It owns the pull, the recache, the site discovery, the `--site` narrowing, the maintenance-mode lifecycle and its unconditional `finally`, the migration fan-out, the optional build/cache/lock clears, and the seven-way aggregation - **returned rather than printed**.
- **It is NOT a generator, and that is the batch's central architectural decision** (design Decision 1). `core.update` is a plain function taking an optional typed-event callback. A `try/finally` inside a generator does not run when a consumer breaks early while holding a reference, or lands in a reference cycle - re-verified first-hand, all five consumer shapes (Decision 1). A GUI pumping events from an event loop holds the iterator on `self`, which is exactly that case, and the GUI is what this rework exists to enable. This is a deliberate, captain-approved reading of **locked decision 4**, recorded with its probe so a later agent reading decision 4 alone does not reopen the hole with an elegant-looking refactor.
- **ONE call, not `update_plan` + `update_run`.** This is the one place the proposal's analysis diverges from the dispatch brief's *phrasing*, and it is raised rather than built around (design Decision 6). Decision 1's substance - not a generator, a plain function, an unconditional `finally`, a typed-event callback, no `rich`/`typer`/`questionary` in the core - is honoured exactly. The `plan` parameter is not: batch 3's two-phase split exists **because generators are lazy**, and a plain function has no laziness to work around. `core/backup.py` is the shipped precedent and the exact same shape (a minutes-long `bench backup`, one call, a terminal DTO, `NEEDS_CHOICE` returned at call time). **Captain: confirm or override.**
- **A lost stream continues the fan-out and is reported as UNKNOWN, never as a failure** (design Decision 2). Today a lost stream raises through `_stream_command`, aborting the fan-out; #81 made the summary survive that raise but did not make the fan-out continue. `core.update` catches the exec-stream `CwcliError` per exec, records the item as unknown, and carries on. Unknown is **not** folded into `failed_*`: an agent branching on `axi apps update` will retry a failure, and retrying a migration that may still be running is harmful. The report distinguishes "this failed" from "we lost track of this".
- **`UpdateReport`, the DTO the seven-way aggregation already is.** Six failure lists, three unknown lists (only the three phases that stream can produce an unknown), `affected_sites`/`migrated_sites` as the honest partial-failure signal, `frappe_reset: bool`, and a pre-computed `ok`. Every field a builtin; no live Docker object crosses the boundary.
- **The frappe fork moves first and loses its hardcoded `verbose=True`** (design Decision 4). Consumption becomes the caller's choice, exactly as batch 3 established for `_stream_command`. Its recache-before-exit-code-check (`:232-239`) is **preserved and noted, not "fixed"**: it is defensible (a partially-applied reset genuinely changes the cache) and untested either way, and a migration is not where that gets decided.
- **Both frontends reseated over ONE implementation.** `cwcli apps update` keeps its **positional** apps argument; the deprecated `cwcli update` keeps its **`--app`/`-a` option** form and its stderr-only deprecation warning. They are **not** interface-identical and both must survive byte-for-byte (Corrections 4).
- **`apps update --json`** joins its three siblings, and **`axi apps update`** ships as the agent verb: one TOON `UpdateReport` on stdout, exit 0/1/2.
- **The exit code comes from `report.ok`, NOT from `result.status`** (design Decision 5). The shipped `axi_backup` pattern maps `OK`/`WARNING` to exit 0, and a partial `update` failure is a `WARNING`-shaped envelope with `ok=False`. Copying that pattern verbatim would ship a fail-open on the exact surface this batch exists to make honest.
- **Characterization tests land first** (design Decision 3), covering the five untested aggregation branches plus `--build`, `--skip-maintenance`, `--clear-website-cache`, `--no-recache` and multi-app fan-out. They pass **before** the migration and **after** it, unchanged. The boundary is stated explicitly: the tests that must NOT change are the ones pinning behaviour this batch preserves; the stream-loss tests change **by design**, because Decision 2 changes what a stream loss does.
- **`apps update`'s first E2E**, both modes: pty-driven interactive, `--yes` non-interactive, and a non-TTY without `--yes` refusing non-zero.

## Capabilities

### New Capabilities

- `update-core-slice`: `core.update` returning a typed `Result[UpdateReport]` - the maintenance-mode state machine, the fan-out, and the seven-way aggregation moved onto the logic core UI-pure; the plain-function-plus-callback shape and the evidence for it; the unified stream-loss handling; the frappe fork's de-hardcoded consumption mode; and the two reseated frontends over one implementation.
- `apps-update-structured-output`: `--json` on the human `cwcli apps update` and the `cwcli axi apps update` verb, both emitting the same `UpdateReport`, with stdout purity on both surfaces and honest exit codes.

### Modified Capabilities

- (none. `openspec/specs/` holds no archived capabilities yet, so there are no existing requirements to amend. `apps`/`update` behaviour stays pinned by `tests/test_apps.py`, which must stay green in substance across the migration.)

## Impact

- **New source:** `src/caffeinated_whale_cli/core/update.py` (`update`, `UpdateReport`, the `UpdateEvent` types).
- **Modified source:** `commands/update.py` reseated as a renderer over `core.update` (keeping the deprecated alias's typer signature and warning); `commands/apps.py`'s `update_apps` shim gains `--json`; `commands/axi.py` gains `apps update`.
- **Reuses (no changes expected):** `core/envelope.py`, `core/errors.py`, `core/docker.py`, `core/resolvers.py`, `core/exec_stream.py`, `utils/cache.py`, `utils/db_utils.py`, `utils/toon.py`. **Expect ZERO new primitives** (design Decision 7), stated as a falsifiable claim as batches 1-3 did, **with one near-miss reported rather than resolved by bending**: `resolvers.require_bench_dir` probes `sites/` only, while `update` probes `apps/` AND `sites/`. Update keeps its own two-directory probe inside `core/update.py`; widening the resolver would change behaviour for `backup`/`unlock`, which this batch does not touch.
- **Tests:** characterization tests against current `develop` FIRST (Decision 3); then core unit tests for `core.update` (each aggregation branch, the maintenance-mode gate, the unconditional `finally`, unknown-vs-failed, the frappe fork, `NEEDS_CHOICE`, no live object in the DTO); reseated `commands/update.py` tests; `apps update --json` tests; `axi apps update` tests (one TOON document, exit 0/1/2). `tests/test_apps.py`'s existing 36 stay green in substance. One new both-modes `apps update` E2E.
- **Docs:** `README.md:839`'s "`apps update` ... has no `--json`" deleted and the two new surfaces documented; `CHANGELOG.md` entry; the `cwcli-apps-update` and `cwcli-core-axi` skills updated; `CLAUDE.md`'s core-migrated list updated.
- **Dependencies:** none added.

## Non-Goals

- **`apps list` / `install` / `uninstall`.** **Batch 5.** The recon's §6.2 case is accepted: `apps update` is a 57-line typer shim over `update.py` (`apps.py:476-532` -> `update.py:859` `run_app_update`), so the seam this batch cuts along **already exists in the code**, and it runs through the middle of `apps.py` rather than between the two files. The other three share `_run_bench`/`_capture_bench`/`_stream_bench` and `_report_and_exit`, already have `--json` (`apps.py:244,316,413`) and honest exit codes, and block nothing. Migrating `update` alone delivers the entire stated payoff, and bundling a mechanical 195-statement, 91%-covered migration next to a maintenance-mode state machine would enlarge the diff exactly where reviewers most need to be reading carefully.
- **A `--json` bolt-on without the core migration.** Considered and rejected, so it is visibly rejected rather than never considered. Re-routing the 46 stdout prints and emitting a dict is roughly 100 lines, but `axi` verbs are frontends over the core by construction (`axi.py:3-4`: "Each verb parses its flags, calls the SAME core function the human CLI calls"). There is no `axi apps update` without `core.update`, and the bolt-on would have to be undone by the real migration later.
- **`init`, `inspect`, `restore`, `rm`, `config`, `open`.** Unchanged from batch 3's list. `logs` stays a partial (reads `core/supervision.py`, no `core.logs`, no verb). `self_update`'s logic is already in `core/version.py` with only its mutating verb deferred.
- **plan/apply.** Still deferred to `restore`/`rm`. `update` is destructive-adjacent (it can leave a site in maintenance mode) but it is not a delete, and **this batch's single-call shape settles nothing about plan/apply** - as batch 3's two-call shape settled nothing about it either.
- **`axi update`, the deprecated spelling.** The deprecated surface is not worth an agent-facing verb, and batch 2's `self-update` discipline (ship a verb only when a real DTO justifies it) applies. `axi` gets ONE verb, on the canonical spelling.
- **Unifying `update.py:_get_sites_with_app` with `apps.py:_list_installed_apps`.** Reported, not fixed. They are two implementations of adjacent questions ("which sites have app X" vs "what is on site Y"); the first is cache-first with a live fallback, the second always live. A legitimate batch-5 item once `apps` migrates, and not a reason to do both in one batch.
- **The AXI cross-cutting shell.** Still `cwcli-axi-pass-x9`.
- **The accurate un-migrated list after this batch:** `inspect`, `restore`, `rm`, `config`, `init`, `open`, and `apps` (list/install/uninstall). `logs` stays a partial. `update` leaves the list.

## Corrections to the record

Batch 1 overrode the foundation recon; batch 2 overrode batch 1's Non-Goals prose; batch 3 overrode its own brief twice; the batch-4 recon overrode the brief's seam and batch 3's shipped `design.md`.
Continuing, from a first-hand audit of `46c9c83`:

1. **The recon's headline architectural argument is STALE, and this proposal does not make it.** The recon's §1.3 - "the single strongest architectural argument for the batch" - is that a `CwcliError` mid-fan-out unwinds past the seven-way aggregation, stripping a stuck site of its remediation. **PR #81 fixed that**, hours after the recon was written: `_report_summary` is extracted (`:478-577`) and called from the `finally` (`:841`), with an `aborted` flag recording that the fan-out stopped short. Verified: the summary now survives the raise. The batch stands on the frappe stdout blocker, the stream-loss inconsistency, the coverage gap, and the structural argument in Why 3 - **not** on a gap that is already closed.
2. **`_run_frappe_update_reset` does NOT run inside the maintenance-mode `try/finally`.** Batch 3's shipped `design.md` Risks and the batch-3 recon §4c both say it does. Both are false, and the recon already corrected them against `d6d2349` and `a8cd2a0`; **re-verified a third time at `46c9c83`**, where #81 moved every line: the frappe fork calls at `:707` and **returns at `:708`**, while the `try` opens at `:723` and the `finally` at `:818`. The path returns before the state machine is ever entered. Nothing was mis-built as a result, but the reason on file was wrong and would otherwise be inherited a third time.
3. **The `b8` comment is a regression GUARD, not a live bug.** Fixed in `f252a69`. It now sits at `:729-733` (the recon's `:625-629`, the batch-3 recon's and the old plan's `:627-631` - all three now stale). The migration neither fixes nor preserves it: **it removes the conditions that made the bug possible.** b8 was a presentation fork mis-bound to a data condition; in the core there is no presentation fork, because `verbose` becomes a pure frontend rendering choice. The bug class becomes structurally unrepresentable. This is stated explicitly because "the migration deletes the code the b8 tests pin" would otherwise read as coverage loss at review: `test_update_pulls_and_discovers_once` and `test_update_empty_affected_set_performs_neither_second_pass` (both parametrized over `verbose`) must survive **in substance**, re-pointed at the core, still asserting one pull and one discovery pass.
4. **The deprecated `cwcli update` is NOT interface-identical to `apps update`, and carries no frappe logic of its own.** It takes apps as `typer.Option(None, "--app", "-a", ...)` (`update.py:907`); `apps update` takes them as `typer.Argument(None, ...)` (`apps.py:482`). `cwcli update proj --app erpnext` versus `cwcli apps update proj erpnext`. Anyone assuming a pure passthrough will unify the signatures and break every existing `cwcli update ... --app x` invocation. The frappe special-case lives in `_update_project:687-708`, which **both** commands reach through `run_app_update`.
5. **Every line number in the recon is stale, and the file grew.** #81 took `update.py` from 929 to **993** lines and added `_report_summary` (`:478-577`), which did not exist when the recon was written. `_update_project` is now `:580-856`; the `try` `:723`; the `except BaseException` `:811`; the `finally` `:818`; the bench probe `:667-669`; `_run_frappe_update_reset` `:209-240`. Totals are now 532 + 993 = **1525**, not the recon's 1461 or the old plan's 1435.
6. **Coverage moved, and was re-measured rather than inherited.** `update.py` is **71.06%** (387 stmts, 112 missed), not the recon's 69.95% of 376. The 2-of-7 aggregation finding **still holds exactly**, and #81's new tests are what cover the `aborted` branch (`:517-523`).
