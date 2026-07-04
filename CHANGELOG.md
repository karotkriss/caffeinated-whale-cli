# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.33.0] - 2026-07-04

### Added
- **`restore` command** - `--yes`/`-y` flag to skip the confirmation prompt in receive mode, for non-interactive use

### Changed
- **`restore` command** - Receive mode (`--receive`) now warns and asks for confirmation before the destructive `bench restore --force` that replaces all data in the target site
  - Refuses to proceed in a non-interactive session unless `--yes` is passed, instead of restoring silently
  - Warns when the backup's origin site does not match the site being restored into
  - Streams the backup into the container instead of buffering it in host memory, so large `--with-files` backups no longer spike memory
- **`rm` command** - Refuses to delete a project's named volumes and databases unless a verified, non-empty backup has landed on the host; pass `--no-backup` to remove without one
  - Streams each backup artifact out of the container instead of buffering it in host memory, so large `--with-files` backups no longer spike memory

### Fixed
- **`restore` command** - Database and admin passwords are no longer exposed on the container process list during a restore
  - A failed copy of the backup into the container now fails clearly instead of surfacing a confusing "backup not found" later
- **`rm` command** - Validates the project name before any deletion, rejecting empty, `.`, `..`, absolute, or path-separator names that could escape the projects directory
  - Returns a non-zero exit code when a removal only partially succeeds, instead of reporting success

## [0.32.0] - 2026-07-02

### Added
- **`inspect` command** - `--no-refresh` flag to return cached data as-is, skipping the freshness pass entirely

### Changed
- **`inspect` command** - 3-tier freshness model (cache / partial / full) so cached reads stay fast but no longer go stale
  - By default, a cheap read-only pass re-checks the apps and sites of known benches and escalates to a full re-inspect only when they drift from the cache, so a freshly installed app now shows up without `--update`
  - Serves cached data directly when the project's containers are not running, without prompting to start them
  - No longer rewrites the cache on every read; the cache is only written when a full inspect runs

### Fixed
- **`open` command** - `--app` now sees freshly installed apps instead of failing with "App not found" on a stale cache, and matches apps against the selected bench rather than the first one
- **`rm` command** - Removal now actually deletes the project's named Docker volumes (e.g. databases) and the local project directory instead of silently leaving them behind
  - Archives only `conf/` (the generated compose config) before deletion, so dangling bench symlinks can no longer abort the archive and block removal
  - Cleans up orphaned projects whose containers are already gone, still archiving config and removing leftover volumes and the project directory
  - No longer hangs on a stopped project: the pre-removal recache is skipped with a warning instead of deadlocking on a hidden prompt
  - Accepts trailing flags after the project name (e.g. `cwcli rm myproject --yes`)
- **`init` command** - Declining to reuse an existing bench now prompts for a different bench name and continues setup, instead of aborting the whole command

## [0.31.1] - 2026-06-24

### Fixed
- **`init` command** - Frappe `version-14` compatibility
  - Uses `--no-mariadb-socket` for `bench new-site` (matching `version-13`), replacing `--mariadb-user-host-login-scope=%` which is only supported in bench/Frappe 15+

## [0.31.0] - 2026-03-08

### Changed
- **`init` command** - Removed automatic default site setting after init
  - `bench use <site>` is no longer called after site creation
  - `bench set-config developer_mode 1` now explicitly targets the site with `--site <site_name>` instead of relying on a default
  - `bench set-config -g server_script_enabled 1` is unaffected (global config)

## [0.30.0] - 2026-03-06

### Fixed
- **`init` command** - `version-13` compatibility fixes
  - Uses `--no-mariadb-socket` for `bench new-site` (replaces `--mariadb-user-host-login-scope=%` which is not supported in older bench versions)
  - Pins `setuptools<82` immediately after `bench init` to retain `pkg_resources` compatibility
  - Installs `yarn` globally (`npm install -g yarn`) after the correct Node.js version is activated via nvm

