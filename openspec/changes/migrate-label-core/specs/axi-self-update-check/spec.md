## ADDED Requirements

### Requirement: cwcli axi self-update --check is a read-only version check over the existing core

The system SHALL provide `cwcli axi self-update --check`, emitting `core.version.check`'s `VersionInfo` as exactly one TOON document on stdout.
It SHALL be built on the existing `emit_result` / `emit_axi_error` / `exit_for` helpers and the already-migrated `core.version.check`, adding NO core logic: `core/version.py` is already UI-pure and already returns `Result[VersionInfo]`, and `VersionInfo` is already a serializable frozen dataclass.
It SHALL be read-only and SHALL NOT execute an upgrade.
`--no-cache` SHALL be supported, forcing a fresh PyPI lookup exactly as the human command does.

#### Scenario: An agent can ask whether cwcli is current

- **WHEN** `cwcli axi self-update --check` runs
- **THEN** it emits one TOON document carrying the current version, the latest version, the detected install method, and whether an update is available, and it upgrades nothing

### Requirement: The check verb reports outdatedness in the document, not the exit code

`cwcli axi self-update --check` SHALL exit 0 on any successful read, including when an update IS available, and SHALL carry that fact in the document's `is_outdated` field.
This SHALL deliberately diverge from `cwcli self-update --check`, which exits 1 when an update is available so shell scripts can gate on it; the human command's exit code SHALL be unchanged.
The divergence exists because on the agent surface a non-zero exit means an error, and a read verb that successfully answers "you are outdated" has not failed.
This SHALL match the established axi precedent that a successful read exits 0 regardless of what it found, as `cwcli axi status` does when reporting an entirely offline project.
Exit non-zero SHALL be reserved for a genuine failure.

#### Scenario: An available update is a successful read

- **WHEN** `cwcli axi self-update --check` runs and a newer version exists on PyPI
- **THEN** it emits `is_outdated: true` in the document and exits 0

#### Scenario: An unreachable PyPI fails open and still exits 0

- **WHEN** `cwcli axi self-update --check` runs and the PyPI lookup fails
- **THEN** it emits the document with a null latest version and the `pypi.unreachable` warning, and exits 0, because a read-only check must not punish a flaky network

#### Scenario: A dev or uvx install is reported, not special-cased away

- **WHEN** `cwcli axi self-update --check` runs from a dev/editable checkout or an ephemeral uvx invocation
- **THEN** it emits the document carrying that install method and a null upgrade command, and exits 0

### Requirement: The mutating axi self-update verb remains deferred

The system SHALL NOT provide a mutating `cwcli axi self-update` verb in this change.
Only the read-only `--check` form ships.
The deferral SHALL be recorded as deliberate rather than as an oversight, with the reason stated: no rationale for the original deferral was ever recorded, the read half is free, and the mutating half has an agent upgrade the tool it is currently executing from, mid-session, which is the part that plausibly had a real reason.

#### Scenario: Documentation states the split accurately

- **WHEN** a reader consults the project's agent-facing documentation about `self-update`
- **THEN** it states that the read-only `cwcli axi self-update --check` verb exists and that the mutating verb remains deliberately deferred, and it no longer claims `self-update` is human-CLI-only with no axi verb
