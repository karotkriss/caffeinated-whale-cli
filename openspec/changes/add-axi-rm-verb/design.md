# Design - `cwcli axi rm`

## Decision 1: A thin renderer, zero core delta

`core.remove(project, *, remove_volumes, no_backup, on_event) -> Result[RemovalOutcome]` is already the whole operation, and `migrate-rm-core` shaped it for exactly this caller: one plain function, plain-data DTO, typed events, no prompt, no exit.

The verb therefore adds no logic. It parses three things, calls the core, and renders.
That is not an aesthetic preference - it is the property that makes the captain's approval safe to act on. Every safety guarantee named in the deferral is a line of `core/rm.py`, so a frontend that reimplements none of them cannot weaken any of them.

`migrate-rm-core` predicted this shape: *"the single-function core shape makes the verb thin whenever it is decided (it would wire the destructive consent separately, the `apps uninstall` precedent)"*. That is what happened.

## Decision 2: Consent is un-fused from auto-start, and the fusion is the interesting half

The human verb's `--yes` means two things. `core.remove` takes neither - it has no consent parameter at all, because rm's confirmation is computed in the frontend BEFORE any core call (`migrate-rm-core` Decision 1), and it has no auto-start because the transient-start flow lives in the frontend too (it reuses `_check_port_conflicts`, which prompts).

So this surface had to decide both, and it splits them:

- **Consent**: `--yes`, required. Its default is `False`, so a caller that simply did not pass it can never be read as having consented.
- **Auto-start**: absent. Not "defaulted off" - there is no parameter, and a test asserts the verb's source reaches no start path at all.

The reasoning for the split is the same one that produced `core.remove`'s signature. A fused flag is a frontend's interface convenience; carrying it onto a second frontend spreads the convenience into a capability. An agent's request to DELETE an instance is not a request to START one, and the two happening under one flag is how an agent ends up having started containers a user deliberately stopped - the threat `axi apps checkout` named when it refused auto-start.

## Decision 3: No `--no-backup`, and the stopped-project refusal that follows from it

Dropping auto-start has a consequence that must be stated rather than discovered: a stopped project on the volume-deleting path can never satisfy the backup gate on this surface.

Three ways to answer it were available.

1. **Reimplement the transient start non-interactively.** Rejected. It would put a container start behind a delete verb - Decision 2's whole point - and it would duplicate `_transient_start_for_backup`/`_wait_for_db_ready`/`_stop_after_transient_start`, a frontend flow whose port-conflict step prompts.
2. **Add `--no-backup` so the caller can delete anyway.** Rejected. That flag turns off C1. `axi apps install` refused a `--force` and `axi migrate` refused a `--skip-maintenance` on the same grounds (captain ruling M1): with no named beneficiary, a bypass flag's existence is itself the harm.
3. **Refuse, early, naming every way out.** Chosen.

The refusal is raised in the frontend BEFORE `core.remove` is called, so the core never creates an archive directory it cannot fill. The core's own not-running branch stays the fail-closed backstop behind it, not a substitute for it - if this pre-check is ever wrong, the core still aborts before removing anything.

It is scoped to the STOPPED case only. An ORPHAN (no containers at all) is passed to the core, which distinguishes a project holding leftover data-bearing volumes from one that is genuinely gone; reporting the latter as "not running" would be a lie, and the core already returns it as `found=False`, an exit-0 no-op.

`--no-volumes` deliberately does NOT trigger the refusal: it destroys nothing data-bearing, so the gate does not apply and a stopped project is removable that way. That is one of the three exits the refusal names.

## Decision 4: The refusal messages carry the human verb's flag, on purpose

`core.remove`'s gate warning hints *"use --no-backup to remove without a backup"*. On this surface that flag does not exist, so an unmodified pass-through would name a flag the caller cannot use.

Rather than change the core's text (which the human verb depends on and which is correct there), the verb appends its own `Message` to the emitted warnings when the gate blocked: it states that NOTHING was deleted and names `cwcli rm <project> --no-backup` - the flag WITH the command that has it.

This is the actionability rule applied literally. The alternative - a bare refusal - is what pushes a caller back to the raw command, which is the outcome the whole `axi` surface exists to avoid.

## Decision 5: Exit codes read `outcome.failures`

Unchanged from the human verb and from every other aggregating frontend in this repo. A partial removal returns `Status.WARNING`, and `WARNING` maps to exit 0 everywhere else - so a status-driven exit code would report success for an instance that is still half there.

Pinned by a test that fails if the source reads `result.status`.

## Decision 6: Narration to stderr, one TOON document on stdout

`RmStep` (the human spinner label) and `RmNotice` (its progress line) are the only running account of which bench is being backed up and which volume is being destroyed, and a removal can take minutes, so both narrate to stderr for liveness. `RmWarning`/`RmError` follow.

`RmTrace` is DROPPED: it is the human verb's `--verbose`-only diagnostic and this surface has no `--verbose` (stdout is always TOON).

## What was verified end to end, and why unit tests were not enough

The repo's standing rule for delete paths - a real full lifecycle against the worktree's own editable install, never mocks and never a published build - was followed, because a mocked delete proves nothing about a delete. That rule exists because mocks previously hid a real bug in this exact area.

The evidence is `docs/e2e/axi-rm-destructive-lifecycle.md`: a real `cwcli axi init`, real seeded data, `cwcli axi rm --yes`, then two independent confirmations - the backup restored into a fresh instance with the seeded record readable, and the deletion honest with volumes and directory actually gone.
