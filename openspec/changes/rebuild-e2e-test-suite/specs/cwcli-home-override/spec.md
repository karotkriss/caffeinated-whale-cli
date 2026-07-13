## ADDED Requirements

### Requirement: CWCLI_HOME relocates cwcli's home directory
The system SHALL honor a `CWCLI_HOME` environment variable that, when set, is used verbatim as the base directory for cwcli's entire on-disk footprint in place of `~/.cwcli`.
The config directory SHALL resolve to `$CWCLI_HOME/config`, the projects directory to `$CWCLI_HOME/projects`, and the cache database to `$CWCLI_HOME/cache/cwc-cache.db`.
When `CWCLI_HOME` is unset or empty, the base directory SHALL remain `Path.home() / ".cwcli"`, so existing installs are unaffected.
The base directory SHALL be resolved through a single shared helper, so `config_utils` and `db_utils` cannot diverge on where cwcli's state lives.
The relocated directories SHALL be created with the same restrictive permissions as today (cache directory `0700`, cache DB file `0600`).

#### Scenario: CWCLI_HOME set redirects all of cwcli's state
- **WHEN** `CWCLI_HOME=/tmp/cwe2e-home-1` is set and any cwcli command reads or writes its config, projects registry, or cache
- **THEN** cwcli uses `/tmp/cwe2e-home-1/config`, `/tmp/cwe2e-home-1/projects`, and `/tmp/cwe2e-home-1/cache/cwc-cache.db`, and never touches `~/.cwcli`

#### Scenario: CWCLI_HOME unset keeps the default location
- **WHEN** `CWCLI_HOME` is unset (or empty) and a cwcli command runs
- **THEN** cwcli uses `~/.cwcli/config`, `~/.cwcli/projects`, and `~/.cwcli/cache/cwc-cache.db` exactly as before

#### Scenario: relocated cache keeps secure permissions
- **WHEN** `CWCLI_HOME` points at a fresh directory and cwcli first creates its cache
- **THEN** the cache directory is created `0700` and the cache DB file `0600`, identical to the default location's hardening

### Requirement: CWCLI_HOME does not repoint the process HOME
Setting `CWCLI_HOME` SHALL affect ONLY cwcli's own on-disk paths.
It SHALL NOT change the process `HOME` environment variable, so host-side tooling that derives its own state from `HOME` (git config, ssh, shell) is unaffected by redirecting cwcli's state.

#### Scenario: redirecting cwcli state leaves HOME-derived tooling alone
- **WHEN** `CWCLI_HOME` is set to a temporary directory but `HOME` is left at the user's real home
- **THEN** cwcli reads and writes only under `$CWCLI_HOME`, while any host tool that reads `HOME` (e.g. git, ssh) still resolves to the user's real home unchanged
