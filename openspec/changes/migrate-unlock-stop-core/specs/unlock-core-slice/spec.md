## ADDED Requirements

### Requirement: core.unlock returns a typed UnlockOutcome built on the existing core primitives

The system SHALL provide `core.unlock(project, *, site=None, bench=None, bench_path=None) -> Result[UnlockOutcome]` that owns everything `commands/unlock.py` performed between "ensure running" and its printed banner: frappe-container resolution, container run-state resolution, bench resolution, default-site resolution, site and bench-path validation, the bench and site existence probes, and the removal of `{bench_path}/sites/{site}/locks`.
`core.unlock` SHALL be built from the primitives the foundation already provides (`core.docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench`, `resolvers.DEFAULT_BENCH_PATH`, `db_utils.get_default_site`, the `Result`/`Choice`/`CwcliError` envelope) WITHOUT adding new primitives or altering existing ones.
`UnlockOutcome` SHALL be a frozen dataclass carrying `site`, `bench_path`, `locks_path`, `removed` (the list of paths the removal reported), and `already_unlocked`.
`core.unlock` SHALL NOT print, prompt, or call `typer.Exit`.

#### Scenario: Unlock removes the locks directory and reports the removed paths

- **WHEN** `core.unlock` runs against a running project whose resolved site has a locks directory
- **THEN** it removes the directory and returns `Result(OK, UnlockOutcome(already_unlocked=False, removed=[...]))` with the removed paths listed and nothing printed

#### Scenario: An absent locks directory is a clean success, not an error

- **WHEN** `core.unlock` runs against a site that has no locks directory
- **THEN** it returns `Result(OK, UnlockOutcome(already_unlocked=True, removed=[]))` and does NOT raise

#### Scenario: A stopped container is a confirm_start choice

- **WHEN** `core.unlock` runs against a project whose frappe container is not running and `auto_start` was not requested
- **THEN** it returns a `confirm_start` `NEEDS_CHOICE` result, starts nothing, and removes nothing

#### Scenario: Multi-bench with no selector is a select_bench choice

- **WHEN** `core.unlock` runs on a multi-bench project with neither `bench` nor `bench_path`
- **THEN** it returns a `select_bench` `NEEDS_CHOICE` result and removes nothing

#### Scenario: The default site is resolved when --site is omitted

- **WHEN** `core.unlock` is called without `site` and the bench has a resolvable default site
- **THEN** it operates on that site and carries a `default_site.resolved` warning in the envelope rather than printing it

#### Scenario: Hard failures raise typed errors

- **WHEN** the project is absent, the bench directory is missing, the site is not found, or the removal fails
- **THEN** `core.unlock` raises `CwcliError` with the matching `ErrorKind` (`NOT_FOUND` for absent project/bench/site, `PRECONDITION` for a failed removal) and never calls `typer.Exit`

### Requirement: The rm -rfv output is buffered into a structured list, not streamed

`core.unlock` SHALL execute the locks removal as a single buffered `exec_run` and SHALL parse the removed paths into `UnlockOutcome.removed`.
The system SHALL NOT introduce streaming or typed-event-iterator machinery for `unlock`.
`cwcli unlock --verbose` SHALL continue to report each removed path to the same stream it uses today, printed from `UnlockOutcome.removed` at completion.

#### Scenario: Verbose reports the removed paths from the structured result

- **WHEN** `cwcli unlock --verbose` succeeds
- **THEN** each removed path is reported, sourced from `UnlockOutcome.removed` rather than from a live output stream

#### Scenario: The agent surface receives the removed paths as data

- **WHEN** `cwcli axi unlock` succeeds
- **THEN** the emitted TOON document carries `removed` as a structured list, not as an opaque blob of command output

### Requirement: Bench-op validation is shared by backup and unlock

The system SHALL extract the shell-metacharacter validation for site names and bench paths, and the bench-directory and site-directory existence probes, into ONE shared helper used by BOTH `core.backup` and `core.unlock`.
The system SHALL NOT duplicate the metacharacter list a second time.
Every container command issued by the shared helper and by `core.unlock` SHALL be passed as an argv list, never as a shell string.

#### Scenario: A site name with shell metacharacters is rejected identically by both verbs

- **WHEN** either `core.backup` or `core.unlock` is given a site name containing a shell metacharacter
- **THEN** it raises `CwcliError(kind=USAGE)` from the shared validation, with both verbs rejecting the same character set

#### Scenario: A shell-significant site name stays one literal argv element

- **WHEN** `core.unlock` builds any container command involving a site name
- **THEN** the site name is a single literal element of an argv list, so it cannot be interpolated as shell syntax regardless of validation

### Requirement: cwcli unlock is a thin frontend over core.unlock

`commands/unlock.py` SHALL call `core.unlock` and SHALL retain only presentation and interaction: the typer signature, `rich` output, the spinner, and the resolution of `NEEDS_CHOICE` results via the existing CLI wrappers.
The command's flags, messages, and exit codes SHALL be preserved.

#### Scenario: Interactive multi-bench selection is resolved by the frontend

- **WHEN** interactive `cwcli unlock` receives a `select_bench` `NEEDS_CHOICE` from the core
- **THEN** the frontend prompts for the bench and re-invokes `core.unlock` with the selection, and the core itself never prompts

#### Scenario: Preserved exit codes

- **WHEN** `core.unlock` raises a `CwcliError`
- **THEN** the frontend renders it in the historical message form and exits with the code the command used before the migration

### Requirement: cwcli axi unlock is non-interactive and structured

The system SHALL provide `cwcli axi unlock <project> [--site] [--bench]` on the existing `cwcli axi` serializer and exit-code mapper.
It SHALL NOT prompt and SHALL NOT auto-start a stopped container: a `NEEDS_CHOICE` result SHALL be rendered as a structured usage error naming the flag (or, for a stopped container, the `cwcli start` command) that resolves it, with exit code 2.
It SHALL emit exactly one TOON document on stdout.
It deliberately carries NO `--yes` flag, matching `cwcli axi backup`'s established convention exactly: adding one would open a new start-from-axi path that diverges from its twin, and a structured error pointing at `cwcli start` already meets the non-interactive, never-starts-anything intent.

#### Scenario: Multi-bench without a selector is a usage error naming the flag

- **WHEN** `cwcli axi unlock` runs on a multi-bench project with no `--bench`
- **THEN** it emits a structured error naming `--bench`, lists the valid benches, and exits 2 without prompting

#### Scenario: A stopped container is a structured error pointing at cwcli start

- **WHEN** `cwcli axi unlock` runs against a project whose container is stopped
- **THEN** it emits a structured error with a `help` line directing the agent to run `cwcli start <project>` first, and exits non-zero without prompting or starting anything

### Requirement: unlock is E2E-verified in both interactive and non-interactive modes

The system SHALL provide a real-Docker E2E for `unlock` covering BOTH modes: non-interactive (flags supplied, stdin closed, no prompt, runs to completion) and interactive (pty-driven, the prompt genuinely shown and genuinely collecting input).
The E2E SHALL assert a real outcome against a real instance, not a string match on output.

#### Scenario: Non-interactive unlock runs to completion with no prompt

- **WHEN** `cwcli unlock <project> --site <site> --yes` runs with stdin closed against a real instance whose site has a locks directory
- **THEN** it exits 0 with no prompt and the locks directory is genuinely gone from the container

#### Scenario: Interactive unlock genuinely prompts

- **WHEN** `cwcli unlock <project>` runs under a pty against a real instance with a stopped container
- **THEN** the auto-start confirm is genuinely displayed and genuinely awaits input rather than being skipped or auto-answered
