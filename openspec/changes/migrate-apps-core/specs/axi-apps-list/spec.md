## ADDED Requirements

### Requirement: cwcli axi apps list emits one TOON listing

The system SHALL provide `cwcli axi apps list <project>` as the agent-facing read verb over the SAME `core.list_apps` the human CLI calls, adding no business logic of its own.
It SHALL emit exactly ONE TOON document on stdout - the serialized `AppsListing` - and SHALL never prompt.
It SHALL accept `--bench` and `--site`, and SHALL NOT accept `--json`: the agent surface is TOON-only.
It SHALL exit 1 when `AppsListing.ok` is false (a site's installed-apps read failed), NOT off `result.status`.

#### Scenario: One document and nothing else

- **WHEN** `cwcli axi apps list <project>` runs
- **THEN** stdout carries exactly one TOON document listing the bench's available apps

#### Scenario: Installed apps per site are answerable

- **WHEN** `cwcli axi apps list <project> --site <site>` runs
- **THEN** the document reports which apps are installed on that site

#### Scenario: A failed site read is honest in the exit code

- **WHEN** the installed-apps read fails for one site
- **THEN** the document marks that site's entry as unreadable and the verb exits 1

### Requirement: axi apps list never prompts and never auto-starts

A decision `core.list_apps` returns as `NEEDS_CHOICE` SHALL be rendered by the verb as a structured TOON usage error with exit 2, never as a prompt.
The verb SHALL NOT carry `--yes`, and SHALL NOT auto-start a stopped container, matching `axi backup`, `axi unlock`, and `axi apps update`.

#### Scenario: A multi-bench project names the discovery verb

- **WHEN** `cwcli axi apps list <project>` targets a multi-bench project with no `--bench`
- **THEN** it emits a TOON usage error naming `--bench`, points at `cwcli axi benches` for the valid values, and exits 2

#### Scenario: A stopped container is a usage error, not a start

- **WHEN** `cwcli axi apps list <project>` targets a project whose container is stopped
- **THEN** it emits a TOON usage error naming `cwcli start <project>`, exits 2, and starts nothing

### Requirement: The apps mutation verbs are deliberately not shipped

`cwcli axi apps install` and `cwcli axi apps uninstall` SHALL NOT be provided by this change.
The deferral is a recorded decision, not an omission: an agent-facing verb that destroys site data requires its own decision on its own evidence, and SHALL NOT be settled as a side effect of a refactor.

#### Scenario: An agent cannot destroy site data through the axi surface

- **WHEN** an agent invokes `cwcli axi apps uninstall`
- **THEN** no such verb exists, and destroying site data remains reachable only through the human `cwcli apps uninstall`
