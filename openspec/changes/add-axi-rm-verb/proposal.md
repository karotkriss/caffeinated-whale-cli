## Why

**cwcli's only destructive verb has no agent-facing form, and its absence was a decision that has now been reversed.**

`cwcli rm` removes an instance's containers, its named volumes (databases, sites, files), and its local project directory.
It is the one operation an agent driving a full lifecycle cannot finish: it can `axi init` a whole instance, install apps into it, migrate its schema and run its tests, and then has no way to take it down again.
Every teardown drops to the human command.

The verb's absence was pinned by a test (`tests/test_core_rm.py::TestNoAxiRmVerb`) recording the deferral from `migrate-rm-core` design Decision 5:

> whether an agent may delete an instance's data (named volumes, the whole bench) is a product decision the captain owns on its own evidence [...] The fail-closed backup gate protects against ACCIDENT, not against an agent that deliberately means to delete.

That is a real distinction and it was never about the gate being weak.
It asked one question - should an agent hold this capability at all - and the captain answered it on 2026-07-21:

> doing cwcli axi rm is very doable at this point in time.

**That approval overturns the WHETHER and waives nothing else.**
This proposal treats it that way: the verb is a thin frontend over the UNCHANGED `core.remove`, so every safety property lives in the same code the human verb runs, and the two properties the human FRONTEND owns are re-decided here on the agent surface's own evidence rather than inherited.

## What ships

`cwcli axi rm <project> --yes [--no-volumes]`, a TOON renderer over `core.remove`.
Zero `core/rm.py` delta.

### Inherited unchanged, because they live in the core

- The EARLY fail-closed backup gate, evaluated before any container is removed, so a failed backup aborts while the frappe container is still alive and a retry can still produce one.
- The per-bench verified copy-out (`_backup_sites` streams each artifact to the host and confirms the DB dump landed non-empty).
- The conf/ and project-directory archives, and the cache entry kept whenever a step failed.
- The exit code read from `outcome.failures`, never `result.status`: a partial removal is a `WARNING`-shaped envelope, and `WARNING` maps to exit 0 everywhere else.

### Decided here, because the human frontend owns them

**1. `--yes` grants consent ONLY.**

The human `cwcli rm --yes` fuses two meanings: skip the destructive confirmation, AND auto-start a stopped project so a live backup can be taken.
`core.remove` deliberately does not take that fused flag - the fusion is one frontend's UX choice and `migrate-rm-core` kept it out of a core that also serves `axi` and a future GUI.
Re-creating it here would mean an agent that asked to delete an instance had thereby STARTED one, which is the threat `axi apps checkout` already named when it refused auto-start: an agent starting containers a user deliberately stopped.

So consent is the flag's whole meaning, and there is no auto-start on this surface.

**2. There is deliberately NO `--no-backup`.**

That flag is the C1 gate's off switch - the single guard between this verb and unrecoverable data loss.
On this surface it has no named beneficiary, and the rulings on that exact shape are already settled: `axi apps install` ships without a `--force` and `axi migrate` without a `--skip-maintenance` (captain ruling M1), both on the reasoning that a bypass flag's mere existence invites its use.
The escape hatch is the human `cwcli rm --no-backup`, which is where a human confirms the loss.

### The consequence of dropping the auto-start half, and how it is answered

Without a transient start, a STOPPED project on the volume-deleting path cannot satisfy the gate, because a live `bench backup` needs a running bench.
Deleting anyway is precisely what the gate exists to prevent, so the verb refuses (`NOT_RUNNING`, exit 1) before anything is touched.

**A refusal must explain itself.**
A destructive verb that answers only "no" pushes its caller toward the raw command, which is the path this verb exists to replace.
So every refusal names its ways out:

| Refusal | Names |
| --- | --- |
| No `--yes` | `--yes`, and what it will permanently delete |
| Stopped project, volumes | `cwcli start <project>` then re-run, `--no-volumes` (destroys no data), `cwcli rm <project> --no-backup` (the human hatch) |
| Backup gate blocked | that NOTHING was deleted, and `cwcli rm <project> --no-backup` |

One project per invocation, never the human verb's variadic list or stdin pipe: a fan-out is how an agent reaches an instance it never named, and here that costs an instance.

## Impact

- `commands/axi.py`: one new verb, one narrator. 17 -> 18 top-level verbs.
- `core/rm.py`: unchanged.
- `tests/test_core_rm.py`: `TestNoAxiRmVerb` becomes `TestAxiRmVerbShipped`, reversed deliberately and carrying the reasoning, the way `apps checkout` and `axi init` reversed theirs.
- `tests/test_axi_rm.py`: new, covering the two decided properties and every refusal's actionability.
- `skills/cwcli/SKILL.md`: regenerated from the live registry.
