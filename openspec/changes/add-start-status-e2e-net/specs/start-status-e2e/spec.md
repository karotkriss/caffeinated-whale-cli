## ADDED Requirements

### Requirement: Real-Docker E2E net for the lifecycle commands

The system SHALL provide a real-Docker `e2e`-marked test suite for `cwcli start`, `cwcli status`, `cwcli logs`, and `cwcli restart` that drives the real `cwcli` console script against genuine throwaway Frappe instances on the existing `tests/e2e/harness.py` seam (the `cwe2e-` isolation rails, the session/instance fixtures, the teardown backstop) and runs in the permanent v14/v15/v16 `e2e.yml` matrix.
The suite SHALL assert observable OUTCOMES (container state, process/site reachability, exit codes) via `harness` helpers, never mock strings, and SHALL NOT introduce any `src/` or CI-workflow change.

#### Scenario: Started instance genuinely serves

- **WHEN** `cwcli start <project>` runs against a stopped throwaway instance
- **THEN** its containers and bench start, the command exits 0, and the site becomes genuinely reachable (asserted via `harness.wait_for_site_ready` / the web port answering inside the container), not by a string match

#### Scenario: Re-running start leaves the instance serving

- **WHEN** `cwcli start <project>` is run a second time against an already-serving instance
- **THEN** the command exits 0 and the site is still reachable afterward - asserting only that the instance stays healthy, NOT the in-container process count (the double-start mechanic PR 2 fixes)

#### Scenario: Missing project fails honestly

- **WHEN** `cwcli start <nonexistent>` (or `cwcli restart <nonexistent>`) runs
- **THEN** the command exits non-zero and never reports the instance as started

### Requirement: status discriminates the real lifecycle states

The E2E SHALL assert that `cwcli status` reports the genuine lifecycle state of an instance, matching only the tokens that are contractually stable across the migration (`offline`, and the running state) and asserting the not-started state by its invariant rather than by string-matching the transitional token.
`cwcli status` SHALL exit 0 across these states (today's contract).

#### Scenario: Offline when containers are absent or stopped

- **WHEN** `cwcli status <project>` runs with the project's containers absent or stopped
- **THEN** it prints `offline` and exits 0

#### Scenario: Running when the site answers

- **WHEN** `cwcli status <project>` runs against an instance whose site is reachable
- **THEN** it reports the running state and exits 0

#### Scenario: Containers up but bench not started is distinct from running

- **WHEN** `cwcli status <project>` runs with the containers up but `bench start` not yet run
- **THEN** it reports a state distinct from the running state - asserted by the invariant (containers up, site not answering), NOT by string-matching the transitional `online` token that PR 2 may enrich

### Requirement: logs shows the bench stream

The E2E SHALL assert that `cwcli logs <project>` surfaces the started bench's captured output, asserted by content/exit rather than by the literal container log path (which PR 2 relocates off `/tmp`).

#### Scenario: logs surfaces output for a started instance

- **WHEN** `cwcli logs <project> --no-follow` runs against a started instance
- **THEN** it prints the captured bench output and exits 0, with no assertion on the `/tmp/bench-<project>.log` path

### Requirement: restart recovers a reachable instance

The E2E SHALL assert that `cwcli restart <project>` stops then restarts the instance and the site is reachable again afterward.

#### Scenario: restart brings the site back

- **WHEN** `cwcli restart <project>` runs against a running instance
- **THEN** the command exits 0 and the site is genuinely reachable again afterward (via `harness.wait_for_site_ready`)

### Requirement: Both interactive and non-interactive modes are covered

Every prompting lifecycle command SHALL be E2E-verified in BOTH modes (captain standard): non-interactive through flags with stdin closed (`--yes`, `--bench`), and interactive through a pty (`harness.spawn_cwcli` + `pexpect`) that awaits the prompt_toolkit `ESC[?2004h` raw-mode marker before each keystroke and asserts each prompt is genuinely shown and answered.
Any multi-bench case SHALL be driven with an explicit `--bench <index|label>` rather than the ambiguous default (the exact policy PR 2 changes).

#### Scenario: Non-interactive start runs to completion

- **WHEN** `cwcli start <project> --yes` runs from a non-TTY with stdin closed
- **THEN** it starts the instance to a reachable state and exits 0 with no hang and no prompt

#### Scenario: Interactive port-conflict prompt is genuinely driven

- **WHEN** `cwcli start <project>` is driven through a pty into its port-conflict confirmation prompt
- **THEN** the prompt is shown (after the `ESC[?2004h` marker), answering it proceeds, and the run completes - proving the interactive path is not skipped or auto-answered

#### Scenario: Non-interactive multi-bench uses an explicit selector

- **WHEN** a multi-bench instance is exercised in the E2E
- **THEN** the command passes `--bench <index|label>` explicitly, so the suite encodes neither the pre-migration `first-with-note` default nor the post-migration prompt/error
