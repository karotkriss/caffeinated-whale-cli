## ADDED Requirements

### Requirement: cwcli apps update supports --json

`cwcli apps update` SHALL accept `--json` and emit the `UpdateReport` as a single JSON document on stdout, matching its three sibling subcommands (`list`, `install`, `uninstall`), which already do.
In `--json` mode stdout SHALL carry ONLY that document: all progress, diagnostics, and bench output SHALL go to stderr or be discarded, and no bench output SHALL reach stdout on any path, including the `--app frappe` bench-wide reset.
`README.md`'s statement that "`apps update` delegates to the streaming update flow and has no `--json`" SHALL be removed, since the exec-stream contract removed that blocker.

#### Scenario: JSON mode keeps stdout pure

- **WHEN** `cwcli apps update <project> <app> --json` runs and bench emits output
- **THEN** stdout holds exactly one parseable JSON document and no bench output

#### Scenario: JSON mode stays pure on the frappe path

- **WHEN** `cwcli apps update <project> --app frappe --json` runs
- **THEN** stdout holds exactly one parseable JSON document, and the `bench update --reset` output does not corrupt it

#### Scenario: A partial failure is visible in the document and the exit code

- **WHEN** a multi-site update migrates one site and fails another
- **THEN** the JSON document names both outcomes, its `ok` is false, and the command exits non-zero

### Requirement: cwcli axi apps update emits one TOON report

The system SHALL provide `cwcli axi apps update <project> <apps...>` as the agent-facing verb over the SAME `core.update` the human CLI calls, adding no business logic of its own.
It SHALL emit exactly ONE TOON document on stdout, the serialized `UpdateReport`, and SHALL never prompt.
There SHALL be no `axi update` verb on the deprecated spelling: the deprecated surface is not worth an agent-facing verb, and `axi` gets one verb on the canonical spelling.
Progress events SHALL NOT be emitted on stdout: multiple documents would violate the one-TOON-document contract, and the shipped precedent is `axi backup`, which runs a minutes-long `bench backup` and emits only its terminal outcome.

#### Scenario: One document, whatever bench prints

- **WHEN** `cwcli axi apps update <project> <app>` runs a migration that emits output
- **THEN** stdout carries exactly one TOON document and nothing else

#### Scenario: The verb blocks and returns a verdict rather than streaming progress

- **WHEN** an agent invokes `cwcli axi apps update` for a long-running update
- **THEN** the command blocks until the update finishes and emits one terminal report, exactly as `axi backup` does

### Requirement: Both structured surfaces exit on the report's aggregate, not the envelope status

`cwcli apps update --json` and `cwcli axi apps update` SHALL derive their exit code from `UpdateReport.ok`: 0 when true, 1 when false.
They SHALL NOT copy the shipped `axi backup` pattern of exiting on `result.status`, because that pattern maps `WARNING` to exit 0 and a partial `update` failure is a `WARNING`-shaped envelope carrying `ok=False`; copying it verbatim would report success for an update that partly failed.
A `NEEDS_CHOICE` result SHALL be rendered as a TOON usage error and exit 2.
`UpdateReport.ok` SHALL be pre-computed by the core rather than re-derived by each frontend, matching the existing `"ok"` key on `apps`'s other subcommands.

#### Scenario: A partial failure exits non-zero on the agent surface

- **WHEN** `cwcli axi apps update` completes with any failed or unknown item, or any stuck site
- **THEN** it emits the report and exits 1, never 0

#### Scenario: Multi-bench with no selector is a usage error

- **WHEN** `cwcli axi apps update` runs against a multi-bench project with no `--bench`
- **THEN** it emits a TOON usage error naming the flag to pass, lists the available benches, and exits 2

#### Scenario: An unknown outcome is distinguishable from a failure by an agent

- **WHEN** an agent reads the emitted report after a lost stream
- **THEN** the affected item appears in an `unknown_*` field rather than a `failed_*` field, so the agent can tell "this failed, retry is safe" from "we lost track of this, it may still be running"

### Requirement: apps update is covered by a both-modes E2E on a real instance

`cwcli apps update` SHALL gain end-to-end coverage against a real Frappe instance, in both modes, per the captain standard.
There is no E2E for `apps` or `update` today, and `apps update` prompts (its auto-start path), so the standard applies.
The E2E SHALL cover the interactive path driven through a real pty, the non-interactive path driven by `--yes`, and a non-TTY without `--yes` refusing non-zero rather than hanging, defaulting, or silently proceeding.
It SHALL include `apps update --app frappe`, which runs `bench update --reset` gated by no confirmation and is the path most in need of real coverage.

#### Scenario: The interactive path genuinely prompts

- **WHEN** `cwcli apps update` runs against a stopped instance at a TTY
- **THEN** the auto-start prompt is shown and genuinely collects input, rather than being skipped or returning empty without waiting

#### Scenario: The non-interactive path runs to completion with no prompt

- **WHEN** `cwcli apps update <project> <app> --yes` runs against a stopped instance with no TTY
- **THEN** it completes with no prompt

#### Scenario: A non-TTY without the flag refuses

- **WHEN** `cwcli apps update` needs to auto-start with no TTY and no `--yes`
- **THEN** it refuses with a non-zero exit rather than hanging, defaulting, or silently proceeding