### Improved
- **`init` command** - Clearer error message when the container runs out of disk space (`ENOSPC`), prompting the user to free up space rather than showing a generic exit-code error



### Changed
- **`init` command** - Bench image resolved from Docker Hub at runtime
  - Queries Docker Hub API for the latest stable semver tag of `frappe/bench` (e.g. `v5.29.1`)
  - Never uses the `:latest` tag — always pins to a specific version for reproducible builds
  - Falls back to a known-good version (`v5.29.1`) if the API is unreachable
  - Applies to all branches, not just `version-15`
  - Verbose mode logs the resolved tag and Docker Hub lookup progress

- **`init` command** - Automatic Python version pinning per Frappe branch
  - `version-15`: Uses `PYENV_VERSION=3.12.x`
  - `version-14`: Uses `PYENV_VERSION=3.10.x`
  - `version-13`: Uses `PYENV_VERSION=3.9.x`
  - Checks `~/.pyenv/versions` inside the container for installed versions
  - Automatically installs the correct Python version via `pyenv install` if not found
  - Verbose mode logs each step (discovery, installation, version used)

- **`init` command** - Automatic Node.js version pinning per Frappe branch
  - `version-14`: Uses Node.js 16 via `nvm use`
  - `version-13`: Uses Node.js 14 via `nvm use`
  - Checks `~/.nvm/versions/node/` inside the container for installed versions
  - Automatically installs the correct Node.js version via `nvm install` if not found
  - Verbose mode logs each step (discovery, installation, version used)

## [0.28.0] - 2026-01-29

### Added
- **`where` command** - Search all cached instances for apps or sites by name
  - Case-insensitive partial string matching across all projects
  - `--apps/-a` flag to search only for apps
  - `--sites/-s` flag to search only for sites
  - `--installed/-i` flag to show only installed apps (excludes available-but-not-installed)
  - `--json` flag for JSON output (useful for scripting)
  - Displays results in formatted tables with project, app/site name, version, and branch
  - Deduplicates results (prefers installed apps over available apps when both exist)
  - Examples: `cwcli where erpnext`, `cwcli where payments --apps`, `cwcli where .local --sites`

## [0.27.0] - 2026-01-21

### Added
- **`rm` command** - Remove (delete) Frappe projects with automatic backups
  - **Re-caches project before removal** to get accurate site information
  - **Creates database backups for all sites** before removal
  - **Archives project configuration** to `~/.cwcli/archive/{project_name}_{timestamp}/`
    - Backs up all site databases (with files)
    - Archives docker-compose.yml
    - Archives site_config.json for all sites
    - Includes archive metadata (project name, timestamp, bench path)
  - Stops and removes all containers for a project
  - **Removes Docker volumes by default** (complete removal)
  - Requires user confirmation before proceeding (destructive action)
  - `--no-volumes` flag to preserve volumes and keep data
  - `--no-backup` flag to skip backups and recaching (not recommended)
  - `--yes/-y` flag to skip confirmation prompt
  - Supports multiple projects via arguments or piped input
  - Clears project cache after removal
  - Verbose mode shows detailed removal, backup, and archive progress
  - Clear warning messages for data-destructive operations
  - Examples: `cwcli rm my-project`, `cwcli rm my-project --no-volumes`

## [0.26.0] - 2026-01-20

### Added
- **`restore` command** - Missing app detection before restore
  - Checks if apps from backup site are available on the target bench
  - Re-caches project before check to ensure accuracy
  - Displays warning with list of missing apps
  - Prompts for confirmation before proceeding with potentially incomplete restore
  - Provides helpful install instructions for missing apps
  - Applies to both local and remote (--receive) restore modes
  - New `--no-recache` flag to skip re-caching and use existing cache
- **`update` command** - Re-cache after app updates
  - Automatically re-caches project after apps are updated via git pull
  - Ensures site detection uses fresh cache data before migrations
  - Improves accuracy when finding which sites have updated apps installed
  - Verbose mode shows re-cache progress
  - New `--no-recache` flag to skip re-caching and use existing cache
