# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

# <!-- ## [Unreleased] -->
## [Unreleased]

### Added

- Global `--bench`/`-b` option for targeting a bench alias across commands (no project name needed when alias is unique)
- `list-apps` command: new subcommand to display apps for a bench alias
- `config bench alias` and `config bench unalias` commands to assign or clear aliases for bench instances by project and bench path

### Changed

- Default `list-apps` output is now a table of App | Version | Branch | Sites
- Added `--quiet`/`-q` flag to `list-apps` for name-only output suitable for scripting
- Persist interactive aliases immediately to cache in `inspect --interactive`
- Renamed `AvailableApp` table to `available_apps`; renamed join model to `installed_apps` for clarity
- Introduced `InstalledAppDetail` (`installed_apps` table) to store per-site app version and branch

## [0.3.2] - 2025-08-03

### Added

- Interactive bench naming support for `cwcli inspect` (via `--interactive`, `-i`)
