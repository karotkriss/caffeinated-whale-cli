# Design: narrow agent verbs for `bench migrate` and `bench run-tests`

Written as Phase A only. No code exists, deliberately, so the naming and safety arguments get read before an implementation biases the review - the sequencing `add-axi-apps-checkout-verb` used and the captain approved.

Every claim below was checked against the live surface (`cwcli axi --help`, `cwcli axi apps --help`) and the source on this branch, not against any document. A stale enumeration in this fleet's own records caused real wrong work on 2026-07-19; that is why.

---

## Decision 1 - Shape and naming (the captain's call)

**Recommendation: Option A, two top-level verbs `cwcli axi migrate` and `cwcli axi run-tests`.**

### The three options and what each costs

| | A: two top-level verbs | B: `axi bench <sub>` group | C: `migrate` only, defer `run-tests` |
| --- | --- | --- | --- |
| Top-level surface | 16 -> 18 | stays 16 | 16 -> 17 |
| Boundary rule | "each new verb is its own captain decision" | must be written, and no natural one exists | n/a |
| Slope risk | grows linearly, visibly, one approval at a time | accretes invisibly inside an existing container | none |
| Closes the reported gap | fully | fully | half |

### Why A over B

The argument against B is not "groups are bad" - `apps` is a group and it works. It is that **`apps` groups by domain noun and `bench` would group by mechanism**, and only the first has a boundary anyone can state.

`apps`'s boundary is legible: operations on Frappe apps. A reader can tell whether a proposed verb belongs. It is why `apps checkout` fit the group without argument while `apps install` remained a separate, still-open decision - membership in the group never implied approval.

A group named `bench` means "things implemented as a `bench` subcommand". Every bench subcommand qualifies by construction. Its completion state is a full passthrough, reached by accretion instead of by decision - which is the same destination `TestNoAxiRunVerb` refuses to reach by flag. The group would not be *arbitrary* passthrough (each subcommand still gets typed parameters), but the pressure to add the next one would come from the container's existence rather than from evidence, and that is the failure mode this repo has spent two changes unwinding.

Option A's cost is real and should not be minimized: `--help` is the agent's primary discovery surface (AXI §8, §10), it is read on every orientation, and eighteen entries is more to read than sixteen. The trade is that each of those entries got there by an explicit decision.

There is also a live asymmetry that argues against grouping these two *specifically*: `migrate` and `run-tests` are not the same kind of operation. `migrate` is a bounded state mutation whose effect cwcli can describe exactly. `run-tests` is unbounded execution of the repository's own code. Putting them in one group asserts a kinship that the safety analysis (Decisions 3 and 4) shows does not exist - and would invite a future reader to approve one on the other's evidence, which is the precise inherited-rationale error `add-axi-apps-checkout-verb` had to spend a whole proposal unwinding.

### Why A over C

C's concern is correct and its remedy is wrong. The two verbs *should* be judged on separate evidence - so this change presents them with **separate safety sections and separate approval checkboxes** (`tasks.md` §0), not with separate changes. The captain can approve `migrate`, decline `run-tests`, and the tasks file supports shipping exactly that outcome. Splitting into two changes would buy the same separation at the cost of two review cycles and a second enumeration of the same surface.

### Sub-decision: is `run-tests` the right name?

Recommended: **`run-tests`**, matching the bench subcommand exactly, on the same principle that makes `axi apps update` match `bench update`. An agent that knows Frappe should not have to translate.

The alternative is `axi test`, shorter and with no `run` prefix at all.

The hazard against `run-tests`: it sits one hyphen from the asserted-absent `axi run`, so an agent skimming `--help` could misread the surface as offering a passthrough. This is a *cosmetic* hazard, not a functional one - `TestNoAxiRunVerb` compares exact names (`tests/test_axi.py:527-535`), so `run-tests` does not weaken the assertion, and `--help`'s one-line description resolves the ambiguity immediately.

Recommended resolution: keep `run-tests`, and make the assertion explicit in the test's docstring so the next reader does not think the absence eroded.

