# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.2] - 2025-10-10

### Changed
- Updated `.gitignore` to exclude `frappe_docker/` directory

## [0.6.1] - 2025-10-04

### Fixed
- `inspect` command now correctly displays available app names without requiring `--verbose` or `--show-apps` flags

## [0.6.0] - 2025-10-04

### Added
- `logs` command: view bench logs in real-time with `tail -f`
  - `--follow/-f` flag to follow logs (default: true)
  - `--no-follow` to show logs and exit
  - `--lines/-n` to specify number of lines to show (default: 100)
  - Logs stored in `/tmp/bench-{project_name}.log` inside container
- Shared console instances across all commands for consistent spinner behavior
  - Created `utils/console.py` with shared `console` and `stderr_console`
  - Fixes spinner artifacts and ensures verbose output is properly buffered

### Changed
- **BREAKING**: Replaced tmux session management with log file approach
  - `start` command now runs bench in background with nohup, logging to file
  - Simpler, more reliable - logs persist across laptop sleep/wake cycles
  - No more tmux dependencies or configuration needed
- Enhanced spinner UX for `start`, `stop`, and `restart` commands
  - Spinners now dynamically update to show which container is being started/stopped
  - Spinner shows bench startup status with log file location
  - All verbose output properly buffered until after spinner exits
- Improved output messages:
  - `start` and `restart` now show log file location and `cwcli logs` usage
  - Removed tmux-specific instructions

### Removed
- Tmux session management and configuration
- Tmux-related keybinding setup and config file creation

## [0.5.0] - 2025-10-03

### Added
- `open` command: `--app`/`-a` option to open a specific app directory within the bench
  - Verifies app exists in cached bench data
  - Shows available apps if requested app not found
  - Opens path at `{bench_path}/apps/{app_name}`

## [0.4.2] - 2025-10-03

### Fixed
- Windows compatibility: VS Code extension commands now work correctly on Windows by using platform-specific shell parameter

## [0.4.1] - 2025-10-03

### Changed
- `open` command now automatically runs `inspect` if no cached bench path is found
- Improved spinner behavior: single persistent spinner that exits before running nested commands
- Fixed spinner conflicts when `open` command calls `inspect` command

### Removed
- Unused `platformdirs` dependency

## [0.4.0] - 2025-10-03

### Added
- `open` command: open Frappe project instances in VS Code dev containers or Docker exec
  - Auto-detects VS Code and VS Code Insiders installations
  - Interactive editor selection with custom styled menu
  - Automatic installation of required VS Code extensions (Docker and Dev Containers)
  - Uses cached bench paths from `inspect` command
  - Spinners and verbose mode for progress feedback
  - Support for cancelling selection with Escape or Ctrl+C
- Enhanced UX with Rich spinners across multiple stages of operations
- Verbose mode (`-v`) shows all executed commands with `$ <command>` prefix

### Changed
- **BREAKING**: Removed bench alias system entirely
  - Removed `--bench`/`-b` global flag
  - Removed `config bench alias` and `config bench unalias` commands
  - Removed `list-apps` command
- **BREAKING**: Commands now use project names (from `cwcli ls`) instead of bench aliases
  - `run` command: `cwcli run <project_name> <bench_commands>`
  - `status` command: `cwcli status <project_name>`
  - `open` command: `cwcli open <project_name>`
- All commands now accept project name as first argument for consistency
- Updated help descriptions to be more concise and high-level
- `open` command automatically retrieves bench path from inspect cache

### Removed
- Bench alias database schema and all related functions
- `list-apps` command (functionality replaced by `inspect` with `--show-apps`)
- Global `--bench`/`-b` option

## [0.3.2] - 2025-08-03

### Added

- Interactive bench naming support for `cwcli inspect` (via `--interactive`, `-i`)
