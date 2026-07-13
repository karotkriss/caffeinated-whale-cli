## Why

The start / status rework migrates `cwcli start` and `cwcli status` onto the UI-pure logic core, and it deliberately CHANGES behavior: `start` becomes idempotent (no more latent double-start), `status` reports real per-process health instead of a single blind `curl`, `start`'s multi-bench policy aligns to the data-command prompt/error, and the captured log moves off ephemeral `/tmp` onto the bench volume.
A behavior-altering migration needs a real-Docker net UNDER it before the first line of migration code is written - the same refactor-under-green discipline the `backup` slice enjoyed via `tests/e2e/test_backup_e2e.py`.

There is no such net today.
`tests/e2e/` covers `backup` and `init` only; `start`, `status`, `logs`, and `restart` have no E2E, and `status` has no test at all (recon `data/cwcli-startstatus-recon-h9/report.md` §1d).
Their unit coverage asserts the exact shell strings handed to a fake `exec_run`, so it measures "did we build the right command", not "did the instance actually come up and answer".

This change lands FIRST (its own PR), before the migration change `migrate-start-status-core`.
It automates the manual `docs/e2e/` recipe for the lifecycle commands on the existing `tests/e2e/harness.py` seam, pinning the OUTCOME-level invariants the migration must preserve - a started instance actually serves, `status` discriminates the real lifecycle states, `logs` shows the bench stream, `restart` brings the instance back, and each is honest in both interactive and non-interactive modes.
It is structure-agnostic on purpose: it asserts observable outcomes, NOT the internal mechanics (`/tmp` log path, `pkill`-by-pattern, the three flat status tokens) that the migration replaces, so the same suite stays green before and after PR 2.

## What Changes

- **Add a real-Docker E2E module for the lifecycle commands** (`tests/e2e/test_start_status_e2e.py`, plus `test_logs_e2e.py` / `test_restart_e2e.py` if cleaner split), driving the real `cwcli` console script through the existing `tests/e2e/harness.py` fixtures (`session_instance` / `running_instance`, the `cwe2e-` isolation rails, the teardown backstop), marked `e2e` in the permanent v14/v15/v16 matrix.
- **Pin the preserved invariants as real side-effect assertions, not string matches:**
  - `cwcli start <project>` on a stopped instance starts the containers AND the bench, and the site becomes genuinely reachable (the web port answers inside the container / the site is ready) - reusing `harness.wait_for_site_ready`.
  - `cwcli start <project>` re-run on an already-serving instance leaves it still serving and exits 0 (the re-run invariant; this change asserts only that the instance stays healthy, NOT the internal process count, which is exactly the double-start mechanic PR 2 fixes).
  - `cwcli status <project>` returns `offline` when the project's containers are absent or stopped, and reports the running state (site answering) distinctly from the containers-up-but-bench-not-started state - asserted via the states themselves, matching only the tokens that are contractually stable across the migration (`offline`, and the running state) rather than over-fitting to the transitional `online` token PR 2 may enrich.
  - `cwcli logs <project>` shows the bench's captured output for a started instance (asserted by content/exit, NOT by the literal `/tmp/bench-<project>.log` path, which PR 2 relocates).
  - `cwcli restart <project>` stops then restarts the instance and the site is reachable again afterward.
- **Both modes per command** (captain standard): non-interactive via flags with stdin closed (`--yes`, `--bench`), and interactive via `pexpect` awaiting the prompt_toolkit `ESC[?2004h` raw-mode marker (`harness.spawn_cwcli` / `expect_prompt_ready`), asserting each prompt is genuinely shown and answered.
- **Honest exit codes.** A `start`/`restart` against a nonexistent project exits non-zero; a successful run exits 0. `status` exits 0 across the lifecycle states (today's contract).
- **Multi-bench is driven explicitly.** Where a multi-bench instance is exercised, the E2E passes `--bench <index|label>` rather than relying on the ambiguous default - because the no-selector multi-bench policy is exactly what PR 2 changes (D5), so this net must not encode the pre-migration `first-with-note` behavior.
- **No source or CI-matrix changes.** This is a test-only addition on the already-shipped harness and the already-shipped `e2e.yml` matrix; the migration change owns all `src/` edits.

## Capabilities

### New Capabilities

- `start-status-e2e`: real-Docker, real-side-effect E2E coverage for the lifecycle commands (`start`, `status`, `logs`, `restart`) that drives the real `cwcli` binary against genuine throwaway Frappe instances, asserts observable outcomes (a started instance serves; `status` discriminates the real lifecycle states; `logs` shows the stream; `restart` recovers the instance) in both interactive and non-interactive modes with honest exit codes, and is structure-agnostic so it survives the subsequent core migration unchanged (refactor-under-green).

### Modified Capabilities

- (none - `openspec/specs/` holds no archived capabilities yet, so no existing requirement changes. The suite lands additively alongside the existing `e2e-test-suite` capability from `rebuild-e2e-test-suite`.)

## Impact

- **New tests:** `tests/e2e/test_start_status_e2e.py` (and, if split for clarity, `tests/e2e/test_logs_e2e.py` / `tests/e2e/test_restart_e2e.py`) - real-side-effect E2E for `start` / `status` / `logs` / `restart` in both modes, on the existing `tests/e2e/harness.py` seam.
- **Reuses (no changes):** `tests/e2e/harness.py` (`run_cwcli`, `spawn_cwcli`, `expect_prompt_ready`, `exec_in_frappe`, `frappe_container_id`, `wait_for_site_ready`, the isolation rails, the `cwe2e-` teardown backstop) and `tests/e2e/conftest.py` (`session_instance` / `running_instance`); the permanent v14/v15/v16 `e2e.yml` matrix runs the new module unchanged.
- **No `src/` changes, no CI workflow changes, no new dependencies.** `pexpect` is already the E2E extra.
- **Docs:** a short entry in `tests/README.md` / `docs/testing/README.md` noting the lifecycle-command E2E now exists, landing with this PR.

## Non-Goals

- Any `src/` change - the migration onto the core (`core.start` / `core.status`, the reseated CLIs, the `cwcli axi` verbs, the log relocation, the idempotency fix) is the separate `migrate-start-status-core` change / PR 2.
- Asserting the NEW post-migration behavior - per-process health fields, the `degraded` aggregate, the idempotent single-supervisor no-op, the relocated bounded log, the multi-bench prompt/error. Those tests land WITH PR 2 as its own new-behavior coverage, on top of this preserved net.
- Pinning migration-transient mechanics: the `/tmp/bench-<project>.log` path, `pkill -f 'bench start'`, and the exact three flat status tokens are deliberately NOT asserted, so PR 2 can replace them without editing this suite.
- New CI matrix legs, self-hosted runners, or a pre-built sited base image (owned by `rebuild-e2e-test-suite`; out of scope here).
