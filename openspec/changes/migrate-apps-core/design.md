## Context

Batch 5 of the logic-core rework: `apps list` / `install` / `uninstall` onto the core.
The seven foundation forks (`core-logic-foundation/design.md`) are locked and are NOT reopened here.
This document records only the forks this batch actually hit, so a later reader cannot silently reopen a settled one.

Ground truth was read first-hand from `commands/apps.py` at `3ff07ca` (534 lines); the `file:line` citations below are from that read, not inherited from a prior recon.

## Decisions

### 1. `axi` scope = `axi apps list` ONLY; the two mutations are deferred

Ship the read verb. Do NOT ship `axi apps install` or `axi apps uninstall`.

- **Why it wins (captain-locked, 2026-07-15).**
  `axi apps uninstall` would let an agent destroy site data (`bench uninstall-app` drops the app's tables).
  That is a product decision about what agents are permitted to do, not a technical consequence of migrating a command, and it does not belong in a batch whose job is to move logic without changing behaviour.
  Batch 3 is the precedent: `run` was migrated and shipped NO verb at all.
  `axi apps list` carries no such question - it is a read, it has a clean DTO, and it answers "which apps, on which site" which the agent surface genuinely cannot answer today.
- **Rejected: all three, with `--yes` required on uninstall.**
  Defensible and consistent with `axi apps update` already existing, but it settles the destructive-agent-verb question as a side effect of a refactor. If agent-driven uninstall is wanted, it should be decided on its own evidence.
- **Rejected: `list` + `install` (additive only).**
  Splits the mutations on "does it delete data", which is a real line, but `install` still mutates a real site and there is no reason to decide half the question now.
- **The deferral is recorded as tasks (`tasks.md` §7), not as prose.**
  A deferred decision nobody can find is the same failure mode as dead code left behind after it is replaced: the next reader cannot tell "deliberately not built" from "forgotten".

### 2. The three core functions are PLAIN FUNCTIONS with an optional event callback

`list_apps`/`install_apps`/`uninstall_apps` return a `Result`; progress rides `on_event: Callable[[AppsEvent], None] | None`.
None of them returns an iterator.

- **Why it wins.** Batch 4 settled this for exactly this shape: `core.update` is a state machine that emits progress and whose terminal value is a report, and locked Decision 4's "typed event iterators" governs genuine streaming (`logs`, raw exec output - which `core.exec_stream` already is).
  `install`/`uninstall` are fan-outs with real logic between execs and an aggregate report at the end. Identical shape, identical answer.
- **The generator's GC hazard is WEAKER here than in `update`, and that is worth stating honestly rather than borrowing batch 4's argument wholesale.**
  `update`'s `try/finally` disables maintenance mode; a generator abandoned in a reference cycle strands a site in maintenance.
  `install`/`uninstall` have no such cleanup block, so an abandoned generator would leak a socket, not corrupt state - the `run_stream` situation, which is fine.
  The decision therefore rests on shape-consistency with `core.update`, not on the safety argument. Do not cite the maintenance-mode hazard as if it applied here.
- **`list_apps` takes no callback at all.** It is a read with two cheap execs and nothing to report progress about.

### 3. The recache stays in the FRONTEND; this batch adds ZERO new core-to-CLI reaches

`core.install_apps`/`uninstall_apps` do NOT call `cache.recache_project`.
The frontend calls it after the core returns, gated on `any(r.ok for r in report.results)` read off the returned report.

- **Why it wins, and why it is not the same as `update`.**
  `AGENTS.md` records that `core/update.py` is the FIRST core module to reach back into the CLI layer at runtime (`cache.recache_project` lazily imports the `inspect` COMMAND), that it is unavoidable *because it runs mid-fan-out*, and that "it should stay the one place".
  Migrating `install`/`uninstall` naively would make it three places.
  But their recache is not mid-fan-out: it is an epilogue after every mutation (`apps.py:377-378`, `:465-466`), and the condition it gates on is already in the returned report.
  An epilogue hoists to the frontend for free, so the rule holds without bending anything.
- **Disclosed cost.** Each frontend repeats the recache call (two lines). Today that is one frontend, since Decision 1 defers the axi mutations; if they ever ship, they repeat two lines rather than the core acquiring a CLI import.
  That is the right trade: a duplicated two-line epilogue is visible and cheap, a core-to-CLI reach is neither.
- **The warning text on a failed recache stays in the frontend too**, because it is rendering.

### 4. `uninstall`'s fused `--yes` is untangled at the CORE boundary only

`cwcli apps uninstall --yes` keeps its exact current meaning (skip the destructive confirm AND auto-start containers).
`core.uninstall_apps` takes the two as separate params: `auto_start: bool` and the destructive consent, surfaced as a `confirm_uninstall` `NEEDS_CHOICE`.

- **Why.** The flag's own help text admits the fusion: "Skip the destructive confirmation and auto-start containers" (`apps.py:415`).
  Two unrelated consents behind one flag is a UX question, not a migration question - so the CLI keeps it byte-for-byte (a migration does not change the contract) while the core, which must serve axi and a future GUI, models them as what they are.
- **This is what makes Decision 1's deferral cheap.**
  When `axi apps uninstall` is decided, the core already exposes destructive-consent separately from auto-start, so the verb does not have to re-open a fused flag - it wires the one it means. Batch 4 learned this the hard way: `axi apps update`'s first cut threaded `auto_start=yes` into a core function that never performs a start, and exec hit a stopped container with a raw `docker.errors.APIError`.
- **A stopped container in the core is `NEEDS_CHOICE`/`confirm_start`, never an auto-start the core performs.** `resolve_container_state(auto_start=True)` only REPORTS `start_requested`; the caller performs the start. The CLI frontend keeps its `ensure_containers_running` prologue, which actually starts it, exactly as `commands/update.py` does.

### 5. The exit code reads `report.ok`, NOT `result.status`

`install`/`uninstall` exit 1 iff any per-(app, site) step failed.

- **Why this needs its own decision.** It is batch 4's trap, and it is live here for the same reason: the closed `Status` set has no `ERROR` member, so a partial fan-out failure is a `WARNING`-shaped envelope, and every other verb maps `WARNING` to exit **0**.
  Copying the shipped `raise typer.Exit(0 if result.status in (OK, WARNING) else 1)` pattern would report success for an uninstall that half failed.
  `_report_and_exit` gets this right today (`apps.py:197,219-220`); the migration must not lose it.
- **`list` has the same shape** and the same answer: a site whose `list-apps` read failed is `installed[site] = None` and exit 1 (`apps.py:263,287-288`). Preserve it, including the JSON branch emitting the document BEFORE the non-zero exit (`apps.py:269-272`).

### 6. Refactor-under-green, with the boundary of "unchanged" stated up front

`tests/test_apps.py` is the net. It must pass before and after, unchanged.

- **Where "unchanged" is expected to hold:** every existing assertion about flags, messages, exit codes, and the JSON shapes.
- **Where test edits are expected BY DESIGN: exactly two, identified before implementation and named here.**
  The draft of this decision claimed "none currently identified". That was **wrong**, and re-reading the suite falsified it: `tests/test_apps.py:1151` (`test_capture_bench_reports_cwclierror_cleanly`) and `:1161` (`test_stream_bench_reports_cwclierror_cleanly`) bind directly to `apps_mod._capture_bench` / `apps_mod._stream_bench` - the exact helpers task 4.2 deletes.
  Their SUBJECT moves; their BEHAVIOUR does not. Both pin "a `CwcliError` out of `exec_stream` renders `Error:` to stderr and exits 1", and that must still hold - relocated from inside the helper to the frontend's `try/except CwcliError` around the `core.<verb>` call, because the core cannot exit.
  They are therefore REPLACED by equivalents bound to the new subject, with their assertions intact.
  **Precedent, exactly:** batch 4 hit this and did the same thing - `tests/test_apps.py:1169`'s own comment reads "REPLACES test_update_stream_command_reports_cwclierror_cleanly, whose subject (update.py's `_stream_command`) moved into core.update with the state machine."
  Everything else in `tests/test_apps.py` passes untouched.
