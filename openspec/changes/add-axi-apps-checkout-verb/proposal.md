## Why

**Putting a feature branch under test inside the instance that holds the app has no agent-facing form.**

`cwcli apps checkout <project> <app> <ref> [--reset]` exists as a HUMAN verb only.
It fills the gap `install` (a fresh `bench get-app` clone) and `update` (`bench update --pull` on the tracked upstream) leave: fetching one named branch, tag, or commit into the app directory that already exists at `apps/<app>`.
That is Step 3 and Step 7 of the Frappe app delivery workflow it was built for (`frappe-workflow-impl-w4`), and it is the single most common thing anyone does when testing an app change against a real bench.

Verified against this branch's live registry, not against prose: `cwcli axi apps --help` lists exactly `list` and `update`.
An agent driving a delivery workflow can update every app to its tracked upstream, but it cannot put ONE branch under test.
It cannot compose its way there either, because there is deliberately no `axi run` / `axi exec` passthrough (`tests/test_axi.py::TestNoAxiRunVerb`), so no arbitrary in-container `git` is reachable from the agent surface.

Found by frappemate on 2026-07-19 while doing exactly that work.

## The recorded deferral, and why it does not decide this

`apps checkout`'s absence is pinned by a test (`tests/test_axi_apps_list.py:146-148`):

> `apps checkout` mutates the in-instance checkout, so like install/uninstall it is a HUMAN verb only - no axi surface. Keep its absence a decision.

That is an INHERITED rationale, not one taken on checkout's own evidence, and the thing it inherits from is narrower than the sentence implies.
Two facts weaken it, and this proposal engages both rather than asserting them.

### 1. The install/uninstall rationale is specifically about destroying site data, and checkout destroys none

The captain-locked deferral says so in its own words (`openspec/changes/migrate-apps-core/tasks.md` §7.1):

