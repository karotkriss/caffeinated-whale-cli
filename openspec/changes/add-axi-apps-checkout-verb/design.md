# Design: `cwcli axi apps checkout` over the unchanged `core.checkout_app`

The verb itself is thin: `core.checkout_app` already returns `Result[AppsReport]`, already resolves the bench and the run-state fork, and already returns `NEEDS_CHOICE` for both decisions it cannot make.
The weight of this change is Decision 1, which the captain owns.

The evidence base is a real read of `core/apps.py` (the `checkout_app` / `_resolve_remote` pair), `commands/apps.py`'s checkout renderer, `commands/axi.py`'s `apps` group and `init` verb, `tests/test_axi_apps_list.py`, and `openspec/changes/migrate-apps-core/tasks.md` §7, plus a live `cwcli axi --help` and `cwcli axi apps --help` on this branch and a direct probe of git's dirty-tree behaviour.

---

## Decision 1 - Does the verb exist at all?

**Recommendation: yes. Build it.**

The full argument is in `proposal.md` ("The recorded deferral, and why it does not decide this").
Restated here as the three claims it rests on, each with its evidence, so a reviewer can falsify any of them independently.

### Claim A - the locked rationale is about deleting site data, and checkout deletes none

`migrate-apps-core/tasks.md` §7.1 names the threat exactly: `bench uninstall-app` "drops the app's tables".
§7.2 goes further and pre-authorizes a different answer for non-deleting mutations: install "mutates a real site but does not delete data, so it may well be decided differently".

`core.checkout_app` (`core/apps.py:524-531`) runs, in `apps/<app>`:

```
git fetch <remote> -- <ref>
git checkout -B <ref> FETCH_HEAD
git reset --hard FETCH_HEAD        # only with --reset
```

No `bench` invocation, no site, no SQL, no table.
Whatever the right rule for agent mutation is, the one that was actually locked does not reach this.

### Claim B - the absence assertion inherited that rationale rather than taking its own

`tests/test_axi_apps_list.py:146-148` reads "`apps checkout` mutates the in-instance checkout, so **like install/uninstall** it is a HUMAN verb only".
That "like" is doing the work, and it is where the reasoning slips: install/uninstall were held for data destruction, checkout is grouped with them for being a mutation, and the conclusion carries over as if the premise had.

This is not a criticism of the person who wrote it.
`apps checkout` shipped inside `add-app-management`, and pinning its absence next to its siblings was the right conservative default at the time.
It is simply not a decision made on checkout's evidence, which is what §7.1 explicitly asked for before a verb ships.

### Claim C - "the agent surface is read-only" is not a rule this codebase follows

Enumerated live, not from memory (`cwcli axi --help` on this branch): eight of eighteen verbs mutate real state.
`axi label` writes a marker file INSIDE the bench.
`axi setup` edits the user's own `~/.claude/settings.json`.
`axi init` provisions an entire instance, bench, and site.
`axi apps update` runs `bench update --pull` and then Frappe schema migrations across live sites under maintenance mode.

A `git fetch` plus `git checkout -B` into a source directory sits well below the largest of those.
Any rule that permits `axi apps update` and forbids `axi apps checkout` has to explain why migrating a live site's schema is safer than fetching a branch into `apps/`, and no such explanation exists in the record.

### The counter, stated rather than waved away

A checkout is not zero-risk:

1. It changes the code a live site will run at the next build, migrate, or process restart. That is deferred, not immediate, but it is real.
2. `--reset` discards uncommitted work in the container's app directory.
3. It fetches from a network remote, and for a private repo it does so using the host user's `gh`/`glab` credentials.

Each is answered by a named guard in Decision 5, none is data deletion, and (3) is already true of `axi apps update`.

### If the captain declines

The useful outcome is not the status quo but a *stated* rule: what may the agent surface mutate?
Today an inherited comment on a test blocks a verb whose source rationale does not cover it, while a strictly larger mutation ships next to it.
Whichever way this goes, writing the rule down is worth more than this one verb.

---

## Decision 2 - Flag set

**Recommendation: `--bench <index|label>` and `--reset`. Nothing else.**