- **Why the streaming still works once the exec is in the core.** `_stream_bench` wrote to stdout, which the core may never do. The live output rides Decision 2's `on_event` callback: the core emits each `ExecChunk`'s text as an event, and the frontend's callback writes it to stdout (human) or buffers it (JSON). Both of the exec-stream contract's consumption modes are preserved, and the choice of which one moves to where it belongs - the renderer.
- **Coverage baseline must be re-measured first-hand, not inherited.** Batch 4's tasks.md records the recon's numbers being stale by a PR; assume the same here. Done in task 1.1.

### 7. Do NOT add resolvers `apps` does not use today

`core.list_apps`/`install_apps`/`uninstall_apps` resolve container + bench and nothing else.
No `resolve_default_site`, no `validate_site_name`, no `validate_bench_path`, no `require_bench_dir`.

- **Why.** This is batch 3's Decision 8 trap and batch 4 hit it again: every one of those primitives exists and is one import away, and reaching for them because they are there ADDS failures `cwcli apps` does not have today - a behaviour change wearing a migration's clothes.
- **Specifically preserve `_resolve_bench`'s fallback** (`apps.py:47-54`): `resolve_bench_path(...) or _DEFAULT_BENCH`, falling back to the historical default only when there is no cache to resolve against. That matches `run` and it is behaviour, not an accident.
- **`--site` is a FILTER here, not a site to resolve.** `_resolve_target_sites` de-dups explicit values or fans out over `bench_sites.list_sites` (`apps.py:154-164`). There is no default-site concept in `apps` and adding one would invent behaviour.

