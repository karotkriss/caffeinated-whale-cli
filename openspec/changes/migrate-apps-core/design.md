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
- **Where test edits are expected BY DESIGN:** none currently identified. If the implementation forces one, say which changed by design and which merely moved with its subject - do not silently rewrite a test to match new behaviour and call it characterization.
- **Coverage baseline must be re-measured first-hand, not inherited.** Batch 4's tasks.md records the recon's numbers being stale by a PR; assume the same here.

### 7. Do NOT add resolvers `apps` does not use today

`core.list_apps`/`install_apps`/`uninstall_apps` resolve container + bench and nothing else.
No `resolve_default_site`, no `validate_site_name`, no `validate_bench_path`, no `require_bench_dir`.

- **Why.** This is batch 3's Decision 8 trap and batch 4 hit it again: every one of those primitives exists and is one import away, and reaching for them because they are there ADDS failures `cwcli apps` does not have today - a behaviour change wearing a migration's clothes.
- **Specifically preserve `_resolve_bench`'s fallback** (`apps.py:47-54`): `resolve_bench_path(...) or _DEFAULT_BENCH`, falling back to the historical default only when there is no cache to resolve against. That matches `run` and it is behaviour, not an accident.
- **`--site` is a FILTER here, not a site to resolve.** `_resolve_target_sites` de-dups explicit values or fans out over `bench_sites.list_sites` (`apps.py:154-164`). There is no default-site concept in `apps` and adding one would invent behaviour.

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