- **`utils.cache.recache_project()`** - New utility function
  - Clears cache and re-runs inspect for a project
  - Ensures fresh, trustworthy cache data
  - Reusable for any future operations requiring up-to-date cache
  - Located in utils/cache.py for cache management operations

## [0.25.2] - 2026-01-20

### Fixed
- **`restore` command** - Auto-inspect when no cache found
  - Automatically runs inspect if no cached bench path or default site is found
  - Matches behavior of other commands (open, update, unlock)
  - Eliminates manual `cwcli inspect` step before restore
- **`restore` command** - Remote restore always available
  - "Restore from remote source (via sendme)" option now shown even when no local backups exist
  - Displays helpful "No local backups found" separator when applicable
  - Allows restoring from P2P transfers without requiring local backups first

## [0.25.1] - 2026-01-20

### Fixed
- **`restore --receive` command** - Encryption key update actually works now
  - Fixed heredoc implementation to properly write JSON without mangling
  - Previously reported success but failed to update encryption_key in site_config.json
  - Now matches the working implementation in main restore command

## [0.25.0] - 2026-01-08

### Added
- **`open` command** - Cursor editor support
  - New `--cursor` flag to open directly in Cursor (skips interactive prompt)
  - Cursor automatically detected and shown in interactive editor selection menu
  - Uses same Dev Containers integration as VS Code
  - Installation validation with helpful error messages pointing to https://cursor.sh/
  - Dynamic editor name display in status messages

## [0.24.0] - 2026-01-08

### Fixed
- **`init` command** - Frappe Docker image selection based on branch
  - Only pins Frappe bench image to `v5.26.0` for `version-15` branch to ensure stability
  - Uses latest image tag for `develop` branch to get the most current features
  - Prevents compatibility issues between branch versions and Docker images

## [0.23.1] - 2026-01-06

### Fixed
- **`restore --receive` command** - Resilient ticket input handling
  - Automatically strips all whitespace (newlines, tabs, spaces) from pasted sendme tickets
  - Handles janky copy-paste from terminal output with carriage returns and formatting
  - Users can now copy the entire output without carefully selecting just the ticket text

## [0.23.0] - 2026-01-05

### Added
- **`update` command** - Automatic maintenance mode management (opt-out)
  - Sites automatically enter maintenance mode before migrations to prevent user access during updates
  - Maintenance mode only enabled for affected sites (not entire bench)
  - Guaranteed cleanup via try-finally error handling - sites never stuck in maintenance mode even on failures
  - `--skip-maintenance` flag to disable maintenance mode if needed for operational hours
  - Per-site status messages in verbose mode matching other command output styles

### Changed
- **`update` command** - Performance optimization with database cache integration
  - Now uses `db_utils.get_cached_project_data()` for site-app lookups instead of repeated `bench list-apps` calls
  - O(n) performance with cache vs O(n×m) without cache
  - Graceful fallback to live queries if cache unavailable
  - Cache hit/miss logging in verbose mode
  - Recommendation: Run `cwcli inspect <project>` before `cwcli update` for best performance

### Fixed
- **`update` command** - Robust error handling prevents sites from being stuck in maintenance mode
  - Try-finally block ensures maintenance mode cleanup even on migration failures
  - Build failures, cache clearing failures, or exceptions no longer leave sites in maintenance mode
  - User interrupts (Ctrl+C) properly trigger maintenance mode cleanup
  - Inner try-except in finally block prevents cleanup errors from masking original errors
  - Consistent post-migration lock release delay (0.5s) in both verbose and non-verbose modes

## [0.22.0] - 2026-01-05

### Changed
- **`init` command** - Updated Frappe bench image tag from `latest` to `v5.26.0` for more predictable and stable deployments
  - Ensures consistent container behavior across different initialization times
  - Prevents unexpected changes from automatic image updates

## [0.21.1] - 2024-12-04

### Fixed
- **Auto-inspect service** - Now runs in update mode to fetch fresh data from running containers instead of only refreshing from cached data

## [0.21.0] - 2025-11-23