> **`cwcli axi apps uninstall`** - deferred. Needs its own decision, on its own evidence: it would let an agent destroy site data (`bench uninstall-app` drops the app's tables).

And §7.2, on install:

> It mutates a real site but does not delete data, so it may well be decided differently; it was held only because there was no reason to settle half the question inside a refactor.

So the recorded rationale is *"an agent must not drop a site's tables"*, plus an explicit note that a non-deleting mutation *"may well be decided differently"*.
`core.checkout_app` (`core/apps.py:477-546`) runs exactly `git fetch <remote> -- <ref>` then `git checkout -B <ref> FETCH_HEAD`, with an optional `git reset --hard FETCH_HEAD`, all with `workdir=apps/<app>`.
It runs no `bench` command, touches no site, issues no SQL, and drops no table.
The rationale that was locked does not reach it.

The absence test's phrase "mutates the in-instance checkout" is true and is not the same claim.
This proposal takes checkout's own evidence, which is what §7.1 asked for.

### 2. The premise that the agent surface is read-only is already false

Enumerated from the live `cwcli axi --help` on this branch, not from memory:

| Verb | Mutates? | What it does to real state |
| --- | --- | --- |
| `ls`, `where`, `status`, `logs`, `benches`, `config`, `self-update`, `apps list` | read | (`inspect` is a read that writes cwcli's own cache) |
| `backup` | yes | writes a backup artifact into the site |
| `unlock` | yes | deletes a site's `locks/` folder |
| `stop` | yes | stops the user's containers |
| `start` | yes | starts containers and launches the bench under supervisord |
| `restart` | yes | restarts a supervised process |
| `label` | yes | writes cwcli's DB **and a marker file inside the bench** |
| `init` | yes | provisions a whole instance, bench, and site |
| `setup` | yes | edits the user's own `~/.claude/settings.json` / `~/.codex/` files |
| `apps update` | yes | `bench update --pull` plus **schema migrations across live sites** |

Nine of the eighteen verbs mutate.
`cwcli axi init` creates an entire instance.
`cwcli axi apps update` runs Frappe schema migrations against live sites under maintenance mode, which is materially more consequential than a `git fetch` into a source directory.

So "agents get reads only" cannot be the reason to withhold checkout, because it is not a rule this codebase follows.
It was never written as one either: the actual rule is the narrow, evidence-scoped one in §7.1.

There is a live precedent for exactly this move.
`axi init` was deferred, pinned by a registry-absence test, and shipped once decided on its own evidence (`add-axi-init-verb`; CLAUDE.md records that "a deferral is not permanent").
This change asks for the same treatment.

### Where this lands, and the honest counter

**Recommendation: build `cwcli axi apps checkout`.**

The counter that deserves stating rather than waving away: a checkout is not risk-free.
It changes the source a live site will run at the next build, migrate, or process restart, and `--reset` discards uncommitted work in the container's app directory.
The proposal's answer is not "it is harmless"; it is that each of those risks has a named, bounded guard (below), none of them is data deletion, and the surface already carries strictly larger mutations.

If the captain disagrees, the useful outcome is a *stated* rule for what the agent surface may mutate.
The current situation, where an inherited one-line comment on a test blocks a verb that its own source rationale does not cover, is the failure mode this fleet spent 2026-07-19 unwinding.

## Safety posture (each guard names its threat)

| Guard | Threat it protects against | Where it comes from |
| --- | --- | --- |
| **No `--yes` / no auto-start.** A stopped project is a usage error (exit 2) naming `cwcli start`. | An agent silently starting containers a user deliberately stopped. | Every bench-scoped `axi` verb already refuses this (`axi inspect`'s stated rule); `core.checkout_app(auto_start=False)` returns `confirm_start`. |
| **The app must already exist as a git checkout.** `git remote` failing raises `NOT_FOUND` / `app.no_checkout`. | A typo'd or absent app name reading as a silent no-op, or as an implicit install. | Already in `core/apps.py:447-474`. No new code. |
| **A dirty working tree refuses the checkout unless `--reset` is passed.** | Silently discarding a human's uncommitted edits inside `apps/<app>`. | **git's own guard**, probed and confirmed: `git checkout -B <ref> FETCH_HEAD` over a conflicting local edit exits 1 with "Your local changes ... would be overwritten", leaving the file intact. cwcli adds nothing; the verb's job is to SURFACE it as a failed `checkout` step and a non-zero exit, not to build a redundant pre-check. |
| **`--reset` stays an explicit opt-in flag and is reported in the outcome.** | Irrecoverable loss of uncommitted work in the container's app directory. | The one genuinely destructive element. Kept, not omitted: it is the clean-tree guarantee the delivery workflow's Step 7 exists for, and dropping it from `axi` would leave an agent unable to complete the workflow while giving it a way to fail halfway. |
| **No new credential exposure.** The private-repo fetch rides the existing `core/credbridge.py` bridge unchanged. | An agent reaching host credentials it did not have before. | Not new: `axi apps update` already wraps its whole dispatch in the same bridge, so the agent surface already borrows the host's `gh`/`glab` auth. Named here so it is a known property rather than a surprise. The raw token still never enters the container. |

**Guards deliberately NOT proposed**, because a guard with no named threat is noise:

- **No "app must be installed on a site" precondition.** Checkout targets `apps/<app>`; an app present in the bench but not yet installed on any site is a legitimate target, and the check would reject it for nothing.
- **No ref allowlist and no refusal of tags or commit shas.** Fetching a sha is not more dangerous than fetching a branch, and a `bench get-app` remote is not more trustworthy than an arbitrary ref within it.
- **No destructive-consent parameter in the core.** `core.uninstall_apps` takes `consent` because it drops tables. Checkout drops nothing, and adding a consent axis would model a threat that does not exist while implying to a future reader that it does.

## What Changes

- **ADD the `cwcli axi apps checkout <project> <app> <ref>` verb** to `commands/axi.py`: a thin TOON renderer over the UNCHANGED `core.checkout_app`, with flags `--bench <index|label>` and `--reset`.
  It emits ONE TOON `AppsReport` document on stdout, narrates the git steps to stderr, and never prompts.
- **ZERO `core/` changes.** `core.checkout_app` already returns `Result[AppsReport]`, already resolves `bench` / `auto_start` itself, and already returns `confirm_start` / `select_bench` as `NEEDS_CHOICE`. The verb wires the existing `emit_result` / `emit_axi_error` / `emit_axi_choice_as_usage_error` machinery.
- **A post-mutation recache epilogue**, gated on `any(r.ok for r in report.results)` exactly as the human verb gates it (`commands/apps.py:472-475`), via `cache.recache_project` with a stderr warning on failure. This is `axi init`'s existing pattern (`commands/axi.py:1249-1255`), not a new seam: a checkout changes the app's git state and reported version, so leaving the cache stale would make the next `axi apps list` / `axi inspect` lie.
- **FLIP the guard.** `tests/test_axi_apps_list.py:148`'s `assert "checkout" not in registered` becomes a presence assertion with an updated docstring recording this decision. `install` and `uninstall` stay asserted absent, unchanged.
- **Regenerate the installable skill.** `scripts/build_skill.py` walks the live Typer registry, so `axi apps checkout` appears with no hand-edit; `tests/test_axi_skill.py`'s `--check` unit test keeps it honest. The `axi apps install` / `axi apps uninstall` / `axi restore` absent-verb parametrization is untouched.

## Design questions resolved (recommendations in `design.md`)

1. **Should the verb exist at all** - yes, on checkout's own evidence, engaging the recorded deferral above (Decision 1).
2. **Flag set** - `--bench` only (no `--path`, which is the human verb's lower-level alternative and has no axi precedent), plus `--reset`. No `--yes`, no `--verbose`, no `--json` (stdout is always TOON) (Decision 2).
3. **Output shape and the resulting-commit gap** - reuse `AppsReport` verbatim via `emit_result`, matching `axi apps update`. An agent cannot currently confirm which commit it landed on through the agent surface; that gap is real and is recommended DEFERRED to a read verb (per-app git state on `axi apps list`), not bolted onto the shared mutation DTO (Decision 3).
4. **Exit codes** - 0/1 read from `report.ok`, never `result.status`; 2 for any `NEEDS_CHOICE`; `exit_for(kind)` for a raised `CwcliError` (Decision 4).

## Impact

- **New:** `axi_apps_checkout` in `src/caffeinated_whale_cli/commands/axi.py`, plus `tests/test_axi_apps_checkout.py`, plus a real-instance E2E leg.
- **Changed (implementation phase only):** `commands/axi.py`, `tests/test_axi_apps_list.py` (the absence assertion flips to presence), `skills/cwcli/SKILL.md` (regenerated), `CLAUDE.md`'s `apps checkout` entry, the `cwcli-apps-update` and `cwcli-core-axi` skills, `README.md`'s agent-surface section, `tests/README.md`.
- **Unchanged:** every `core/` file, the human `cwcli apps checkout` and its `--json` / `--path` / `--yes` / `--verbose` behaviour, `AppsReport`'s shape, the credential bridge, the cache schema.
- **Zero new core primitives, as a falsifiable claim:** the verb reuses `core.checkout_app`, the `Result` envelope, `emit_result`, `emit_axi_error`, `emit_axi_choice_as_usage_error`, the `OnEvent` idiom, and `cache.recache_project`. No new `Choice.kind` token and no new `ErrorKind`.

## Non-goals

- **NO `axi apps install`.** The sibling gap is filed separately as `cwcli-axi-apps-install-verb` and is a different decision on different evidence (it clones a NEW app into the bench and installs it onto sites). This change proposes ONE verb and leaves the `install` / `uninstall` absence assertions exactly as they are.
- **NO change to the human `cwcli apps checkout`.** Its flags, output, exit codes, and messages stay byte-identical.
- **NO change to `core/apps.py`.** If implementation finds a core change is needed, that is a signal worth reporting, not absorbing.
- **NO `--yes` / auto-start on the agent surface.** An agent composes `cwcli axi start` then this verb.
- **NO resulting-commit field on `AppsReport`** in this change (Decision 3).