### If the captain picks B

Everything else in this design holds unchanged: the same two operations, the same core module, the same guards, the same output shape. Only the Typer registration and the verb strings change. B additionally requires a **written boundary rule** for the group (what is in scope, what needs its own decision) recorded in the group's docstring, since B's whole cost is that the boundary is otherwise undefined.

---

## Decision 2 - Core delta, and the one primitive-bending signal

**Recommendation: one new module `core/bench_ops.py`, plus promoting `_set_maintenance` out of `core/update.py`.**

This change cannot hold the "zero `core/` changes" claim `add-axi-apps-checkout-verb` held, because there is nothing to render: `bench migrate` exists only inside `core.update`'s pull-and-fan-out state machine (`core/update.py:733-748`) and inside `restore_apply`'s post-restore step (`core/restore.py:862`), and `bench run-tests` does not exist in `src/` at all. Stating that up front is the point; a proposal that implied otherwise would be discovered wrong during implementation, which is the expensive time to discover it.

### Why one module rather than two

`migrate_site` and `run_tests` share their entire resolve (project -> container -> bench -> site), their execution mechanism (`exec_stream`), their report shape, and their choice surfaces. Two modules would duplicate all of it to express a distinction that only exists in the safety analysis, not in the code. One module, two public functions.

### Why not reuse `AppsReport`

`AppResult` is `(app, site, action, ok)` and is documented as deliberately frozen to the shape the human `apps --json` output already carried (`core/apps.py:65-77`). A migrate has no app, so `app` would carry a placeholder - a field that lies. A new `BenchOpReport` / `BenchOpResult` pair is a handful of lines and keeps the mutation DTO shared by install/uninstall/update/checkout untouched, which was `add-axi-apps-checkout-verb` Decision 3's binding ruling (do not bolt fields onto that shared report).

### The reported signal: `_set_maintenance`

`_set_maintenance` is private to `core/update.py:273` and this change gives it a second caller. Per this repo's own standard - *needing to bend a primitive is a signal worth reporting, not absorbing* - it is reported here rather than resolved silently.

**Recommendation: promote it to a shared core helper and re-point `core.update` at it, with its behaviour byte-identical.** The alternative, a second copy inside `bench_ops`, is how the two drift until one of them stops disabling maintenance mode - and a site stuck in maintenance is a site that is down.

This is the only core change outside the new module, and it is a move, not a rewrite.

---

## Decision 3 - `axi migrate`'s scope: exactly one site, never a fan-out

**Recommendation: resolve exactly ONE site per invocation - explicit `--site`, else the bench's default site - and never fan out.**

This is the single most important behavioural difference from the capability that already exists.

`axi apps update` migrates *every site it discovers* to have the named app installed (`core/update.py:733`). That is correct for `apps update`, because the app is the subject and the affected sites are derived from it. It is wrong for a standalone migrate: the subject is the site, and an agent that ran `axi migrate myproj` should never discover it migrated four sites.