### Added
- **`init` command** - Initialize a complete Frappe development environment in a single step
  - Creates project directory structure in `~/.cwcli/projects/{project_name}/`
  - Downloads docker-compose.yml from frappe_docker GitHub repository
  - Pulls Docker images and starts containers automatically
  - Initializes Frappe bench inside the container
  - Creates a new site with configurable credentials
  - Optionally installs ERPNext with `--install-erpnext` flag
  - Custom port selection with `--port/-P` flag (creates ports {port}-{port+5} for web, {port+1000}-{port+1005} for socketio)
  - Port conflict detection before starting containers
  - Interactive prompts if project name not provided
  - Configurable Frappe/ERPNext branches with `--frappe-branch` and `--erpnext-branch`
  - Progress feedback with TipSpinner during long-running operations
  - Automatic bench path registration for `cwcli open` compatibility
  - Reuse existing bench with confirmation prompt
  - Elapsed time display on completion
  - Usage: `cwcli init <project_name> [--port 8000] [--install-erpnext]`

## [0.20.0] - 2025-11-16

### Changed
- **Home directory location** - Moved from `~/caffeinated-whale-cli/` to `~/.cwcli/` (hidden directory)
  - Config files now in `~/.cwcli/config/`
  - Cache database now in `~/.cwcli/cache/`
  - Auto-inspect PID/logs now in `~/.cwcli/run/`
  - Service names updated: `com.cwcli.auto-inspect` (macOS), `cwcli-auto-inspect.service` (Linux)
  - Log files updated: `/tmp/cwcli-auto-inspect.log` (macOS/Linux)
  - Follows standard Unix convention for hidden config directories

### Fixed
- Type annotations in completion utilities (added `| None` to optional parameters)

## [0.19.0] - 2025-01-15

### Added
- **P2P Backup Transfer via sendme** - Share and receive backups between machines using peer-to-peer connections
  - `cwcli restore <project> --send` - Share backup with remote machine via sendme
    - Interactive backup selection menu
    - Automatic sendme binary installation and management
    - Ticket automatically copied to clipboard for easy sharing
    - Multi-file transfer support (database, public files, private files, config)
    - Cross-platform support (macOS, Linux, Windows)
  - `cwcli restore <project> --receive` - Receive backup from remote machine
    - Ticket input prompt for receiving transfers
    - Automatic file download and verification (BLAKE3 hash-verified)
    - Files automatically copied to container's backup directory
    - Seamless integration with standard restore process
  - New `sendme_utils.py` module for sendme binary management
    - Platform detection (darwin-aarch64, darwin-x86_64, linux-x86_64, windows-x86_64)
    - Automatic binary download with progress bars
    - PATH configuration for Unix and Windows
    - Clipboard integration (pbcopy, xclip, xsel, clip)
  - Hash-verified transfers using BLAKE3 for data integrity
  - NAT traversal with automatic relay fallback
  - Resumable transfers (interrupted downloads can resume)
  - Support for multiple simultaneous receivers from one ticket

### Changed
- **Enhanced restore command** - Added `--send` and `--receive` modes for P2P transfers
- **Improved restore reliability** - Added `--force` flag to bypass Frappe version check prompts in non-interactive mode
- **Fixed file path handling** - File archives now use absolute paths for reliable restore operations

### Fixed
- **Terminal formatting issues** - Resolved escape code conflicts from sendme output
  - Added cursor position resets before interactive prompts
  - Isolated sendme process with `start_new_session=True` to prevent terminal state corruption
  - Simplified output to show only ticket copy confirmation instead of full ticket string
- **File archive paths** - Fixed "Invalid path" error by using full paths for `--with-public-files` and `--with-private-files`
- **Version mismatch prompts** - Added `--force` flag to restore command to handle version differences non-interactively

### Documentation
- Added comprehensive sendme CLI reference (`docs/technical/sendme-cli-reference.md`)
- Added Iroh blobs protocol security explanation (`docs/technical/iroh-blobs-and-sendme.md`)
- Updated Frappe backup/restore reference with real-world examples

