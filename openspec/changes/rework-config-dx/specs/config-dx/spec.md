## ADDED Requirements

### Requirement: config show answers "what is my config?" in one shot

The system SHALL provide `cwcli config show [--json]` rendering the effective configuration in one invocation: the custom search paths, the auto-inspect settings together with live daemon state and boot-hook state, the tips setting, and the config-file and cache-DB locations.
`--json` SHALL emit a stable machine-readable object on stdout via plain `print` (never a width-wrapping console), derived from `core.config.show_config()`'s `ConfigReport` DTO.

#### Scenario: A fresh install shows the defaults

- **WHEN** `cwcli config show` runs against a fresh `CWCLI_HOME`
- **THEN** it exits 0 and displays the default search paths (empty), auto-inspect disabled with interval 3600, daemon not running, boot hook not installed, tips enabled, and both file locations

#### Scenario: The JSON form is machine-parseable

- **WHEN** `cwcli config show --json` runs
- **THEN** stdout is exactly one JSON object containing `search_paths`, `auto_inspect` (with `enabled`, `interval`, `startup_enabled`, `daemon_running`, `daemon_pid`, `boot_installed`), `show_tips`, `config_file`, and `cache_db`, with no rich markup and no prose

### Requirement: Search paths become a noun group with a read and with input validation

The system SHALL provide `cwcli config paths [--json]` (list), `cwcli config paths add <path>`, and `cwcli config paths remove <path>`.
`add` SHALL refuse a non-absolute path with a usage error (exit 2) after `~` expansion, and SHALL normalize (trailing slash, redundant separators) before duplicate detection.
`remove` SHALL normalize before matching.
Adding an already-present path and removing an absent path SHALL both exit 0 (idempotent), saying which no-op occurred.

#### Scenario: A relative path is refused before anything is written

- **WHEN** `cwcli config paths add not/absolute/../weird` runs
- **THEN** it exits 2 with a usage error naming the absolute-path requirement, and the config file is unchanged

#### Scenario: A trailing-slash variant is the same path

- **WHEN** `/a/b` is already configured and `cwcli config paths add /a/b/` runs
- **THEN** it exits 0 reporting the path is already present, and the config file still contains exactly one entry for it

#### Scenario: The empty list is definitive

- **WHEN** `cwcli config paths` runs with no custom paths configured
- **THEN** it exits 0 and states explicitly that zero custom search paths are configured

### Requirement: auto-inspect enable is atomic and reaches the desired state in one verb

`cwcli config auto-inspect enable [--interval N] [--startup/--no-startup]` SHALL validate every input BEFORE persisting anything, then write the config once, start the daemon, and sync the boot hook when requested.
A failing invocation SHALL leave the config file byte-identical to its pre-invocation state.
Re-running `enable` SHALL be idempotent: it applies changed settings (restarting the running daemon when the interval changed) and reports already-satisfied state as a no-op, exit 0.

#### Scenario: An invalid interval mutates nothing

- **WHEN** `cwcli config auto-inspect enable --interval 30` runs with auto-inspect disabled
- **THEN** it exits non-zero naming the 60-second minimum, `enabled` remains `false` in the config file, and no daemon is started

#### Scenario: One verb reaches running

- **WHEN** `cwcli config auto-inspect enable` runs with a valid config
- **THEN** it exits 0 with the daemon running, and no second command is required or suggested

### Requirement: auto-inspect disable and stop are distinct, honest verbs

`cwcli config auto-inspect disable` SHALL stop the daemon, set `enabled = false`, and remove the boot hook, reporting each action it actually took.
`cwcli config auto-inspect stop` SHALL stop the daemon only, leaving `enabled` and the boot hook untouched.
Both SHALL be idempotent: acting on an already-reached state exits 0 with a no-op message.

#### Scenario: disable tears down all three stores

- **WHEN** auto-inspect is enabled, running, and boot-hooked, and `cwcli config auto-inspect disable` runs
- **THEN** it exits 0, the daemon is stopped, `enabled` is `false`, the boot hook is removed, and all three actions are reported

#### Scenario: stop preserves persistence

- **WHEN** auto-inspect is enabled with a boot hook and `cwcli config auto-inspect stop` runs
- **THEN** the daemon stops, `enabled` stays `true`, the boot hook stays installed, and `status` reflects exactly that mix

### Requirement: cache clear refuses contradictory or missing targets

`cwcli config cache clear` SHALL treat a project name combined with `--all` as a usage error (exit 2, nothing cleared), and no target at all as a usage error (exit 2).
`--all` SHALL keep its confirmation: prompted on a TTY, refused with exit 1 on a non-TTY without `--yes`.
Clearing a project with no cache entry SHALL remain exit 0 (idempotent).

