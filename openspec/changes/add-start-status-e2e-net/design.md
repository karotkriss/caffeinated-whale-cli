## Context

This is the first of the two changes the start / status rework decomposes into (the captain's two-PR structure): a behavioral E2E net that lands FIRST, then the core migration (`migrate-start-status-core`) under it.
It mirrors how the overall core rework already split `rebuild-e2e-test-suite` (the net) from `core-logic-foundation` (the reference migration).

The recon (`data/cwcli-startstatus-recon-h9/report.md`) establishes the ground truth: `start` is fire-and-forget with no process handle, `status` is a single blind `curl`, neither is on the core, and there is no start/status E2E today (§1, §1d).
OpenSpec reads no code, so the `file:line` citations come from that recon and the reading done for this proposal.

## Goals / Non-Goals

**Goals:**

- A real-Docker net that proves the lifecycle commands actually work end to end on a genuine instance, so the subsequent migration is refactor-under-green from its first commit.
- Assert observable OUTCOMES, so the net is structure-agnostic and survives PR 2 unchanged.
- Cover both interactive and non-interactive modes per command (captain standard), with honest exit codes.

**Non-Goals:** any `src/` change; asserting post-migration behavior (per-process health, `degraded`, idempotent single-supervisor, relocated log, multi-bench prompt); pinning migration-transient mechanics (`/tmp` path, `pkill` pattern, the exact status tokens). See the proposal's Non-Goals.

## The load-bearing decision: pin invariants, not mechanics

The subtlety that makes this a genuine net rather than a change-detector PR 2 must rewrite: the migration is NOT behavior-preserving the way the `backup` migration was.
PR 2 deliberately changes four observable things - `start` idempotency, `status` output shape, `start`'s multi-bench policy, and the log location.
So a naive "pin today's exact output" net would go red the moment PR 2 lands, defeating its purpose.

The net therefore asserts the behavior the migration must PRESERVE, and deliberately abstains from the mechanics it changes:

| Preserved invariant this net pins | Migration-transient mechanic this net must NOT pin |
| --- | --- |
| A started instance genuinely serves (site ready / web answers) | The `/tmp/bench-<project>.log` path (PR 2 relocates it) |
| Re-running `start` leaves the instance still serving, exit 0 | The in-container process count / that a second honcho was/wasn't spawned (PR 2 makes it a clean no-op) |
| `status` returns `offline` for absent/stopped containers | The exact `online` token for the containers-up-but-not-started state (PR 2 may enrich it) |
| `status` reports the running state (site answering) distinctly from the not-started state | The single-`curl` probe internals / three-flat-token shape |
| `cwcli logs` shows the bench stream for a started instance | That logs reads `/tmp` specifically |
| `cwcli restart` recovers a reachable instance | The `stop`+`_start_project` internal wiring |
| Honest exit codes (missing project non-zero; success 0) | - |

This is the same "structure-agnostic, drives the binary, asserts a real side effect" property that lets `tests/e2e/test_backup_e2e.py` pin `backup` across its migration (recon §1d, `core-logic-foundation` design §1).

Two concrete guards fall out of it:

- **Assert states, match only stable tokens.** `offline` and the running state are contractually stable across the migration and may be asserted directly; the transitional not-started token is asserted by its INVARIANT (containers up, site not answering) rather than by string-matching `online`, which PR 2 is free to enrich toward the `overall` aggregate (`offline`/`online`/`running`/`degraded`).
- **Drive multi-bench explicitly with `--bench`.** The no-selector multi-bench default is the exact policy PR 2 changes (D5: drop `on_ambiguous="first"`, align to the data-command prompt/error). Any multi-bench case this net exercises passes `--bench <index|label>` so it encodes neither the old `first-with-note` nor the new prompt/error, staying valid across the change.

## Approach

Follow `tests/e2e/test_backup_e2e.py` exactly:

- Drive the real `cwcli` console script via `harness.run_cwcli` (non-interactive, stdin closed) and `harness.spawn_cwcli` + `pexpect` (interactive, awaiting the `ESC[?2004h` marker via `expect_prompt_ready`).
- Stand up / tear down through the existing session fixtures (`running_instance` for the up-state, `session_instance` for lifecycle transitions); never touch Docker directly for lifecycle - drive `cwcli`.
- Assert real side effects via `harness.exec_in_frappe` / `harness.frappe_container_id` / `harness.wait_for_site_ready` (container state, process presence, site reachability), never a mock string.
- Mark `pytest.mark.e2e`; the module is collected but deselected under the fast `-m unit` tier and runs in the v14/v15/v16 `e2e.yml` matrix unchanged.

## Risks / Trade-offs

- **A `start` lifecycle test that stops the shared `session_instance` could disturb sibling tests.** Mitigation: lifecycle-mutating cases (stop -> start, restart) use their own throwaway instance or restore the running state (as `running_instance` already does with `frappe_container_id`/`wait_for_site_ready`), so ordering stays independent - matching the harness's existing per-test `running_instance` guarantee.
- **`wait_for_site_ready` timing on a cold `bench start`.** The dev stack takes time to answer; reuse the harness's existing generous `wait_until`/`wait_for_site_ready` budgets rather than fixed sleeps.
- **The net must actually go green on TODAY's code.** Every assertion is chosen to hold on the pre-migration implementation (e.g. re-run `start` stays reachable because the first honcho keeps serving even if a second is spawned), so this PR is green on its own before PR 2 exists.

## Migration Plan

Purely additive test code on an already-shipped harness and matrix.
No `src/` change, no schema/config change, no dependency added.
Rollback is deleting the new test module(s).