## [0.15.0] - 2025-11-12

### Added
- **`restore` command** - Interactive site restoration from backups
  - Scans all backup files across all sites in the bench
  - Interactive backup selection menu with styled UI
  - Backups grouped by target site (shown first) and other sites
  - Automatic detection of file archives (public and private files)
  - Visual badges showing backup contents: `[FILES]`, `[PRIVATE]`, `[DATABASE ONLY]`
  - Automatic encryption key restoration from backup site_config
  - Secure password prompts using questionary library
  - TipSpinner integration for enhanced developer experience
  - Supports default site from common_site_config.json
  - Comprehensive input validation and error handling
  - Usage: `cwcli restore <project> [--site <site>]`

### Security
- **Command injection prevention in restore command**
  - Validates passwords don't contain single quotes (shell breaking)
  - Validates MariaDB username for shell metacharacters
  - Validates backup file paths before restore
  - Verifies backup files exist before attempting restore
  - Site name and bench path validation (prevents traversal attacks)
  - Password masking in verbose output

### Changed
- **Enhanced error messages for restore failures**
  - Lists common failure causes (incorrect password, connection issues, etc.)
  - Reminds users to use `-v` flag for detailed diagnostics
  - Shows restore output on failure for better debugging
  - JSON parsing errors handled gracefully with descriptive messages

## [0.14.0] - 2025-11-10

### Added
- **Site configuration caching** - Inspect command now caches site and bench configurations
  - `common_site_config.json` cached for each bench (includes Redis URLs, ports, worker settings)
  - `site_config.json` cached for each site (includes database credentials, developer mode)
  - New database tables: `common_site_config` and `site_config`
  - Helper functions to retrieve cached configs: `get_common_site_config()`, `get_site_config()`, `get_all_site_configs()`, `get_default_site()`
  - Configs are automatically fetched and stored during `cwcli inspect`
  - JSON output includes configs for programmatic access
  - Default site labeled with `(default)` in inspect output
  - Empty configs `{}` now properly preserved (distinguishes from missing configs)
- **Default site support** - `--site` flag now optional when default site is configured
  - `unlock` command automatically uses default site from `common_site_config.json`
  - Shows "Using default site: {site}" when using default
  - Helpful error messages when no default site available
  - Backward compatible: explicit `--site` flag still works

### Security
- **Filesystem permissions for sensitive cache data**
  - Cache directory created with restrictive permissions (`0700` - owner-only access)
  - Database file secured with `0600` permissions (owner read/write only)
  - Prevents unauthorized access to cached credentials and API keys
  - Security warnings added to model documentation
  - Comprehensive test suite validates permission enforcement
  - Note: Data is stored in plaintext; future enhancement will add field-level encryption
- **Command injection prevention in unlock command**
  - Input validation for site names and bench paths
  - Rejects shell metacharacters (`;`, `&`, `|`, `$`, etc.)
  - Prevents path traversal and injection attacks
  - Clear error messages for security violations

### Changed
- **Config validation improvements**
  - Empty dicts `{}` now accepted as valid configurations
  - Removed redundant JSON re-parsing in validation
  - Simplified storage checks to use `is not None` instead of truthiness
  - Better distinction between "no config" (None) and "empty config" ({})
- **Enhanced error handling in unlock command**
  - Try/except with proper exception chaining for default site retrieval
  - Validates site names are not empty or whitespace-only
  - Actionable error messages with tips for resolution

### Fixed
- **Config retrieval robustness**
  - `get_common_site_config()` now iterates all benches instead of just first
  - Returns config from first bench that has one (not just first bench)
  - Preserves empty configs throughout entire data pipeline
- **Code consistency**
  - Refactored `_get_common_site_config()` and `_get_site_config()` to use `_run_command` helper
  - Centralized verbose logging and command execution
  - Fixed unused variable warning in unlock command

## [0.13.1] - 2025-11-10

