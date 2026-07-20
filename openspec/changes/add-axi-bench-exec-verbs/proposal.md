## Why

**`bench migrate` and `bench run-tests` are the two commands every Frappe proof runs, and neither has an agent-facing form.**

Driving Frappe work through `cwcli axi` is currently satisfiable for reads and for a few named mutations, but every *execution* step drops to raw `cwcli run`.
Found by frappemate on 2026-07-19 doing exactly that work; it is the standing exception recorded in its own charter.

This is not only a convenience gap. It is a hole in the workflow `cwcli apps checkout` shipped for four days ago.

### The composition hole, stated precisely

The only agent-surface route to a `bench migrate` today is `cwcli axi apps update`, and that verb runs `git pull` in every named app's directory *before* it migrates (`core/update.py:641`).
So an agent that has just run `cwcli axi apps checkout myproj myapp feature/x` to pin a feature branch cannot then migrate the site without a pull that moves or conflicts with the ref it just pinned.

`add-axi-apps-checkout-verb` shipped 2026-07-20 explicitly to serve "Step 3 and Step 7 of the Frappe app delivery workflow".
The step *between* them - migrate the site against the checked-out branch - is not reachable from the agent surface at all.
The verb that was just approved does not compose into a completable workflow.

`bench run-tests` is worse: it appears nowhere in `src/` in any form.

### What exists in the code today (read, not recalled)

`bench migrate` exists in exactly two places, both of them coupled to a larger operation and neither of them standalone-invocable:

| Site | What it is |
| --- | --- |
| `core/update.py:733-748` | The per-site migrate fan-out *inside* `core.update`, after the pull and inside the maintenance-mode lifecycle |
| `core/restore.py:862` | The post-restore migrate, unconditionally part of `restore_apply` |

`bench run-tests` has no core function, no human verb, and no agent verb.
There is also no human `cwcli migrate` and no human `cwcli run-tests` (enumerated from `cwcli --help`).

**This is therefore NOT a zero-core-delta change**, unlike `add-axi-apps-checkout-verb`, which rendered an already-existing `core.checkout_app`.
That difference is real and is the largest cost in this proposal; §"Core delta" below states it plainly rather than burying it.

## The live agent surface, enumerated