| Human flag | On `axi`? | Why |
| --- | --- | --- |
| `--bench <index\|label>` | **yes** | The standard selector every bench-scoped `axi` verb carries; `axi benches` is the discovery verb that answers it. |
| `--path` / `-p` | **no** | The human verb's lower-level alternative to `--bench`. No `axi` verb exposes it, and an agent that has `axi benches` does not need a raw container path. |
| `--reset` | **yes** | The clean-tree guarantee Step 7 of the delivery workflow depends on. Omitting it would leave an agent able to start the workflow and not finish it. Explicit opt-in; threat named in Decision 5. |
| `--json` | **no** | Stdout is always TOON on `axi`. |
| `--yes` / `-y` | **no** | No auto-start on the agent surface (Decision 5). |
| `--verbose` / `-v` | **no** | Stdout is always TOON; the git step narration goes to stderr unconditionally, as `axi init` narrates its phases. |

Positional arguments are the human verb's, unchanged: `<project> <app> <ref>`.

Per AXI §6, an unknown flag is rejected by name with exit 2 before any Docker call.
That is Typer's existing behaviour for this app and needs no new code; it is stated here so implementation does not weaken it.

---

## Decision 3 - Output shape, and the resulting-commit gap

**Recommendation: reuse `AppsReport` verbatim through `emit_result`, exactly as `axi apps update` does. Defer the resolved commit to a read verb.**

`core.checkout_app` returns the shared `AppsReport` with one `AppResult` per git step, which serializes cleanly:

```
project: myproj
bench_path: /workspace/frappe-bench
ok: true
results[2]{app,site,action,ok}:
  myapp,,fetch,true
  myapp,,checkout,true
```

With `--reset` a third `reset` row appears.
On a failure the run stops at the first failed step, so a dirty-tree refusal emits `fetch,true` then `checkout,false` and exits 1: the agent can see exactly which step failed, and the git message itself reached stderr through the event narrator.