### 8. The `update`/`apps` "duplication" is NOT unified - they answer different questions, and the evidence says unifying would import a bug

The batch-4 recon flagged `update.py:_get_sites_with_app` (cache-first, live fallback) vs `apps.py:_list_installed_apps` (always live) as "two implementations of adjacent questions", and called unifying them a legitimate item "once `apps` migrates".
Judged on the real code: **do not unify.** Keep both.

- **First, a correction to the record.** There is no `update.py:_get_sites_with_app`. Batch 4 moved it into the core; the symbol is `core/update.py:_sites_with_app` (`:282`). The name in the recon is stale.
- **They are not adjacent questions, they are inverse ones.** `_sites_with_app` answers *app -> which sites*; `_list_installed_apps` answers *site -> which apps*. Neither is expressible as the other without a fan-out the caller does not want.
- **Their failure semantics are deliberately opposite, and unification destroys one.**
  `_sites_with_app` silently drops a site whose `list-apps` fails (`continue`, `:316`) - correct for a filter feeding a migration fan-out.
  `_list_installed_apps` returns `(ok, apps)` precisely so `list` can set `installed[site] = None` and exit 1 (Decision 5).
  Merging forces one of them to lose the semantics it was built for: either `list` stops distinguishing "no apps" from "read failed", or `update` grows an error path it does not have.
- **They parse different representations of different things - and this is the load-bearing evidence.**
  `_list_installed_apps` reads a LIVE `bench list-apps` and takes the first token per line (`apps.py:150`).
  `_sites_with_app`'s cache branch reads `installed_apps` out of `get_cached_project_data`, which returns the **raw, unparsed `bench list-apps` lines** (`db_utils.py:443`, `json.loads(site.installed_apps)`) - because `inspect._get_installed_apps` deliberately caches whole lines (`inspect.py:78`) and `db_utils.cache_project_data` is the thing that splits them into name/version/branch for `InstalledAppDetail` (`db_utils.py:404-417`, `split(maxsplit=2)` handling 1, 2 or 3 tokens).
  So the two functions are matching against different data shapes by design. A shared helper would have to know which shape it was handed, which is the abstraction earning nothing.

