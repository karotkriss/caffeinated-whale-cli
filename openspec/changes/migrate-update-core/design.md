## Context

This is the rework's Step 3, batch 4: **`update` onto the logic core, and `axi apps update` delivered**.
It follows `core-logic-foundation` (the `backup` reference slice), `migrate-start-status-core`, `migrate-unlock-stop-core` (batch 1, PR #77), `migrate-label-core` (batch 2, PR #78), and `add-exec-stream-contract` (batch 3, PR #80), whose per-exec primitive this batch is the first real consumer of at the verb level.

OpenSpec reads no code, so every `file:line` below comes from a first-hand audit of this worktree at **`46c9c83`**, not from the backlog, the brief, or the batch-4 recon alone.
**That mattered unusually much here, because the ground moved after the recon was written:** PR #81 landed the `finally`-reporting fix hours later, taking `update.py` from 929 to 993 lines, adding a function (`_report_summary`) that did not exist when the recon was read, and **closing the gap the recon leads with**.
Every line number in the recon is therefore stale, and its headline architectural argument no longer holds.
Where the audit and any prior document disagree, the audit wins and the disagreement is named (proposal's "Corrections to the record", and Decisions 1 and 8 below).

### Audit: what was verified first-hand, and how

| Claim | Verification | Result |
|---|---|---|
| `apps update --app frappe` pollutes stdout when not verbose | drove `_update_project("proj", ["frappe"], verbose=False)` with a fake emitting real bench output | **bench stream on STDOUT: True**, stderr: False |
| the three frappe tests would catch it | read `tests/test_apps.py:557,567,576` | all three pass `verbose=True`. **None can see it** |
| the frappe fork runs inside the maintenance `try/finally` | read `update.py` | **NO.** call `:707`, return `:708`, `try` `:723`, `finally` `:818`. Refutes batch 3's `design.md` Risks (third inheritance) |
| a `try/finally` in a generator always cleans up | probed all 5 consumer shapes | **NO.** held-reference and reference-cycle leave it un-run |
| a plain function's `finally` always runs | same probe, control case | **yes**, on a raise mid-way |
| #81 closed the §1.3 reporting gap | read `update.py:818-856` | **yes.** `_report_summary` called from the `finally`, print-only, `aborted` gated on `sites_to_migrate` |
| `update.py` coverage | `pytest tests/test_apps.py --cov` | **387 stmts, 112 miss, 71.06%** (recon: 376/113/69.95%) |
| the 2-of-7 aggregation finding survived #81 | mapped missing lines onto `_report_summary` | **still 2 of 7.** `failed_maintenance_enable`, `failed_maintenance_disable` covered; five are not |
| `require_bench_dir` matches update's probe | read `resolvers.py:237-245` vs `update.py:667-669` | **NO.** resolver probes `sites/` only; update probes `apps/` AND `sites/` |
| the deprecated alias is a pure passthrough | read `update.py:907` vs `apps.py:482` | **NO.** `--app/-a` Option vs positional Argument |
| no E2E for `apps`/`update` | `ls tests/e2e/` + grep | **none** |
| baseline | `uv run pytest -q` | **870 passed**, 58 deselected |

(Baseline note: 169 unrelated errors appear unless `TMPDIR` is redirected. `/tmp/pytest-of-cmckay` is owned by `root` on this host from an earlier run, so every `tmp_path` test errors in setup. Environmental, outside the worktree, unrelated to this change; recorded so the next agent does not mistake it for a real failure.)

### Audit: the state machine, mapped at `46c9c83`

`_update_project` (`update.py:580-856`) has four phases, not one.

```
:595-680   RESOLVE      containers, frappe container, bench path
                        (:625-664 auto-inspect fallback when nothing is cached)
                        (:667-669 bench dir probe: apps/ AND sites/)
:687-708   FRAPPE FORK  if any app == "frappe": _run_frappe_update_reset(); RETURN
                        *** outside the try/finally entirely - Decision 4 ***
:711-721   STATE        7 failure lists + maintenance_sites + sites_to_migrate + aborted
:723       try:
:734-738     PULL       _pull_apps -> _recache_after_pull -> _discover_affected_sites
                        (ONE pass each - the b8 guard, :729-733)
:741-746     FILTER     --site narrowing; refuses if it narrows an affected set to empty
:750-763     ENABLE     _enable_maintenance, recording EACH site as it succeeds
                        failed_maintenance_enable = all_affected - maintenance_sites
:768-771     GATE       sites_to_migrate = sorted(maintenance_sites)
                        *** a site that never entered maintenance is NEVER migrated ***
:774-779     MIGRATE    verbose/quiet presentation split
:782-783     BUILD      optional
:788-809     CLEAR      cache / website cache / locks - all keyed on sites_to_migrate
:811-817   except BaseException: aborted = True; raise    (#81)
:818-851   finally:     _disable_maintenance, then _report_summary  (#81)
:855-856   EXIT         if has_errors: raise typer.Exit(1)
```

**The invariants the migration must not lose:**

- **The load-bearing gate** is `sites_to_migrate = sorted(maintenance_sites)` (`:771`). A site that could not enter maintenance is never migrated, never cache-cleared, never lock-cleared. It is one of only two aggregation branches with test coverage.
- **`_enable_maintenance` records each site AS it succeeds** (`:343-346`), not from a bulk return, so a mid-loop crash still leaves an accurate record of what to undo.
- **The `finally` guarantees the disable is ATTEMPTED**, not that it succeeds. A failed disable lands in `failed_maintenance_disable` and is the only failure that leaves the user's site **down** and needs a manual command.
- **`_report_summary` is print-only and called from the `finally`** (#81). Raising from a `finally` would replace the in-flight exception; the caller owns the exit.

### Audit: which phases can actually lose a stream

Only the phases that go through `exec_stream` can raise `CwcliError`; the rest use `container.exec_run` and get an exit code back.

| Phase | `update.py` | mechanism | can be UNKNOWN? |
|---|---|---|---|
| frappe reset | `:225-231` | `_stream_command` | **yes** |
| pull (per app) | `:271-282` | `_stream_command` | **yes** |
| migrate (per site) | `:386-388`, `:408` | `_stream_command` | **yes** |
| build (per app) | `:427-435` | `_stream_command` | **yes** |
| maintenance on/off | `:97` | `exec_run` | no |
| clear cache / website cache | `:454` | `exec_run` | no |
| clear locks | `:473` | `exec_run` | no |
| site discovery | `:70` | `exec_run` | no |
| bench dir probe | `:251` | `exec_run` | no |

Three streaming phases sit inside the state machine (pull, migrate, build); the frappe reset returns before it.
That is what bounds Decision 2's blast radius to **three** unknown lists rather than seven.

## Goals / Non-Goals

**Goals:** deliver `axi apps update` and `apps update --json`, the batch's stated payoff; remove the hardcoded consumption mode that makes the verb impossible; unify two behaviours for one real-world event; move the maintenance-mode state machine onto the core with its reporting as a returned value rather than a placement convention; and cover the state machine with characterization tests before touching it.

**Non-Goals** (the proposal holds the full list): `apps list`/`install`/`uninstall`, a `--json` bolt-on, `init`/`inspect`/`restore`/`rm`/`config`/`open`, plan/apply, `axi update` on the deprecated spelling, unifying the two site-discovery helpers, the AXI shell.

## Decisions

### 1. `core.update` is NOT a generator. It is a plain function with a typed-event callback

This is the batch's central architectural decision, it is a **deliberate reading of locked decision 4**, and it is recorded with its evidence so a later agent does not reopen it.

The obvious shape, by analogy with batch 3's `run_stream`, is `core.update_stream(plan) -> Iterator[UpdateEvent]`.
That puts the maintenance-mode `try/finally` **inside a generator**, which makes the cleanup guarantee depend on the consumer.
Re-probed first-hand at `46c9c83` (the recon ran this; it is the decision's whole basis, so it was re-run rather than inherited), with `state` standing in for a site left in maintenance:

```
CASE 1  consumer exhausts it                     finally ran: True   state: clean exit
CASE 2  breaks early (refcount drops)            finally ran: True   state: maintenance OFF
CASE 3  breaks early, HOLDS a reference          finally ran: False  *** state: MAINTENANCE ON ***
        ... after del + gc.collect()             finally ran: True
CASE 4  generator in a reference CYCLE           finally ran: False  *** state: MAINTENANCE ON ***
        ... after an explicit gc.collect()       finally ran: True
CASE 5  contextlib.closing(...)                  finally ran: True   (deterministic)

PLAIN FUNCTION control:
CASE 6  plain function, raise mid-way            finally ran: True   state: maintenance OFF
```

- **Cases 3 and 4 are the problem, and they are not exotic.** A GUI pumping events from an event loop holds the iterator on `self`; a widget tree is a reference cycle. That is CASE 3 and CASE 4 exactly. If the window closes mid-update, **a site stays in maintenance mode until the garbage collector happens to run**. The rework exists to enable that GUI (locked decision 1), so the generator shape is actively hostile to the rework's own goal, on its single most safety-critical invariant.
- **Today's guarantee is unconditional** because the `finally` sits in a plain function (CASE 6): `_update_project` cannot return without running it.
- **`contextlib.closing` (CASE 5) restores determinism and is rejected**: it relocates a safety-critical guarantee into the frontend's hands - every frontend, forever, including ones not yet written. That is the wrong place for it.
- **Decision.** `core.update(...) -> Result[UpdateReport]`, a plain function taking an optional `on_event` callback. The maintenance-mode `finally` stays unconditional. The callback is batch 3's three consumption modes made explicit: the verbose CLI passes a renderer, the non-verbose CLI passes one that renders only step boundaries (it runs under a `console.status` spinner that streaming would shred), and `axi` passes `None` (drain and discard).
- **The tension with locked decision 4 is real and is resolved deliberately, not quietly.** Decision 4 says "streaming operations return typed event iterators". The reading: it governs genuine **streaming operations** - `logs`, and raw exec output, which is precisely what `core.exec_stream` already is and remains. `update` is not a streaming operation; it is a **state machine that emits progress**, and its terminal value is a report, not a stream. Batch 3's `run_stream` has the same GC exposure **and it is fine**: an abandoned `run_stream` leaks a socket until GC, not a stuck site.
- **A callback receiving typed events is not UI in the core.** It carries no `rich`, no `questionary`, no `typer` - only dataclasses of builtins. `tests/test_core_envelope.py`'s import ban globs `core/*.py` and covers the new module automatically.
- **Rejected: emit progress by returning both an iterator and a report.** Two return values, one of which reopens CASE 3/4 for the half that matters.

### 2. A lost stream continues the fan-out and is UNKNOWN, never a failure

Today, one real-world event produces two behaviours depending on whether Docker happened to record an exit code:

| what happened | `update.py` today | fan-out |
|---|---|---|
| migration **returns** non-zero | recorded in `failed_migrations` (`:390`, `:410`) | **continues** |
| migration's **stream is lost** | `_stream_command` raises `typer.Exit(1)` (`:53-59`) | **aborts** |

Nothing about that difference is meaningful to a user or an agent.
PR #81 made the summary survive the abort; it did not make the fan-out continue.

- **Decision.** `core.update` catches the exec-stream `CwcliError` per exec, records the item as **unknown**, and continues the fan-out. The two behaviours become one.
- **Unknown is NOT folded into `failed_*`, and that is the point.** A lost stream means the exit code is unknowable and **the command may still be running**. An agent branching on `axi apps update` will retry a failure; retrying a live migration is harmful. The report must distinguish "this failed" (retry is safe) from "we lost track of this" (retry is not).
- **All exec-stream `CwcliError`s are unknown; `exec.start_failed` is NOT split out into a failure.** Considered: `exec_stream` raises three codes, and `exec.start_failed` (`exec_create` threw) arguably means the command definitely never ran, which would make it a safe-to-retry failure. **Rejected.** It is a confident claim of "did not run" derived from an API call whose own outcome is uncertain, and the safe direction for a retry decision is unknown. The one `if` it would save is not worth being wrong about a live migration.
- **Blast radius: three lists, not seven.** Only pull, migrate and build stream inside the state machine (see the audit table). The frappe reset streams too, and maps onto `failed_apps`/`unknown_apps` with the app name `frappe`, so it needs no field of its own.
- **A dead daemon degrades honestly and terminates.** Every subsequent `exec_create` fails fast, so each remaining item is recorded unknown and the run ends with a report rather than a hang. The 10s poll bound only applies when a stream ended without a code, so it does not multiply across the fan-out.
- **The envelope's `warnings` carries the why.** Each stream loss appends a `Message("exec.stream_lost", ...)`, so the structured report says *what* is unknown and the warnings say *why*, using machinery that already exists.
- **No new hazard is introduced by continuing.** Disabling maintenance on a site whose migration may still be running is what today already does: the `finally` disables every enabled site on the abort path too. Continuing changes which sites get migrated, not that property. Noted as pre-existing rather than defended as new.

### 3. Characterization tests land FIRST, and the boundary of "unchanged" is stated

Coverage is 71.06% with 2 of 7 aggregation branches (re-measured; Why 4).
"Refactor under green" against that net is aspirational, and batch 3's own re-point claimed it while the untested branches were exactly where #81's bug then shipped.

- **Decision.** The characterization tests land **before** the migration, in this PR, against current `develop`: the five untested aggregation branches (`failed_apps`, `failed_migrations`, `failed_builds`, `failed_cache_clears`, `failed_website_cache_clears`), plus `--build`, `--skip-maintenance`, `--clear-website-cache`, `--no-recache`, and multi-app fan-out. They must pass before the migration and after it, **unchanged**.
- **The boundary, stated explicitly, because "unchanged" cannot be universal in a batch with a deliberate behaviour change.** The tests that must not change are the ones pinning behaviour this batch **preserves**. The **stream-loss tests change by design** (Decision 2 changes what a stream loss does): `test_update_stream_loss_mid_fanout_still_reports_stuck_site_remediation` (parametrized over `verbose`) today asserts the summary and remediation survive a **raise**; after this batch a stream loss does not raise, so it is re-pointed to assert the summary and remediation survive a **recorded unknown**, the fan-out **continues**, and the remaining site is still migrated. Its substance - a stuck site keeps its remediation when a stream is lost - is strengthened, not dropped.
- **`test_update_site_filter_refusal_is_not_reported_as_an_interrupted_update` survives** and keeps pinning the `aborted`-gated-on-`sites_to_migrate` rule.
- **The two b8 tests survive in substance**, re-pointed at the core, still asserting one pull and one discovery pass (Corrections 3).

### 4. The frappe fork moves FIRST, and loses its hardcoded `verbose=True`

- **The recorded risk was wrong; the real one is bigger.** Batch 3's `design.md` Risks say the re-point "runs inside the maintenance-mode `try/finally`". It does not, and never did (audit table; the fork returns at `:708`, the `try` opens at `:723`). Its real risk is `:229`'s hardcoded `verbose=True`.
- **It is the one path in either file that hardcodes a consumption mode.** Batch 3's whole contribution is that `verbose` decides whether to **render**, not how to **obtain**. This function did not get that message, because its `verbose=True` predates the fork's collapse and survived it.
- **It is therefore the direct blocker for the verb.** Verified: `verbose=False` still puts bench output on stdout (Why 1). An axi verb whose stdout must hold exactly one TOON document cannot call it.
- **It is the least-constrained path**: `bench update --reset` resets every app repo, pulls, migrates every site, and rebuilds - and it is gated by nothing. `apps update --app frappe` needs no `--yes` and prompts for no confirmation (the only prompt in the flow is `ensure_containers_running`'s auto-start, `:598`). That is also why it is the E2E's primary target (captain's ruling 5).
- **Decision.** The frappe fork moves first: 32 lines, outside the state machine, with three tests. Consumption becomes the caller's choice via `on_event`.
- **Its recache-before-exit-code-check (`:232-239`) is PRESERVED and noted, not fixed.** A failed `bench update --reset` still triggers a full recache and only then reports failure. That is defensible (a partially-applied reset genuinely changes the cache) and untested either way. A migration is not where that gets decided.
- **Its `--site`/`--clear-cache`/`--build`/`--skip-maintenance` ignore-announcement (`:687-706`) is preserved**, warnings-carried rather than printed from the core.

### 5. The exit code comes from `report.ok`, NOT from `result.status`

This is a small decision that would otherwise ship a fail-open on the exact surface the batch exists to make honest.

- **The shipped pattern maps `WARNING` to 0.** `axi.py:253` (backup) and every other verb: `raise typer.Exit(0 if result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)`.
- **A partial `update` failure is `WARNING`-shaped.** The envelope's own comment calls `WARNING` "completed with non-fatal issues (partial fan-out, no-op)", and the closed `Status` set has no `ERROR` member by design (hard failures raise `CwcliError`; the envelope has no `errors` field). But `update`'s partial failures are precisely what must **not** raise - reporting all of them is the whole job.
- **Decision.** `UpdateReport.ok` is the pre-computed aggregate (matching `has_errors` at `:499-508` and `apps.py:205`'s `"ok"` key, per the axi guideline that agents should not re-derive aggregates), and **both** frontends exit on it: `axi apps update` exits `0 if report.ok else 1`, not on `result.status`. `Status` stays for envelope-level notes (`bench.default_used`, a failed recache, a lost stream).
- **`NEEDS_CHOICE` -> exit 2** via `emit_axi_choice_as_usage_error`, unchanged.
- **The `--site`-matched-nothing refusal keeps exit 1 on the human CLI.** It raises `CwcliError(USAGE)` from the core (it cannot live in a resolve phase: it needs the discovered sites, so it fires mid-work), and the reseated frontend maps it to exit 1 to preserve today's behaviour byte-for-byte. On `axi` it maps through `exit_for(USAGE)` to **2**, which is correct for a new agent surface (the agent passed a bad `--site`) and is a difference between surfaces, not a regression on either.

### 6. ONE call, not `update_plan` + `update_run`. Raised, not built around

The dispatch brief specifies `core.update_run(plan, *, on_event=None) -> Result[UpdateReport]`.
**Decision 1's substance is honoured exactly** - not a generator, a plain function, an unconditional `finally`, a typed-event callback, no UI in the core.
The `plan` parameter is where the analysis diverges, and the standing instruction is to say so plainly rather than quietly build around it.

- **Batch 3's two-phase split exists BECAUSE GENERATORS ARE LAZY** (`core/run.py:1-33` says so at length): a core function returning `Iterator[...]` cannot raise at call time and cannot return `NEEDS_CHOICE` at all. **A plain function has no laziness to work around.** `core.update(...)` can raise `CwcliError` at call time and return `NEEDS_CHOICE` from the same call. The forcing function that justified the seam in batch 3 is absent here.
- **`core/backup.py` is the shipped precedent and the exact same shape.** A minutes-long `bench backup`, ONE call, `NEEDS_CHOICE` returned at call time, a terminal DTO: `backup(project_name, *, site=None, bench=None, bench_path=None, with_files=False) -> Result[BackupOutcome]`. `update` is `backup` plus a fan-out plus a callback. The recon itself makes this comparison for the axi verb ("`axi backup` runs a `bench backup` - minutes-long, unbounded - and emits only a terminal `BackupOutcome`"); it applies to the core function too.
- **The house naming follows.** `core/backup.py` exposes `backup()`, `core/stop.py` exposes `stop()`. `core/update.py` exposes `update()`, reached as `core_update.update(...)`. `update_run` is a name that only earns its keep next to an `update_plan`.
- **A plan phase would also be incomplete, which is the tell.** `_fail_if_site_filter_matched_nothing` (`:190-206`) is a resolve-shaped refusal that **cannot** live in a resolve phase: it needs the discovered affected sites, which only exist after the pull and the discovery pass. A plan that cannot hold one of the resolution decisions is structure without a job.
- **What a split would buy, weighed honestly:** an independently serializable `UpdatePlan` a future GUI could preview ("about to update X on bench Y"). That is speculative today, it is what `on_event`'s first event can carry anyway, and it is one refactor away if a GUI ever wants it.
- **Decision (proposed).** ONE call: `core.update(project_name, apps, *, bench=None, bench_path=None, sites=None, clear_cache=False, clear_website_cache=False, build=False, skip_maintenance=False, no_recache=False, auto_start=False, on_event=None) -> Result[UpdateReport]`. **Captain: confirm or override.** If overridden, the split is mechanical and changes nothing else in this design.

### 7. Zero new primitives, as a falsifiable claim - and the near-miss is REPORTED, not resolved by bending

Batch 1 came back zero bent; batch 2 parameterized one hardcoded string; batch 3's resolvers came back zero bent while its own new primitive needed correcting, and an existing test suite caught it.
The claim's value is the signal, so here it is, before implementation, stated so it can be falsified.

**The claim:** `core.update` needs **zero new shared primitives**, and there is **one near-miss that must not be resolved by bending the primitive**.

| # | `update` step | `update.py` | Primitive | Exists? |
|---|---|---|---|---|
| 1 | ensure containers running | `:598` | `resolvers.resolve_container_state(offer_choice=True)` | yes |
| 2 | frappe container | `:601-614` | `core_docker.get_frappe_container` | yes |
| 3 | resolve bench | `:618` | `resolvers.resolve_bench` | yes |
| 4 | no-cache fallback | `:625-664` | `resolvers.DEFAULT_BENCH_PATH` + a warning | yes |
| 5 | bench dir probe | `:667-669` | `resolvers.require_bench_dir` | **NEAR-MISS** |
| 6 | every exec | 4 streaming + 5 blocking sites | `core.exec_stream` / `exec_run` | yes (batch 3) |
| 7 | site discovery | `:110-175` | update-specific; belongs in `core/update.py` | n/a |

**The near-miss, precisely.**
`resolvers.require_bench_dir` (`resolvers.py:237-245`) probes **`{bench_path}/sites` only**.
`update.py:667-669` probes **`{bench_path}/apps` AND `{bench_path}/sites`**.

- Use `require_bench_dir` as-is -> silently **drops** update's `apps/` check. A behaviour change, in the quiet direction.
- Widen `require_bench_dir` to check both -> **adds** a check to `backup`/`unlock`, which do not have it. A behaviour change to commands this batch does not touch. **This is the bending the standard exists to catch.**
- **Keep update's own two-directory probe inside `core/update.py`.** No primitive bent, no behaviour changed, a few lines not shared. **Decision.**

**While there:** `update.py:243-252`'s `_dir_exists` uses `["sh", "-c", "test -d " + shlex.quote(path)]`; `require_bench_dir` uses `["test", "-d", path]` - no shell at all, and strictly safer. The core version adopts the shell-free form. A simplification, not a behaviour change; `test_update_shell_interpolations_are_shlex_quoted` guards the property either way.

**The Decision 8 trap, applied** (batch 3 established it): a migration must resolve **no more** than the command does today.
`resolvers.validate_site_name` and `resolvers.resolve_default_site` exist and are one import away.
`update` uses **neither**: it takes `--site` as a filter, never resolves a default, and `shlex.quote`s every interpolation (`:92,146,160,385,404,424,448,467`, pinned by `test_update_shell_interpolations_are_shlex_quoted`).
**Adding `validate_site_name` would add failures `cwcli apps update` does not have today.** Do not.

### 8. Two prior claims are wrong, and the code says so

Recorded rather than absorbed, because the standing instruction is that the code wins.

- **The recon's §1.3 - its "single strongest architectural argument for the batch" - is stale.** PR #81 closed it. Verified at `:818-851`. The batch is argued on the frappe stdout blocker, the stream-loss inconsistency, the coverage gap, and the by-construction-versus-by-convention argument, and on nothing else. **Impact on the batch: the scope is unchanged; only the Why is.**
- **`_run_frappe_update_reset` does not run inside the maintenance-mode `try/finally`.** Batch 3's shipped `design.md` Risks say it does; the batch-3 recon §4c says it does. Both were wrong at `d6d2349`, wrong at `a8cd2a0` (the recon's verification), and wrong at `46c9c83` (re-verified here, after #81 moved every line). **Impact on the batch: none** - the fork moves first for a different and better reason (Decision 4) - but a third inheritance of a false reason stops here.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateReport:
    project: str
    bench_path: str
    apps: list[str]
    frappe_reset: bool                    # the bench-wide path ran; per-site fields are empty
    affected_sites: list[str]
    migrated_sites: list[str]             # == sites_to_migrate, the load-bearing gate (:771)
    failed_apps: list[str]                # git pull (or bench update --reset) failed
    unknown_apps: list[str]               # its stream was lost; it MAY still be running
    failed_maintenance_enable: list[str]  # affected but NOT migrated
    failed_migrations: list[str]
    unknown_migrations: list[str]         # *** may still be running: do NOT retry blindly ***
    failed_builds: list[str]
    unknown_builds: list[str]
    failed_cache_clears: list[str]
    failed_website_cache_clears: list[str]
    failed_maintenance_disable: list[str] # *** STUCK: the site is DOWN, needs manual action ***
    aborted: bool                         # the fan-out stopped early (Ctrl-C / an unexpected error)
    ok: bool                              # pre-computed aggregate; the exit code reads THIS

# The callback's vocabulary. Typed events only: no rich, no typer, no questionary.
@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateStepStart:
    phase: str                  # "pull" | "migrate" | "build" | "maintenance_enable" | ...
    item: str | None            # the app or site, when the phase has one
    index: int                  # 1-based, for "Pulling app: x (1/3)"
    total: int

@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateOutput:
    phase: str
    item: str | None
    stream: str                 # "stdout" | "stderr" - carried through from ExecChunk
    text: str

@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateStepEnd:
    phase: str
    item: str | None
    status: str                 # "ok" | "failed" | "unknown"

@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateAborted:
    report: UpdateReport        # see below

UpdateEvent = UpdateStepStart | UpdateOutput | UpdateStepEnd | UpdateAborted
```

- **`failed_maintenance_disable` is the field that justifies the verb.** It is the only outcome that leaves the user's site *down*. Today it is reachable only by scraping `rich`-marked-up prose off stdout. In the DTO it cannot be lost.
- **`migrated_sites` vs `affected_sites` is the honest partial-failure signal**, and it is the `:771` invariant. An agent seeing `affected=[a,b], migrated=[a]` knows precisely what did not happen.
- **`frappe_reset: bool` is how the two paths share one DTO** rather than sprouting a union. The frappe path genuinely has no per-site data (it ignores `--site`, `:687-706`), so those lists are simply empty and the flag says why. Its own outcome rides in `failed_apps`/`unknown_apps` as the app `frappe`.
- **Every field is a builtin.** No live Docker object crosses the return boundary (locked decision 4).
- **`UpdateAborted` preserves #81's property through the migration, and this is the subtle one.** Today a Ctrl-C mid-update still prints the summary with its stuck-site remediation, because `_report_summary` runs in the `finally` (`:841`) and the skill notes the `except BaseException` is deliberately not `except Exception` for exactly this reason. A **returned** report cannot survive an unwinding `KeyboardInterrupt` - there is no return. So the core's `finally` builds the report and, when unwinding, hands it to `on_event(UpdateAborted(report=...))` before the exception continues. The frontend renders the same report through the same renderer either way. Decision 2 removes the only *realistic* raise from inside the fan-out, so this covers Ctrl-C and genuine bugs - which is precisely what `aborted` is now for.
- **`aborted` is `False` on every returned report** (a return means no abort) and `True` only inside `UpdateAborted`. The mild redundancy is deliberate: it keeps the renderer single-argument and mirrors today's `_report_summary(aborted=...)` parameter exactly.

## Boundary discipline

`core/update.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py` globs `core/*.py` and covers it automatically.
No live Docker object crosses the return boundary: every field of `UpdateReport` and every event is a builtin.
The `on_event` callback receives dataclasses only, so a frontend's renderer is the only thing that knows what a spinner is.
`core.update` prints nothing, prompts nothing, and calls no `typer.Exit`: it returns `NEEDS_CHOICE` for the multi-bench and stopped-container forks, raises `CwcliError` for hard failures, and returns `Result(status, UpdateReport(...))` otherwise.

## Risks / Trade-offs

- **This is the largest thing the rework has done.** `update.py` at 387 statements with 46 stdout `console.print` calls and 6 stdout `console.status` spinners interleaved through the state machine (re-measured at `46c9c83`: 84 `console.print` less 38 `stderr_console.print`; 7 `console.status` less 1 on `stderr_console`) lands a `core/update.py` around 300-450 lines plus a reseated renderer plus a test suite that must be larger than what exists. Calibration: `status` (the largest core to date) is 335 core lines; `run` (batch 3) is 122. Mitigation: characterization tests first (Decision 3), the frappe fork first (Decision 4), and a scope that stops at the seam the code already has.
- **The maintenance-mode `finally` is the single most safety-critical thing in the batch**, and it must be **re-verified by test, not by inspection** - the same requirement batch 3 set for its re-point, which is how #81's bug was found in the first place.
- **Decision 2 is a behaviour change on a destructive-adjacent command.** A lost stream stops aborting. Argued, not slipped in; and the direction is toward the behaviour the non-lost-stream case already has.
- **Decision 1 deliberately reads a locked decision.** Recorded with the probe so the reading is auditable and a later "elegant" refactor to a generator has to argue with the evidence rather than with prose.
- **The characterization tests cannot cover the paths the migration is most likely to break silently** - the auto-inspect fallback (`:625-664`) needs `inspect` faked, and is uncovered today. Noted as a known thin spot rather than claimed as covered.
- **`apps update --app frappe` runs `bench update --reset` gated by nothing** and is the E2E's primary target for exactly that reason (captain's ruling 5).

## Migration Plan

Ordered so the risky part lands under tests that actually cover it, and the enabling part lands first.

1. **Characterization tests against current `develop`** (Decision 3). Green before anything moves.
2. **The frappe fork** (Decision 4): move it, drop the hardcoded `verbose=True`, make consumption the caller's choice. Smallest, most isolated, and the thing that unblocks the verb.
3. **`core/update.py`**: `UpdateReport` + the event types + `core.update`, with the state machine, the unconditional `finally`, and the seven-way aggregation returned. **Report the zero-new-primitives verdict** (Decision 7).
4. **Reseat `commands/update.py`** as a renderer over the report; keep both frontends (`apps update` positional, `cwcli update --app/-a`) and the deprecation warning, over ONE implementation.
5. **`apps update --json`**, matching its three siblings' stdout-purity discipline.
6. **`axi apps update`**: one TOON `UpdateReport`, exit 0/1/2 (Decision 5).
7. **Both-modes E2E on a real instance**, `--app frappe` included.
8. **Docs, skills, CHANGELOG.** `README.md:839` is the line this batch deletes.

## Open Questions

- **ONE call versus `update_plan` + `update_run`** (Decision 6). The proposal's analysis says one call, matching `core/backup.py`; the brief's phrasing says two. Raised for the captain rather than built around. Mechanical either way.
- **The exact event vocabulary** (the DTOs above). The shape is settled - typed events, a callback, three consumption modes - but the precise field list is tuned in implementation against what the two renderers actually need to reproduce today's output. Batch 3 deliberately left its poll bound open the same way.
- **Whether the non-verbose CLI keeps all 6 `console.status` spinners** once the phase boundaries are events. A rendering choice for implementation to make and report; today's spinner text is reproducible from `UpdateStepStart(phase, item, index, total)`.
- **`update.py:_get_sites_with_app` versus `apps.py:_list_installed_apps`** (proposal Non-Goals). Two implementations of adjacent questions. A batch-5 item once `apps` migrates; filed, not fixed.
