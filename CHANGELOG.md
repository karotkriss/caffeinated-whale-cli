# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.0] - 2025-11-08

### Added
- **Tab completion support** for project names, apps, and sites across all shells (Bash, Zsh, Fish, PowerShell)
  - Context-aware completions for all commands that accept project names
  - App name completions for `--app` option (open, update commands)
  - Site name completions for `--site` option (unlock command)
  - Fast completion with 2-second TTL caching
  - Install with `cwcli --install-completion`
- **CI/CD workflows** with GitHub Actions
  - Lint workflow: Runs Black, Ruff, and mypy on all branches and PRs
  - Build workflow: Builds package and verifies version consistency on master branch
  - Release workflow: Publishes to PyPI and creates GitHub releases on version tags
  - All workflows use `ghcr.io/astral-sh/uv` Docker images for fast, reproducible builds
- **Comprehensive documentation** in `docs/` directory
  - Contributing guides: git workflow, commit messages, branch naming, code quality, chores, CI/CD
  - Testing guide with examples and best practices
  - Technical documentation for bench management
  - Reorganized into logical categories: contributing, testing, technical

### Changed
- Bumped version to 0.10.0
- Enhanced main README with expanded Contributing section and quick links
- Consolidated CI/CD documentation into minimal `docs/contributing/ci-cd.md` guide

## [0.9.1] - 2025-11-05

### Fixed
- **Critical:** Port conflict detection now properly handles mixed scenarios where ports are held by both Frappe projects and external processes
  - Previously, if port 8000 was owned by a Frappe project and port 8080 by Postgres, stopping the Frappe project would allow the start to proceed, causing Docker to fail on the Postgres port
  - Now splits ports into Frappe-owned vs non-Frappe-owned, handles Frappe conflicts first, then re-checks ALL ports to catch remaining external conflicts
  - Ensures all port conflicts are resolved before allowing container startup
- **Critical:** Pressing Ctrl+C during port conflict prompts now cancels the entire start operation instead of just skipping to the next project
  - Previously, `typer.Exit(code=0)` from KeyboardInterrupt was caught indiscriminately, allowing the loop to continue
  - Now checks exit code: code 0 (user cancellation) propagates to exit entire command, code 1 (port conflicts) skips only the current project
  - Provides proper user control to cancel batch operations

## [0.9.0] - 2025-11-05

### Added
- **Port conflict detection and resolution system**
  - New `utils/port_utils.py` module with comprehensive port management functions
  - `get_project_ports()`: Extract all host ports used by a project's containers
  - `find_project_using_ports()`: Identify which Frappe projects are using specific ports
  - `is_port_in_use()`: Socket-based port availability checking
  - `check_ports_in_use()`: Batch port checking with verbose output option
  - `get_ports_in_use_with_processes()`: Cross-platform process identification (Linux/macOS/Windows)
  - `format_port_list()`: Smart port range formatting (e.g., "8000-8005, 9000")
  - `report_port_conflicts()`: User-friendly conflict reporting
- **Interactive port conflict resolution in `start` command**
  - Automatically detects port conflicts before starting containers
  - Identifies ports used by other Frappe projects vs. external processes
  - Offers to automatically stop conflicting Frappe projects via interactive prompts
  - Groups ports by project/process for cleaner, more readable output
  - Provides actionable error messages for non-Frappe port conflicts
  - Uses `questionary` for user-friendly confirmation prompts
- Cross-platform process identification using platform-specific tools:
  - **Linux/macOS**: `lsof` + `ps` to identify PID and process name
  - **Windows**: `netstat` + `tasklist` for process information
  - Graceful fallbacks when tools are unavailable
  - 2-second timeout protection on all system commands

### Changed
- **Enhanced `start` command workflow**
  - Now performs port conflict checks before attempting to start containers
  - Interactive conflict resolution prevents cryptic Docker port binding errors
  - Shows which projects/processes are blocking required ports
  - Improved error messages with specific guidance for resolution
- **Refactored `commands/utils.py` for better separation of concerns**
  - Removed ~500 lines of port management code (moved to dedicated module)
  - Focused on container lifecycle management only
  - `ensure_containers_running()`: Simplified to check container status without port checks
  - `_start_containers_for_command()`: Now delegates port checks to `start` command
  - Added documentation clarifying when port checks are performed
- **Standardized imports across all command modules**
  - All commands now import from `utils/docker_utils` instead of `commands/utils`
  - Consistent use of new `port_utils` module where needed
  - Updated: `inspect.py`, `logs.py`, `open.py`, `restart.py`, `run.py`, `status.py`, `stop.py`, `unlock.py`, `update.py`
- Enhanced `utils/docker_utils.py` with additional container management utilities
- Improved `utils/vscode_utils.py` with better VS Code integration
- Updated `utils/console.py` to export `stderr_console` for error reporting

### Fixed
- Eliminated "port is already allocated" Docker errors through proactive detection
- Port conflict messages now show process information for better debugging
- Container startup failures due to port conflicts are now prevented, not just reported

## [0.8.0] - 2025-10-12

### Added
- `unlock` command: remove locks folder for a specified site to unlock it
  - `--site/-s` flag to specify site name (required)
  - `--path/-p` flag to specify bench path (uses cached path from inspect by default)
  - `--verbose/-v` flag for streaming rm command output
  - Automatically runs `inspect` if no cached bench path is found
  - Uses `rm -rfv` in verbose mode to show files being removed
  - Helps resolve "document is currently locked" errors

### Changed
- **BREAKING**: `update` command now automatically clears locks for all affected sites after completion
  - Prevents stale locks from causing DocumentLockedError on subsequent runs
  - Clears locks after all operations complete (pull, migrate, build, cache clear)
  - Shows "Clearing locks: {site}" in progress spinner
- Enhanced `update` command reliability:
  - Added proper command completion waiting for streamed commands
  - Added polling loop to ensure ExitCode is available before proceeding
  - Added 0.5s delay after migrations to allow background processes to release locks
  - Increased Live display refresh rate to 20 per second for smoother animations
  - Made progress display transient (disappears after completion)
  - Fixed spinner freezing during blocking operations with manual `live.refresh()` calls

### Fixed
- Spinner animation now continues smoothly during all blocking Docker operations
- Commands properly wait for full completion before moving to next operation
- Lock files are automatically cleaned up, preventing migration failures

## [0.7.0] - 2025-10-10

### Added
- `update` command: update Frappe apps and migrate affected sites
  - `--app/-a` flag to specify apps to update (required, can specify multiple)
  - `--build/-b` flag to rebuild assets after updating apps
  - `--clear-cache/-c` flag to clear cache for all affected sites
  - `--clear-website-cache/-w` flag to clear website cache for all affected sites
  - `--path/-p` flag to specify bench path (uses cached path from inspect by default)
  - `--verbose/-v` flag for detailed output with streaming command execution
  - Automatically runs `inspect` if no cached bench path is found
  - Progress tracking with spinners in non-verbose mode
  - Comprehensive error tracking and reporting for all operations
  - Operations run in sequence: git pull → migrate → build → clear cache → clear website cache
  - Exits with error code 1 if any step fails, with detailed error summary

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