**The finding this turned up, reported and NOT fixed here (it is `update`'s code, not this batch's):**

`core/update.py:291` tests `if app in site.get("installed_apps", [])` - exact list membership against those raw lines.
Real captured v16 output is `frappe 16.26.3` (`docs/e2e/init-admin-password-secrets-s5.md:59`), so the cached entry is `"frappe 16.26.3"` and `"frappe" in ["frappe 16.26.3"]` is **False**.
The cache branch therefore misses whenever `bench list-apps` emits a version column, and `_sites_with_app` falls through to its live query.
`db_utils.py:405` handling `len(parts)` of 1, 2 or 3 shows the repo already expects both shapes; the cache branch only hits on the bare-name (1-token) form.

- **Severity: fail-SAFE, not a data-loss bug.** The live fallback returns the correct site set. The cost is a dead optimization and a live fan-out on every `update`.
- **Why it survived: the tests exercise the opposite branch from production.** `tests/test_apps.py:658` seeds the cache with bare names (`installed_apps={"a.localhost": ["payments"]}`), so `"payments" in ["payments"]` hits and returns early - and the live fallback (`core/update.py:296-322`) is **0% covered**, confirmed by first-hand measurement. Production takes the branch the tests never run; the tests take the branch production never reaches.
- **Why this batch does NOT fix it.** Making the cache branch hit is a behaviour change to `update`: it would start serving cached site sets where it currently performs a live read, which is a real staleness risk on the input to a migration fan-out. That is a decision on its own evidence, not a slot in a migration batch (Non-Goals; batch 4 Decision 4). Recorded in `tasks.md` §8 so it is not mistaken for forgotten.
- **The one genuinely shared line is left duplicated, deliberately.** Both take the first token per line (`apps.py:150`; `core/update.py:318`, whose `.strip()` before a no-arg `.split()` is redundant). Extracting a one-line comprehension into a shared primitive would add an import edge between two core modules to save nothing, and would be a NEW primitive this batch has claimed it does not need. Two callers at different layers parsing from different sources is not a shared concern.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class AppsListing:
    project: str
    bench_path: str
    available: list[str]
    installed: dict[str, list[str] | None]   # site -> apps, None = read failed
    ok: bool                                  # False iff any site read failed

@dataclass(frozen=True, slots=True, kw_only=True)
class AppResult:
    app: str
    site: str | None                          # None for the bench-wide get-app step
    action: str                               # "get-app" | "install-app" | "uninstall-app"
    ok: bool

@dataclass(frozen=True, slots=True, kw_only=True)
class AppsReport:
    project: str
    bench_path: str
    results: list[AppResult]
    ok: bool                                  # False iff ANY result failed
```

`AppResult` is deliberately the same four fields the hand-rolled dict already carries (`apps.py:342`), so the human `--json` shape is preserved without a translation layer.

## Risks / Trade-offs

- **`install`'s name derivation is the risk concentration.** The `apps/` before/after diff (`apps.py:337-348`) picks the installed app name, falling back to `_derive_app_name` when the diff is not exactly one new directory. Both paths are covered today; they must stay covered and unchanged.
- **The `--json` stdout-purity contract is easy to break in a migration.** In JSON mode nothing but the document may reach stdout, which is why `_run_bench` captures instead of streaming (`apps.py:117-122`). The core must never write to stdout at all; the frontend decides. `tests/test_apps.py` pins this.
- **`uninstall`'s `--json` implies non-interactive and refuses without `--yes`** (`apps.py:435-440`) rather than prompting. Preserve exactly; it is the non-interactive contract.
- **Three verbs in one batch is more surface than batch 4's one.** Mitigated by them being one fan-out with three payloads, and by `list` being a pure read that can land first.

## Open Questions

- **Whether `AppsEvent` should be one type with an `action` field or three types.** Deferred to implementation; the callback has one consumer (the CLI renderer) and the shape should follow what it actually needs.
- **Whether `axi apps list` needs `--fields`.** `AppsListing` is small; defer per foundation Decision 6's precedent.