#### Scenario: A project name plus --all clears nothing

- **WHEN** `cwcli config cache clear myproject --all --yes` runs with a populated cache
- **THEN** it exits 2 with a usage error naming the conflict, and every cache entry survives

#### Scenario: Non-TTY without consent still refuses

- **WHEN** `cwcli config cache clear --all` runs with stdin not a TTY and no `--yes`
- **THEN** it exits 1 with the refusal naming `--yes`, and the cache is untouched

### Requirement: Deprecated verbs are frozen aliases, hidden, warning on stderr

`add-path`, `remove-path`, `auto-inspect start`, `auto-inspect restart`, `auto-inspect set-interval`, `auto-inspect install-startup`, `auto-inspect uninstall-startup`, and `tips status` SHALL remain registered with byte-identical behavior and exit codes, hidden from `--help`, each emitting a single stderr deprecation line naming its replacement.
The one exception: `add-path`/`remove-path` SHALL apply the same input validation as `paths add`/`paths remove`.
`auto-inspect start` SHALL keep refusing (exit 1) when auto-inspect is disabled, because installed boot units exec it verbatim and rely on that guard to keep a stale hook inert.

#### Scenario: An installed boot unit still works

- **WHEN** a systemd/launchd/schtasks unit created before this change execs `cwcli config auto-inspect start` while auto-inspect is enabled
- **THEN** the daemon starts exactly as before, with only a stderr deprecation line added

#### Scenario: The stale-hook guard survives

- **WHEN** `cwcli config auto-inspect start` runs while auto-inspect is disabled
- **THEN** it exits 1 with the refusal, and no daemon starts

#### Scenario: Warnings never touch stdout

- **WHEN** any frozen alias runs successfully
- **THEN** its stdout is byte-identical to the pre-change output, and the deprecation line appears only on stderr

### Requirement: Reads are machine-readable and script-safe

`cwcli config cache list` and `cwcli config auto-inspect status` SHALL accept `--json`.
`cwcli config path` and `cwcli config cache path` SHALL print the bare path and nothing else, unwrapped at any terminal width.
`cwcli config auto-inspect logs` SHALL exit 1 when the log read fails.

#### Scenario: The path verb is substitution-safe

- **WHEN** `p=$(cwcli config path)` runs in a narrow terminal
- **THEN** `$p` is the exact config-file path with no prose and no line break

#### Scenario: A failed log read is an error

- **WHEN** `cwcli config auto-inspect logs` cannot read the log file for a reason other than "no log yet"
- **THEN** it exits 1 with the error on stderr

### Requirement: The logic lives in the UI-pure core

The system SHALL provide `core/config.py` (`show_config`, `add_search_path`, `remove_search_path`, `set_tips`, `cached_projects`, `clear_cache`) and `core/auto_inspect.py` (`enable`, `disable`, `stop`, `status`, `log_tail`), importing no `rich`, `questionary`, or `typer`, returning `Result[T]` envelopes or raising typed `CwcliError`s, with all DTOs plain data under `dataclasses.asdict`.
`clear_cache(all_projects=True, consent=False)` SHALL return `NEEDS_CHOICE` (`confirm_clear`); consent SHALL be a core parameter, with `--yes` remaining one frontend's spelling.
`commands/config.py` SHALL thin to a renderer over these functions, except the frozen aliases, which deliberately bypass the core.

#### Scenario: The core is silent and pure

- **WHEN** any `core.config` or `core.auto_inspect` function runs without a callback
- **THEN** nothing is written to stdout or stderr, and `tests/test_core_envelope.py`'s import ban covers both modules automatically

#### Scenario: The destructive fork is a returned choice

- **WHEN** `core.config.clear_cache(all_projects=True, consent=False)` is called
- **THEN** it returns `NEEDS_CHOICE` with kind `confirm_clear`, and nothing is deleted

### Requirement: One read-only axi verb

The system SHALL provide `cwcli axi config`, emitting the `ConfigReport` as one TOON document (exit 0; internal failures exit 1 as a structured `error:` line).
It SHALL take no mutating flags, and the `axi` registry SHALL carry no config-mutating verbs; a test SHALL assert both so the deferral cannot be misread as an omission.

#### Scenario: An agent reads the effective config in one call

- **WHEN** `cwcli axi config` runs
- **THEN** stdout is exactly one TOON document carrying search paths, auto-inspect state (settings, daemon, boot), tips, and file locations, and the exit code is 0

#### Scenario: No mutating config verbs exist on the agent surface

- **WHEN** the `axi` Typer registry is inspected
- **THEN** `config` is present and no verb mutating configuration, search paths, or the cache is registered