Progress and the `$ git ...` echo go to stderr; stdout carries only the one TOON document (`commands/axi.py`'s one-document contract).

### The gap this leaves, and why it is deferred rather than closed here

After a checkout, an agent's deterministic next question is "which commit am I actually on?".
`AppsReport` cannot answer it, and there is no `axi run` to fall back on, so today that question is unanswerable from the agent surface.
By AXI §4 (the expensive cost is the follow-up call) that is a genuine argument for including a resolved sha.

It is still the wrong thing to add here, for two reasons:

1. `AppsReport` is SHARED with `install` / `uninstall` / `update`, and its four fields were deliberately preserved so the human `--json` shape survived the migration unchanged (`core/apps.py:66-72`). Adding a field for one caller changes the other three verbs' human `--json` output for nothing.
2. The question is not checkout-specific. "What git state is each app in?" is a READ, it is useful without having just run a checkout, and its natural home is per-app git state on `axi apps list` (which already has `--fields` reserved as an open question in `migrate-apps-core/tasks.md` §7.3).

**Recommendation: file the read as a follow-up, keep this change at zero core delta, and state the gap in the verb's docstring so an agent is not left guessing.**
If the captain prefers to close it now, the cheapest honest form is a separate read verb, not a field on the mutation DTO.

---

## Decision 4 - Exit codes

**Recommendation: read `report.ok`, never `result.status`.**

| Outcome | Exit | Rendering |
| --- | --- | --- |
| All git steps succeeded | 0 | the document above |
| A git step failed (dirty tree, bad ref, auth failure, no such remote branch) | 1 | the same document with `ok: false` and the failing row |
| Stopped container (`confirm_start`) | 2 | TOON usage error naming `cwcli start` |
| Multi-bench, no `--bench` (`select_bench`) | 2 | TOON usage error listing benches, naming `--bench` |
| App directory is not a git checkout (`NOT_FOUND` / `app.no_checkout`) | `exit_for(NOT_FOUND)` | `error:` / `help:` naming `cwcli apps list` |
| No remote (`PRECONDITION` / `app.no_remote`), `DOCKER`, other `CwcliError` | `exit_for(kind)` | `error:` / `help:` lines |

`report.ok` and not `result.status` is batch 4's trap, live again here: a partial failure is a `WARNING`-shaped envelope, and `WARNING` maps to exit 0 everywhere else, so a status-driven exit code would report success for a checkout whose `checkout` step was refused.
`axi apps list` and `axi apps update` both already read `.ok`; this verb matches them.

All three `NEEDS_CHOICE` rows reuse the existing `emit_axi_choice_as_usage_error`, which already renders `confirm_start` and `select_bench`.
No new choice kinds, no new emitter.

---

## Decision 5 - Safety posture

The table in `proposal.md` is the authoritative list.
Three entries need their evidence recorded here.

### The dirty-tree refusal is git's, and cwcli should not duplicate it

Probed directly against real git rather than assumed: with a conflicting uncommitted edit in the working tree,

```
$ git checkout -B feature FETCH_HEAD
error: Your local changes to the following files would be overwritten by checkout:
	f.txt
Please commit your changes or stash them before you switch branches.
Aborting
                                      # exit 1, and f.txt still holds the local edit
```

So the non-`--reset` path already fails closed on a dirty tree, at the layer that owns the question, and the file survives.
Adding a cwcli-side `git status --porcelain` pre-check would be a second implementation of a guard that already works, with its own drift risk, and would additionally reject edits git is perfectly able to carry across (a local edit to a file the diff does not touch is preserved, which is correct).

The verb's obligation is to make that refusal legible: the failed step appears in `results`, `ok` is `false`, the exit code is 1, and git's own message reached stderr.

### `--reset` is kept, explicit, and reported

Threat: irrecoverable loss of uncommitted work inside the container's `apps/<app>`.
`--reset` is exactly the escape hatch from the guard above, and that is its purpose: `bench update`'s dirty-tree guard blocks the workflow's post-merge step otherwise.

It is kept rather than omitted because an agent without it can reach a state (dirty tree, checkout refused) it has no agent-surface way out of.
It is safe enough to expose because it is opt-in by construction: the destructive behaviour requires the agent to have typed the flag, and the resulting `reset` row in `results` records that it ran.

`ponytail:` no additional confirmation axis. The flag IS the consent, and `core.checkout_app` deliberately has no `consent` parameter (unlike `uninstall_apps`, which needs one because it drops tables). Upgrade path if a real incident ever shows an agent passing `--reset` carelessly: add `consent` to the core the way `uninstall_apps` models it, so `axi` and a GUI cannot bypass it.

### The credential bridge is inherited, not extended

`core.checkout_app` wraps its steps in `credbridge.credential_bridge` (`core/apps.py:534`), the same bridge `core.update.update` wraps its whole dispatch in.
Since `axi apps update` already ships, the agent surface ALREADY reaches the host's `gh`/`glab` auth for in-container fetches.
This verb adds no new exposure: same bridge, same per-invocation socket and teardown, raw token still never entering the container, still inert for public repos (git only calls a credential helper on HTTP 401).

Recorded explicitly so that "an agent can use my GitHub credentials" is a known property of the surface rather than something discovered later during a checkout.

---

## Decision 6 - The recache epilogue

**Recommendation: mirror the human verb exactly, using `axi init`'s existing pattern.**

The human renderer refreshes the cache whenever any git step ran (`commands/apps.py:472-475`), because a checkout changes the app's git state and its reported version.
Without it, the next `axi apps list` or `axi inspect` serves a stale version for the app the agent just moved, which is precisely the read an agent makes to confirm its own work.

This is a post-mutation epilogue gated on a condition already present in the returned report, which CLAUDE.md records as the class that "hoists for free".
It reaches `cache.recache_project`, which calls `core.inspect` since batch 7, so it is not a frontend-calling-frontend edge.
`axi init` already does this and warns to stderr on failure (`commands/axi.py:1249-1255`); this verb copies that shape verbatim.

A failed recache is a stderr warning, not a non-zero exit: the checkout itself succeeded, and reporting failure for a stale cache would make an agent retry a mutation that already landed.

---

## Alternatives considered

- **Ship `axi apps checkout` and `axi apps install` together.** Rejected on scope discipline. `install` is a genuinely different decision on different evidence (it clones a NEW app and installs it onto sites), it is filed separately as `cwcli-axi-apps-install-verb`, and bundling them would force one approval to carry two arguments, which is how checkout inherited the wrong rationale in the first place.
- **Omit `--reset` from the agent surface.** Rejected: it strands an agent at a dirty tree with no way forward, and it removes the exact capability the workflow's post-merge step needs, while removing no capability the agent could not obtain by other means anyway.
- **Add a cwcli-side dirty-tree pre-check.** Rejected: git already refuses and fails closed (probe above); a second implementation drifts and over-rejects.
- **Add `resolved_commit` to `AppsReport`.** Rejected here; the useful form is a read on `axi apps list`, and the shared DTO's shape is load-bearing for three other verbs' `--json` (Decision 3).
- **Add a `--yes` that auto-starts a stopped project.** Rejected: no bench-scoped `axi` verb opens a start-from-axi path; an agent composes `cwcli axi start` first.