From `cwcli axi --help` and `cwcli axi apps --help` on this branch, not from any document (including this brief - a stale enumeration in this fleet's own records caused real wrong work on 2026-07-19).

**Nineteen leaf verbs.** Sixteen top-level plus three under `apps`:

| Verb | Class | What it touches |
| --- | --- | --- |
| `ls`, `where`, `status`, `logs`, `benches`, `config`, `self-update`, `apps list` | read | (`inspect` is a read that writes cwcli's own cache) |
| `inspect` | read + cwcli cache write | cwcli's SQLite cache |
| `backup` | mutate | writes a backup artifact into the site |
| `unlock` | mutate | deletes a site's `locks/` folder |
| `stop` | mutate | stops the user's containers |
| `start` | mutate | starts containers, launches the bench under supervisord |
| `restart` | mutate | restarts one supervised process |
| `label` | mutate | cwcli's DB **and a marker file inside the bench** |
| `init` | mutate | provisions a whole instance, bench, and site |
| `setup` | mutate | edits the user's own `~/.claude/settings.json` / `~/.codex/` files |
| `apps update` | mutate | `bench update --pull` plus **schema migrations across live sites** |
| `apps checkout` | mutate | `git fetch` + `git checkout -B` in `apps/<app>` |

Ten of nineteen mutate.
`axi apps update` already runs `bench migrate` against every discovered site under maintenance mode - so *migrating a live site from the agent surface is an existing capability*.
What is missing is the ability to do it **without** an accompanying pull, and to say **which** site.

That framing matters: this proposal does not introduce a new class of risk to the agent surface. It introduces a *narrower* way to reach a risk that is already there, plus one genuinely new capability (`run-tests`).

## This does not reopen the passthrough deferral

`axi run` / `axi exec` are deliberately absent, asserted by `tests/test_axi.py:519` (`TestNoAxiRunVerb`), whose recorded rationale is:

> the `apps` group exists specifically to replace "dropping to the raw `cwcli run <project> bench get-app ...` escape hatch", so giving `run` an axi verb would re-open the exact escape hatch `apps` was built to close

The distinguishing property is **who authors the command string**.
`axi run` takes an unbounded command from the agent; the agent can express anything the container can run.
These two verbs take a fixed bench subcommand with typed, validated, individually-quoted parameters. There is no argument through which an agent can express `rm -rf` or a second command.

That deferral stands unchanged and this change asserts it still stands (`TestNoAxiRunVerb` is untouched and must stay green).

One live hazard worth naming: `TestNoAxiRunVerb` matches **exact** command names (`"run" not in registered`), so a verb named `run-tests` does not trip it. But it sits one hyphen from an asserted-absent verb, which is a readability hazard for an agent skimming `--help`. That feeds the naming decision below rather than deciding it.

## The captain's decision: shape and naming

Three options, each with its cost stated. **Recommendation is Option A**, argued rather than asserted; the full argument is `design.md` Decision 1.

### Option A - two top-level verbs: `cwcli axi migrate`, `cwcli axi run-tests`

- **Cost:** the top-level surface grows one verb per bench command that ever matters. Today that is two, taking the top-level list from 16 to 18. `--help` is the agent's discovery surface (AXI §8/§10) and every line costs tokens on every read.
- **Benefit:** each verb is discoverable at the top level where every other project-scoped operation already lives (`backup`, `unlock`, `restart`, `logs`), and each addition is an explicit captain decision rather than an implicit fit into a container that already exists.

### Option B - one narrow group: `cwcli axi bench <migrate|run-tests>`

- **Cost:** it re-raises "where does the grouping stop", which is the same slope the passthrough deferral was avoiding. And a group named `bench` groups by *mechanism* ("things implemented as bench subcommands"), which has no natural boundary - every bench subcommand qualifies, so the group's completion state is "passthrough, by accretion". Contrast the one group that exists: `apps` groups by *domain noun*, and its boundary is legible (operations on apps).
- **Benefit:** the top-level list stays at 16 no matter how many bench commands are eventually served, and a group signals "these are related" better than two unrelated top-level entries do.

### Option C - the split: `cwcli axi migrate` top-level, `run-tests` deferred entirely

- **Cost:** leaves half the reported gap open; frappemate still drops to raw `cwcli run` for its test step.
- **Benefit:** `migrate` and `run-tests` are not actually the same kind of thing (see the safety section - one is a bounded state mutation, the other is unbounded code execution), and pairing them in one decision invites approving the harder one on the easier one's evidence. That is precisely the inherited-rationale failure `add-axi-apps-checkout-verb` had to unwind.

**Recommended: Option A**, with the two verbs judged on their own separate evidence inside one change (safety sections below are deliberately separate, so the captain can approve one and decline the other without re-opening the change).

Option C's concern is real and is answered by *separating the evidence*, not by separating the change. If the captain approves `migrate` and declines `run-tests`, the tasks file supports shipping exactly that.

## Safety posture: `axi migrate`

**Blast radius, named:** `bench --site <site> migrate` runs every pending schema patch from every installed app against the site's live database. It ALTERs tables and executes arbitrary patch code shipped by those apps. It is not transactional across patches: a patch that fails partway leaves the database partially migrated, and cwcli has no rollback. The honest statement is **irreversible schema change to a live site, recoverable only from a backup**.

That is not a new risk to the surface - `axi apps update` already does exactly this across every discovered site - but it is the risk, and it must be written where a reader will find it.

| Guard | Threat it protects against | Where it comes from |
| --- | --- | --- |
| **Exactly ONE site per invocation**: `--site` explicit, else the bench's default site. No implicit fan-out, ever. | An agent migrating sites it never named, because `apps update` taught it that a migrate discovers its own targets. | New. This is the single most important difference from `apps update`. |
| **The resolved site and bench path are in the report.** | An agent unable to confirm *what* it migrated (the known gap `apps checkout` deliberately left; not repeated here). | New, cheap, and specified. |
| **Maintenance mode is enabled first, and a failed enable REFUSES the migrate.** Disabled in a `finally`. | Migrating a site that is still serving live traffic. | Existing and load-bearing: `core/update.py:724-727` calls this "THE LOAD-BEARING GATE" and refuses to migrate a site it could not put into maintenance. A standalone verb that skipped it would be **less safe than the path that already exists**. |
| **A site left in maintenance mode is reported** (`maintenance_left_on`), and makes the verb exit non-zero. | A site silently left down after a failed disable. | `core.update` already tracks `failed_maintenance_disable`; this surfaces it per-site. |
| **No `--yes`, no auto-start.** A stopped project is exit 2 naming `cwcli start`. | An agent starting containers a user deliberately stopped. | Every bench-scoped axi verb already refuses this. |
| **Multi-bench with no `--bench` is exit 2.** | Migrating a site in a bench the agent did not name. | The existing `select_bench` `NEEDS_CHOICE` shape. |
| **migrate's own stderr is forwarded to stderr.** | A failed patch reported as a bare `ok: false`, with the traceback that names the failing patch discarded. | `_checkout_narrate`'s stated reasoning (`commands/axi.py:998-1016`), which applies verbatim: a migrate is not a supervised process and logs nowhere afterwards. |

**Guards deliberately NOT proposed**, because a guard with no named threat is noise:

- **No mandatory pre-migrate backup.** It sounds prudent and is not cwcli's shape: `axi apps update` migrates without one, `rm`'s backup gate exists because `rm` *deletes* and has no other recovery. An agent that wants a backup composes `cwcli axi backup` first, which is a verb that already exists and is the contextual-disclosure hint this verb should emit (AXI §9).
- **No consent parameter in the core.** `core.uninstall_apps` takes `consent` because it drops tables. A migrate applies patches the app authors intend to be applied. Adding a consent axis would model a threat that does not exist and imply to a future reader that it does.
- **No refusal when there is nothing to migrate.** A no-op migrate is a successful migrate, exit 0 (AXI §6, idempotent mutations).

## Safety posture: `axi run-tests`

**This is a different class of risk from `migrate` and must be judged separately.**

**Blast radius, named:** `bench --site <site> run-tests --app <app>` imports and executes the app's own test modules inside the container, against the named site's live database. cwcli cannot bound what that code does, because it *is* the repository's code: a test suite can create, modify, and delete records, and Frappe test fixtures routinely write to the site database.

So the honest statement is: **arbitrary Python from the repository under test, executed against a live site.** This is the closest thing to arbitrary code execution that would exist on the agent surface.

**Why that is nevertheless not the passthrough deferral**: the agent does not *author* the code. It selects an app whose tests already exist in the bench, put there by a human's `apps install` or `apps checkout`. The threat model is "an agent runs a test suite against a site holding real data", not "an agent injects a command". Those need different guards, and the passthrough deferral's guard (refuse to accept a command string) does not fit.

| Guard | Threat it protects against | Note |
| --- | --- | --- |
| **`--site` is REQUIRED. No default-site fallback.** | An agent running a destructive suite against whatever site happened to be the bench default, having never named it. | **A deliberate divergence** from the `--site`-defaults-to-default-site convention that `backup`, `unlock`, and (as proposed) `migrate` all follow. The reason: for those three, cwcli knows exactly what the operation does; here it cannot know. Making the agent name the site is the cheapest possible way to make "I am willing for this site to be written to" an explicit act. Captain's call - `design.md` Decision 4. |
| **`--app` is REQUIRED.** | A bare `bench run-tests` running every installed app's suite, including `frappe`'s own, against that site. | The scope must be named, not defaulted. |
| **The resolved site, bench, and app are in the report.** | An agent unable to confirm what it ran and where. | Same reasoning as `migrate`. |
| **No `--yes`, no auto-start.** Stopped project is exit 2 naming `cwcli start`. | An agent starting containers a user deliberately stopped. | Surface-wide convention. |
| **Test output is forwarded to stderr in full.** | A failing suite reported as a bare `ok: false`, with the assertion that failed discarded. cwcli must not summarize or parse it. | `_checkout_narrate`'s reasoning again. |
| **The report carries the honest exit code, never a guess.** | A lost exec stream reading as a passing suite - the exact fail-open the `exec_stream` contract was built to close. | Inherited: `core/exec_stream.py` types `ExecDone.exit_code` as `int` and raises `DOCKER` on a genuinely unknown code. |
| **The help text names the practice of using a dedicated test site.** | An agent with no other signal defaulting to the site a human actually works in. | Documentation, not enforcement - cwcli cannot tell a test site from a real one. Stated as such, not dressed up as a guard. |

**Guards deliberately NOT proposed:**

- **No test-name or module allowlist.** `--module` / `--test` narrow the run; they add no capability, and an allowlist would reject legitimate targets for nothing.
- **No timeout.** A long test run is a long test run, not a threat. `axi apps update` already blocks for schema migrations across every site, and cwcli has no timeout precedent anywhere. If the captain wants one as an *ergonomics* measure rather than a safety one, it is cheap to add - but it should not be smuggled in as a guard.
- **No refusal to run against a site with data.** cwcli cannot tell a seeded test site from a production-ish one, and a check that cannot distinguish them would be theatre.

## Core delta (the honest cost)

`add-axi-apps-checkout-verb` held a falsifiable "zero `core/` changes" claim. **This change cannot**, and pretending otherwise would be the bigger sin.

Recommended: ONE new module `src/caffeinated_whale_cli/core/bench_ops.py` holding both functions, reusing without modification the `_resolve` shape from `core/apps.py:155`, the `exec_stream` contract, the `Result` / `CwcliError` envelope, and the existing `select_bench` / `confirm_start` choice kinds.

**One primitive-bending signal, reported rather than absorbed** (the repo's own standard): `_set_maintenance` is private to `core/update.py:273` and this change gives it a second caller. The right move is to promote it to a shared core helper, NOT to copy it - a second copy of the maintenance-mode lifecycle is exactly how the two drift and one of them stops disabling. Flagged here so the captain sees it before implementation, not in a review comment afterwards.

## What Changes

- **ADD `core/bench_ops.py`**: `migrate_site(...) -> Result[BenchOpReport]` and `run_tests(...) -> Result[BenchOpReport]`, UI-pure, on the exec-stream contract.
- **PROMOTE `_set_maintenance`** from `core/update.py`-private to a shared core helper, with `core.update` re-pointed at it and its behaviour unchanged.
- **ADD the two `cwcli axi` verbs** (naming per the captain's Option A/B/C ruling) as thin TOON renderers: one TOON document on stdout, all narration on stderr, never prompting, exit code from `report.ok`.
- **NO human `cwcli migrate` / `cwcli run-tests` in this change** - deferred and filed as tasks, not forgotten (`design.md` Decision 6). This makes them axi-first verbs, which the captain should see stated.
- **`TestNoAxiRunVerb` is untouched and must stay green**, with a new test asserting these two verbs do not introduce any command-string parameter.
- **Regenerate the installable skill** - `scripts/build_skill.py` walks the live Typer registry, so no hand-edit; `tests/test_axi_skill.py`'s `--check` unit test keeps it honest.

## Impact

- **New:** `core/bench_ops.py`, two verbs in `commands/axi.py`, `tests/test_core_bench_ops.py`, `tests/test_axi_bench_ops.py`, a real-instance E2E leg.
- **Changed (implementation phase only):** `commands/axi.py`, `core/update.py` (the `_set_maintenance` promotion only), `skills/cwcli/SKILL.md` (regenerated), `CLAUDE.md`, the `cwcli-core-axi` and `cwcli-apps-update` skills, `README.md`'s agent-surface section, `tests/README.md`.
- **Unchanged:** `core/apps.py`, `core/restore.py`, `core/exec_stream.py`, every human CLI verb, `AppsReport`'s shape, the credential bridge, the cache schema, and the `axi run` / `axi exec` absence.

## Non-goals

- **NO `axi run` / `axi exec`.** The deferral stands, and this change strengthens rather than erodes it by closing the two concrete needs that were driving people to the escape hatch.
- **NO general `bench` passthrough** under any naming, including Option B's group. Option B, if chosen, ships with exactly these two subcommands and a stated boundary rule.
- **NO third bench command** in this change (`bench build`, `bench backup`, `bench console`, ...). Each is its own decision on its own evidence.
- **NO human verbs** in this change (`design.md` Decision 6).
- **NO `axi apps install` / `uninstall` / `restore` / `rm`.** Their absence assertions are untouched.