- **Contextual tips during long-running operations** - Inspired by Claude Code's tip system
  - Rotating helpful tips displayed alongside spinners during operations like `inspect`, `update`, and `open`
  - Tips help users discover features and best practices while waiting
  - 40+ curated tips covering VS Code integration, tab completion, caching, port management, and more
  - Tips rotate every 4 seconds during long operations to show variety
  - New `cwcli config tips` command group to manage tip display
    - `enable` - Enable contextual tips (default)
    - `disable` - Disable tips for simpler status messages
    - `status` - Check current tips display setting
  - Configurable via `show_tips` setting in config.toml (default: true)
  - Tips integrated into:
    - `inspect` command during project inspection
    - `open` command when preparing VS Code integration
    - Extension installation and container verification steps
  - TipSpinner context manager supports reuse across multiple operations

## [0.12.1] - 2025-11-10

### Added
- **Editor selection flags for `open` command** - Skip interactive prompt with direct flags
  - `--code` - Open directly with VS Code (validates installation)
  - `--code-insiders` - Open directly with VS Code Insiders (validates installation)
  - `--docker` - Open directly with Docker exec
  - Mutual exclusivity validation ensures only one flag can be specified
  - Backward compatible: interactive prompt still appears when no flag is specified

### Fixed
- **Docker exec now respects working directory** - `open` command with `--docker` or Docker selection
  - Container shell now opens in bench directory instead of container default
  - Respects `--app` flag to open in specific app directory
  - Uses Docker's `-w` flag to set working directory on exec

### Changed
- Moved `exec_into_container` function from `vscode_utils.py` to `docker_utils.py` for better organization

## [0.11.0] - 2025-11-09

### Added
- **Automatic project inspection** - Background service to keep project cache fresh
  - New `cwcli config auto-inspect` command group with full management suite
  - `enable` - Enable auto-inspection with configurable interval (default: 1 hour, minimum: 60 seconds)
  - `disable` - Disable auto-inspection and stop background process
  - `start` - Start the background daemon process
  - `stop` - Stop the background daemon process
  - `restart` - Restart the background process
  - `status` - Show detailed status (enabled, interval, process state, PID, startup configuration)
  - `logs` - View recent background process logs with `--lines` option
  - `set-interval` - Change inspection interval (requires restart to apply)
  - Cross-platform daemon support: fork (Unix/Linux/macOS) and threading (Windows)
  - Automatic inspection of all running Frappe projects at configured intervals
  - Background logging to `~/caffeinated-whale-cli/run/auto-inspect.log`
  - PID tracking in `~/caffeinated-whale-cli/run/auto-inspect.pid`

- **Automatic startup on system boot/login** - Platform-specific system integration
  - `install-startup` - Install platform-specific startup configuration
  - `uninstall-startup` - Remove startup configuration
  - `--startup` flag for `enable` and `start` commands to enable startup in one step
  - **macOS**: LaunchAgent plist file (`~/Library/LaunchAgents/com.caffeinated-whale-cli.auto-inspect.plist`)
  - **Linux**: systemd user service (`~/.config/systemd/user/caffeinated-whale-cli-auto-inspect.service`)
  - **Windows**: Task Scheduler task ("CaffeinatedWhaleCliAutoInspect")
  - Startup status shown in `status` command

- **Configuration options** in `config.toml`:
  - `auto_inspect.enabled` - Enable/disable auto-inspection (default: false)
  - `auto_inspect.interval` - Inspection interval in seconds (default: 3600)
  - `auto_inspect.startup_enabled` - Track startup configuration state (default: false)

## [0.10.2] - 2025-11-08

### Fixed
- **Critical:** Tab completion function signatures for Typer compatibility
  - Added required parameters (ctx, args, incomplete) to all completion functions
  - Fixed TypeError when Typer calls completion callbacks
  - Removed `sparse=True` from Docker query that prevented label access
  - Fixed DockerException: "Label data is not available for sparse objects"

### Changed
- Corrected `_cache` type annotation from `Dict[str, Dict[str, Any]]` to `Dict[str, Tuple[float, List[str]]]`

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