Following the surface convention (`--site` / `-s`, defaulting to the bench's default site, exactly as `axi backup` and `axi unlock` do) gives one site by construction, with no new flag semantics to learn.

**The resolved site MUST appear in the report.** This closes, for these verbs, the gap `apps checkout` deliberately left open - an agent that cannot read back what it acted on cannot verify its own work. It costs one field and there is no reason to repeat the gap when the fix is this cheap.

### Maintenance mode is not optional here

`core/update.py:724-727` calls the maintenance-mode enable **"THE LOAD-BEARING GATE"** and refuses to migrate any site it could not put into maintenance. A standalone `axi migrate` that skipped it would be *less safe than the path that already exists* - the agent would reach the same schema mutation with strictly fewer protections, which is an indefensible outcome for a change whose stated purpose is a narrower, safer route.

So: enable, migrate, disable in a `finally`; a failed enable refuses the migrate and returns a typed error; a failed disable is reported as `maintenance_left_on` and makes the verb exit non-zero, because a site left in maintenance is a site that is down and the agent must know.

Open sub-question the captain may want to rule on: whether to offer a `--skip-maintenance` escape hatch mirroring `apps update`'s. **Recommendation: no.** `apps update` has it for long multi-site runs where the operator accepted the trade knowingly. An agent-surface flag that removes the load-bearing gate has no named beneficiary, and the flag's existence invites its use.

---

## Decision 4 - `axi run-tests` requires `--site` explicitly (a deliberate divergence)

**Recommendation: `--site` and `--app` are both REQUIRED. No default-site fallback.**

This diverges from the convention `axi backup`, `axi unlock`, and (per Decision 3) `axi migrate` all follow, so it needs its own reason rather than an assertion.

The reason is the difference in what cwcli knows. For `backup`, `unlock`, and `migrate`, cwcli can state exactly what the operation does to the site: write an artifact, delete a locks folder, apply pending patches. For `run-tests`, cwcli is executing the repository's own test modules and cannot bound their effects - a Frappe test suite creates, modifies, and deletes records against the site it runs on.

When the operation's effect is unbounded, defaulting the target is the wrong default. Requiring `--site` makes "I am willing for this site to be written to" an explicit act by the agent, at a cost of exactly one flag.

`--app` is required for a parallel reason: a bare `bench run-tests` runs every installed app's suite, including `frappe`'s own, against that site. The scope must be named.

**If the captain prefers convention-consistency over this divergence**, the fallback is `--site` defaulting to the bench default *plus* a mandatory `--app`, and the report echoing the resolved site. That is a coherent position; it just accepts that the most consequential verb on the surface has the same target-resolution ergonomics as the least. Stated so the choice is visible.

---

## Decision 5 - Output, narration, and exit codes

**Recommendation: identical to `axi apps checkout`, which is the surface's settled shape.**

- **stdout**: exactly ONE TOON document, `emit_result(report, warnings=result.warnings)`. Nothing else, ever.
- **stderr**: everything else - phase narration, the `$ bench ...` echo, and **the command's own bytes**.
- **exit code**: `0 if report.ok else 1`, read from the report and **never** from `result.status`. This is batch 4's trap and it is live here for the same reason: a partial failure is a `WARNING`-shaped envelope, and `WARNING` maps to exit 0 everywhere else on the surface, so a status-driven code would report success for a migrate that failed.
- `NEEDS_CHOICE` (`confirm_start`, `select_bench`) -> `emit_axi_choice_as_usage_error` + exit 2.
- A raised `CwcliError` -> `emit_axi_error` + `exit_for(error.kind)`.

### Forwarding the command's own output is load-bearing

`_checkout_narrate` (`commands/axi.py:998-1016`) forwards git's own bytes to stderr where `_init_narrate` deliberately does not, and its recorded reasoning transfers verbatim to both verbs here:

> init's raw output is thousands of lines of bench build noise an agent will not parse, and `cwcli logs` exists to serve it afterwards. [...] nothing is logged anywhere afterwards (a git step is not a supervised process), and the ENTIRE reason a step failed lives in those bytes.

Neither a migrate nor a test run is a supervised process, so neither writes to a log `cwcli logs` can serve afterwards. The name of the patch that blew up, and the assertion that failed, exist **only** in those bytes. Dropping them leaves a failure reported as a bare `ok: false` and an agent with no next move.

cwcli must forward them **unparsed**. Summarizing a test suite's output into pass/fail counts would be cwcli guessing at a format it does not own, and the guess would be wrong the first time a suite used a different runner.

### Contextual disclosure (AXI §9)

On a failed migrate, the help line should point at `cwcli axi logs` and at `cwcli axi backup` (as the thing to have done first). On a successful migrate, no help line - the output fully answers the query, and AXI §9 says suggestions are noise there.

---

## Decision 6 - No human `cwcli migrate` / `cwcli run-tests` in this change

**Recommendation: axi verbs only. File the human verbs as a separate decision.**

Once `core/bench_ops.py` exists, human verbs are nearly free renderers over it - which is exactly why they do not need to ride along. They carry their own UX design (prompts, spinners, `--json`, confirmation shape) that has nothing to do with the reported gap, and bundling them doubles the review surface for a change whose whole argument is about the agent surface.

**The cost, stated plainly:** these become the first cwcli verbs whose logic is reachable only from `cwcli axi`. That does not violate the architecture - the core is still the single implementation and a human verb can be added later without touching it - but a human cannot migrate a site with cwcli, which is arguably its own gap.

`axi setup` is the nearest precedent for an axi-only verb, and it is a weak one: installing agent session hooks is inherently agent-scoped, whereas migrating a site plainly is not. Cited honestly rather than leaned on.

If the captain prefers the human verbs in the same change, the cost is a larger diff and a second UX review, not a design conflict.

---

## Decision 7 - Guards deliberately NOT built

Recorded as decisions so a later reader can tell "deliberately not built" from "forgotten". A guard with no named threat is noise; each of these was considered and rejected for a stated reason.

| Not built | Why |
| --- | --- |
| Mandatory pre-migrate backup | Not cwcli's shape: `axi apps update` migrates without one. `rm`'s backup gate exists because `rm` deletes and has no other recovery. An agent composes `cwcli axi backup` first, and the verb's help says so. |
| A `consent` parameter in the core | `core.uninstall_apps` takes one because it drops tables. A migrate applies patches the app authors intend applied. A consent axis here models a threat that does not exist and implies to a future reader that it does. |
| A test timeout | A long test run is a long run, not a threat. cwcli has no timeout precedent anywhere, and `axi apps update` already blocks for cross-site schema migrations. If wanted as *ergonomics*, add it as ergonomics - do not smuggle it in as a guard. |
| Refusing to run tests against a site "with data" | cwcli cannot distinguish a seeded test site from a production-ish one. A check that cannot tell them apart is theatre. |
| A test-name / module allowlist | `--module` / `--test` narrow the run and add no capability. An allowlist would reject legitimate targets for nothing. |
| A cwcli-side pre-check for pending patches | `bench migrate` with nothing pending is a clean no-op, exit 0 (AXI §6, idempotent mutations). A pre-check would drift from bench's own answer. |
| A post-migrate recache epilogue | Unlike a checkout, a migrate does not change an app's version or git state, which is what the cache holds. `apps update` recaches because it *pulled*. Adding one here would be cargo-culting `apps checkout`'s epilogue past its reason. **Worth a second look during implementation** if a migrate turns out to change anything `inspect` reports. |

---

## Alternatives considered

### Add `--no-pull` to `axi apps update`

Genuinely the smallest diff, and it deserves to be visibly considered rather than ignored: one flag, no new verb, no new core module, and it would unblock the migrate-after-checkout composition today.

Rejected on three counts. It makes the verb's name a lie ("update" that updates nothing). It keeps the implicit multi-site fan-out, which Decision 3 identifies as the specific thing a standalone migrate must not do - so it would deliver the capability with the wrong safety property. And it does nothing at all for `run-tests`, leaving half the reported gap open.

### `axi run` with a command allowlist

Rejected. It is the passthrough with a config file. The allowlist immediately becomes the grouping-boundary problem in a worse form - now the boundary lives in data rather than in code review - and it re-opens exactly what `TestNoAxiRunVerb` closed.

### Fold migrate into `axi apps checkout` as a `--migrate` flag

Rejected. It couples two operations whose failure modes are unrelated, and it makes a schema mutation a *flag* on a git operation - the least visible possible place to put the surface's most consequential mutation. It also does not serve a migrate that follows anything other than a checkout, which is most of them.

### Ship `run-tests` only, defer `migrate`

The inverse of Option C, considered for symmetry and rejected: `migrate` is the one with an existing, more dangerous agent-surface route (`apps update` migrates everything it discovers). Serving the narrower path first is strictly the safer sequencing.
