# Caffeinated Whale CLI

A command-line interface (CLI) for managing Frappe/ERPNext Docker instances during local development. Simplify container management, bench operations, and development workflows with an intuitive set of commands.

## Features

- **One-Command Setup** - Initialize complete Frappe/ERPNext environments with `cwcli init`
- **Smart Port Management** - Automatic port conflict detection and resolution
- **Project Discovery** - Scan and list all Frappe Docker projects
- **Container Lifecycle** - Start, stop, and restart projects with ease
- **Development Tools** - VS Code integration, log viewing, and command execution
- **Cache System** - Fast project inspection with SQLite-based caching and configuration storage
- **Default Site Support** - Optional `--site` flag when default site is configured
- **Backup & Restore** - Interactive site restoration with automatic file archive detection and P2P transfer support
- **Update Management** - App updates with automatic migrations and lock cleanup
- **Auto-Inspection** - Background process to keep project cache fresh automatically
- **System Integration** - Auto-start on system boot with platform-specific configurations
- **Contextual Tips** - Helpful tips displayed during long-running operations to help you discover features

## Installation

Ensure you have Python 3.10+ and `pip` installed.

```bash
pip install caffeinated-whale-cli
```

### Troubleshooting

After installation, if you see an error like `'cwcli' is not recognized...`, the installation directory is not in your system's `PATH`.

To fix this:

1. **Find the script's location:** Run `pip show -f caffeinated-whale-cli` and look for the location of `cwcli.exe` (or `cwcli` on macOS/Linux). It's typically in a `Scripts` or `bin` folder within your Python installation directory.
2. **Add to PATH:** Follow your operating system's instructions to add this directory to your `PATH` environment variable.
3. **Restart your terminal:** Close and reopen your terminal for changes to take effect.

## Quick Start

```bash
# Initialize a new Frappe project (downloads compose, starts containers, creates bench & site)
cwcli init my-project

# Initialize with ERPNext and custom port
cwcli init my-project --port 10000 --install-erpnext

# List all Frappe projects
cwcli ls

# Start a project (with automatic port conflict detection)
cwcli start my-project

# View bench logs in real-time
cwcli logs my-project

# Open project in VS Code
cwcli open my-project

# Update apps and migrate sites
cwcli update my-project --app erpnext --build
```

## Command Reference

### `init` - Initialize New Project

Creates a complete Frappe development environment in a single step. Downloads compose files, starts containers, initializes bench, and creates a site.

```bash
cwcli init [OPTIONS] [PROJECT_NAME]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | Docker Compose project name. If not provided, will prompt interactively |

**Options:**

| Option | Description |
|--------|-------------|
| `-P`, `--port INTEGER` | Starting port for the project (default: 8000). Creates ports {port}-{port+5} for web servers and {port+1000}-{port+1005} for socketio |
| `-b`, `--bench TEXT` | Bench directory name inside the container (default: frappe-bench) |
| `-s`, `--site TEXT` | Primary site name, must end with .localhost (default: development.localhost) |
| `--bench-parent TEXT` | Directory inside container where bench is created (default: /workspace) |
| `--frappe-branch TEXT` | Frappe branch for bench init (default: version-15) |
| `--db-root-password TEXT` | MariaDB root password (default: 123) |
| `--admin-password TEXT` | Administrator password for the site (default: admin) |
| `--install-erpnext` | Install ERPNext application after initialization |
| `--erpnext-branch TEXT` | ERPNext branch to use (default: version-15) |
| `--auto-start` | Automatically start containers if not running |
| `-v`, `--verbose` | Show verbose output with streaming command execution |

**What It Does:**

1. Creates project directory at `~/.cwcli/projects/{project_name}/conf/`
2. Downloads `docker-compose.yml` from frappe_docker GitHub repository
3. Customizes port mappings based on `--port` flag
4. Pulls Docker images and starts containers
5. Initializes Frappe bench with specified branch
6. Configures database and Redis connections
7. Creates site with admin credentials
8. Enables developer mode and server scripts
9. Optionally installs ERPNext

**Examples:**

```bash
# Initialize with interactive prompts
cwcli init

# Initialize with project name
cwcli init my-project

# Initialize with custom port (avoids conflicts with other projects)
cwcli init my-project --port 10000

# Initialize with ERPNext
cwcli init my-project --install-erpnext

# Full customization
cwcli init my-project \
  --port 12000 \
  --bench custom-bench \
  --site myapp.localhost \
  --frappe-branch version-15 \
  --install-erpnext \
  --erpnext-branch version-15 \
  --admin-password secretpass

# Verbose mode for debugging
cwcli init my-project -v
```

**Example Output:**

```
✓ Successfully initialized bench 'frappe-bench' in 8m 32s
Bench path: /workspace/frappe-bench
Next steps: Run `cwcli open my-project` to open the project in vscode or exec with docker.
```

**Port Conflict Handling:**

If the requested ports are in use, you'll see:

```
Error: The following ports are already in use: 8000-8005

Tip: Use the --port flag to select a different starting port.
Example: cwcli init my-project --port 10000
```

**Reusing Existing Bench:**

If a bench already exists at the specified path:

```
Bench 'frappe-bench' already exists at /workspace/frappe-bench.
? Reuse the existing bench and continue with site setup? (Y/n)
```

---

### `ls` - List Projects

Scans Docker for Frappe/ERPNext projects and displays their status and ports.

```bash
cwcli ls [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Display all ports individually, without condensing them into ranges |
| `-q`, `--quiet` | Only display project names, one per line (useful for scripting) |
| `--json` | Output the list of instances as a raw JSON string |

**Example Output:**

```
┌──────────────┬─────────┬──────────────────┐
│ Project Name │ Status  │ Ports            │
├──────────────┼─────────┼──────────────────┤
│ frappe-one   │ running │ 8000-8005, 9000  │
│ frappe-two   │ exited  │                  │
└──────────────┴─────────┴──────────────────┘
```

---

### `start` - Start Containers

Starts a project's containers with **automatic port conflict detection and resolution**.

```bash
cwcli start [OPTIONS] [PROJECT_NAME]...
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The name(s) of the Frappe project(s) to start (can be piped from stdin) |

**Options:**

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Features:**

- **Port Conflict Detection:** Automatically checks if required ports are available
- **Interactive Resolution:** Offers to stop conflicting Frappe projects
- **Process Identification:** Shows which processes are using ports (cross-platform)
- **Smart Error Messages:** Provides actionable guidance for resolution

**Example:**

```bash
# Start a single project
cwcli start frappe-one

# Start multiple projects
cwcli start frappe-one frappe-two

# Pipe from ls
cwcli ls --quiet | cwcli start
```

**Port Conflict Example:**

```
Warning: Some ports needed by 'frappe-one' are in use by other Frappe projects:
  • Project 'frappe-two': 8000-8005

? Stop project 'frappe-two' to free up its ports? (Y/n)
```

---

### `stop` - Stop Containers

Stops a running project's containers.

```bash
cwcli stop [OPTIONS] [PROJECT_NAME]...
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The name(s) of the Frappe project(s) to stop (can be piped from stdin) |

**Options:**

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Example:**

```bash
# Stop a single project
cwcli stop frappe-one

# Stop multiple projects
cwcli stop frappe-one frappe-two
```

---

### `restart` - Restart Containers

Restarts a project's containers and bench instance.

```bash
cwcli restart [OPTIONS] [PROJECT_NAME]...
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The name(s) of the Frappe project(s) to restart (can be piped from stdin) |

**Options:**

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Example:**

```bash
cwcli restart frappe-one
```

**Example Output:**

```
Attempting to restart 1 project(s)...
✓ Instance 'frappe-one' stopped.
✓ Instance 'frappe-one' started.
✓ Started bench (logs: /tmp/bench-frappe-one.log)
View logs with: cwcli logs frappe-one
```

---

### `logs` - View Bench Logs

View bench logs in real-time from the log file inside the container.

```bash
cwcli logs [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The name of the Frappe project to view logs for (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-f`, `--follow` / `--no-follow` | Follow log output in real-time (default: follow) |
| `-n`, `--lines INTEGER` | Number of lines to show from the end of the logs (default: 100) |
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Examples:**

```bash
# Follow logs in real-time (default)
cwcli logs frappe-one

# Show last 50 lines and exit
cwcli logs frappe-one --no-follow --lines 50

# Show last 200 lines and follow
cwcli logs frappe-one -n 200
```

**Note:** Logs are stored at `/tmp/bench-{project_name}.log` inside the container.

---

### `inspect` - Inspect Project Structure

Inspects a project to find all bench instances, sites, and apps within it. Results are cached for faster subsequent operations.

**Security Note:** The inspect command caches site configurations including database credentials and Redis URLs. The cache is stored with restricted filesystem permissions (directory: `0700`, database: `0600`) to prevent unauthorized access. Only the current user can read the cached data. Do not share the cache directory (`~/.cwcli/cache/`) with untrusted users.

```bash
cwcli inspect [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project to inspect (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Enable verbose diagnostic output |
| `-j`, `--json` | Output the result as a JSON object |
| `-u`, `--update` | Update the cache by re-inspecting the project |
| `-a`, `--show-apps` | Show available apps in the output tree |
| `-i`, `--interactive` | Prompt to name each bench instance interactively |

**What It Caches:**
- Bench instances and their paths
- Sites and installed apps for each bench
- Site configurations (database credentials, developer mode settings)
- Common site configuration (Redis URLs, ports, default site, etc.)
- Default site is labeled with `(default)` in output

**Example Output:**

```
frappe-one
├── Bench: bench
│   ├── Site: frappe-one.localhost (default)
│   │   ├── App: frappe (v15.0.0, develop)
│   │   └── App: erpnext (v15.0.0, version-15)
│   └── Site: site2.localhost
│       ├── App: frappe (v15.0.0, develop)
│       └── App: erpnext (v15.0.0, version-15)
└── Bench: bench2
    └── Site: site3.localhost
        └── App: frappe (v15.0.0, develop)
```

**Benefits:**
- Enables default site feature: `unlock` command can omit `--site` flag
- Faster subsequent operations (uses cached data)
- Stores configurations for programmatic access

**Examples:**

```bash
# Inspect and cache project structure
cwcli inspect frappe-one

# Force refresh the cache
cwcli inspect frappe-one --update

# Show available apps
cwcli inspect frappe-one --show-apps

# Get JSON output
cwcli inspect frappe-one --json

# Interactive bench naming
cwcli inspect frappe-one --interactive
```

---

### `open` - Open in VS Code or Docker Exec

Opens a project's frappe container in VS Code (with Dev Containers) or executes into it.

```bash
cwcli open [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name to open (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-p`, `--path TEXT` | Path inside the container to open (uses cached bench path from inspect if not specified) |
| `-a`, `--app TEXT` | App name to open (opens the app's directory within the bench) |
| `--code` | Open with VS Code directly (skips interactive prompt) |
| `--code-insiders` | Open with VS Code Insiders directly (skips interactive prompt) |
| `--docker` | Open with Docker exec directly (skips interactive prompt) |
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Features:**

- Auto-detects VS Code and VS Code Insiders installations
- Interactive editor selection menu (when no editor flag specified)
- Direct editor selection via `--code`, `--code-insiders`, or `--docker` flags
- Automatically installs required VS Code extensions (Docker and Dev Containers)
- Uses cached bench paths from `inspect` command
- Docker exec opens in bench directory (respects working directory)
- Falls back to Docker exec if VS Code is unavailable

**Examples:**

```bash
# Open project with interactive prompt (uses cached bench path)
cwcli open frappe-one

# Open directly with VS Code (skip prompt)
cwcli open frappe-one --code

# Open directly with Docker exec (skip prompt)
cwcli open frappe-one --docker

# Open specific app directory with VS Code Insiders
cwcli open frappe-one --app erpnext --code-insiders

# Open custom path
cwcli open frappe-one --path /workspace/custom-bench
```

**Interactive Prompt:**

When no editor flag is specified, you'll see:

```
How would you like to open this instance?
❯ VS Code - Open in development container
  Docker - Execute interactive shell in container
```

---

### `update` - Update Apps and Migrate

Updates specified Frappe apps and migrates all sites where they are installed.

```bash
cwcli update [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The name of the project to update (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-a`, `--app TEXT` | App name(s) to update (specify multiple apps after `--app` or use `--app` multiple times) |
| `-p`, `--path TEXT` | Path to the bench directory inside the container (uses cached path from inspect if not specified) |
| `-v`, `--verbose` | Enable verbose output with streaming command execution |
| `-c`, `--clear-cache` | Clear cache for all affected sites after migration |
| `-w`, `--clear-website-cache` | Clear website cache for all affected sites after migration |
| `-b`, `--build` | Build assets after updating apps |
| `--skip-maintenance` | Skip enabling maintenance mode for affected sites during update |

**What It Does:**

1. Runs `git pull` in each specified app directory
2. Identifies all sites where the updated apps are installed
3. Enables maintenance mode for affected sites (to prevent user access during updates)
4. Runs `bench --site <site> migrate` for each affected site
5. (Optional) Runs `bench build --app <app>` for successfully updated apps
6. (Optional) Runs `bench --site <site> clear-cache` for affected sites
7. (Optional) Runs `bench --site <site> clear-website-cache` for affected sites
8. Automatically clears locks folder for all affected sites to prevent stale locks
9. Disables maintenance mode for affected sites after completion

**Examples:**

```bash
# Update a single app (with maintenance mode enabled by default)
cwcli update frappe-one --app erpnext

# Update multiple apps
cwcli update frappe-one --app frappe --app erpnext

# Update with build and cache clearing
cwcli update frappe-one --app erpnext --build --clear-cache --clear-website-cache

# Update without maintenance mode
cwcli update frappe-one --app erpnext --skip-maintenance

# Update with maintenance mode and all options
cwcli update frappe-one --app erpnext --build --clear-cache --clear-website-cache

# Update with verbose output
cwcli update frappe-one --app custom_app -v
```

**Note:** Maintenance mode is **enabled by default** for all affected sites during updates to prevent user access and potential data corruption. Use `--skip-maintenance` only if you need to update during operational hours when user access is critical.

**Example Output:**

```
Updating project: frappe-one

Updating 1 app(s) for project 'frappe-one'

→ Updating app: erpnext
✓ Successfully updated 'erpnext'
  Found 2 site(s) with 'erpnext' installed

Migrating 2 affected site(s)

✓ Migration complete for all affected sites

✓ Successfully updated 1 app(s)
```

---

### `unlock` - Unlock Site

Removes the locks folder for a specified site to unlock it.

```bash
cwcli unlock [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-s`, `--site TEXT` | Site name to unlock. If not provided, uses the default site from `common_site_config.json` |
| `-p`, `--path TEXT` | Path to the bench directory inside the container (uses cached path from inspect if not specified) |
| `-v`, `--verbose` | Enable verbose output and stream rm command output |

**What It Does:**

Removes the `{bench_path}/sites/{site_name}/locks` directory, which can help resolve issues when a site is stuck in a locked state due to incomplete migrations or background jobs.

**Smart Defaults:**
- If `--site` is not specified, automatically uses the default site from your bench's `common_site_config.json`
- Shows "Using default site: {site}" when using the default
- Run `cwcli inspect {project}` first to cache the configuration

**Examples:**

```bash
# Unlock a specific site
cwcli unlock my-project --site development.localhost

# Use default site (no --site flag needed)
cwcli unlock my-project
# Output: Using default site: development.localhost

# Verbose mode with default site
cwcli unlock my-project -v
```

**When to Use:**

- After a migration fails or is interrupted
- When you see "This document is currently locked and queued for execution" errors
- When background jobs don't complete properly

**Examples:**

```bash
# Unlock a site
cwcli unlock my-project --site development.localhost

# Unlock with verbose output to see files being removed
cwcli unlock my-project --site development.localhost -v
```

**Example Output:**

```
✓ Successfully unlocked site 'development.localhost'
Removed locks folder: /workspace/frappe-bench/sites/development.localhost/locks
```

**Note:** The `update` command automatically clears locks after completion, so manual unlocking is typically only needed for interrupted operations.

---

### `restore` - Restore Site from Backup

Interactively restore a site from a backup with automatic detection of file archives and encryption keys. Supports both local restoration and peer-to-peer backup transfers via sendme.

```bash
cwcli restore [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-s`, `--site TEXT` | Site name to restore. If not provided, uses the default site from `common_site_config.json` |
| `-p`, `--path TEXT` | Path to the bench directory inside the container (uses cached path from inspect if not specified) |
| `--mariadb-root-username TEXT` | MariaDB root username (default: root) |
| `--mariadb-root-password TEXT` | MariaDB root password (will prompt if not provided) |
| `--admin-password TEXT` | Set administrator password after restore |
| `--send` | **P2P Mode:** Share backup with another machine via peer-to-peer transfer |
| `--receive` | **P2P Mode:** Receive backup from another machine via peer-to-peer transfer |
| `-v`, `--verbose` | Enable verbose output and show restore command details |

**What It Does:**

1. Scans all backup files across all sites in the bench
2. Presents an interactive menu with backups grouped by target site
3. Shows badges indicating backup contents: `[FILES]`, `[PRIVATE]`, `[DATABASE ONLY]`
4. Automatically detects and restores public/private file archives
5. Restores encryption key from backup's site_config if available
6. Displays helpful error messages on failure with common causes

**Interactive Features:**

- **Smart Grouping**: Backups from the target site shown first, followed by backups from other sites
- **Visual Badges**: Clear indicators of what each backup contains
- **Secure Password Prompt**: Uses questionary for consistent, secure password input
- **Confirmation Dialog**: Warns about data replacement before restore
- **TipSpinner**: Shows helpful tips during restore operation

**Examples:**

```bash
# Restore with interactive backup selection (uses default site)
cwcli restore my-project

# Restore specific site
cwcli restore my-project --site production.localhost

# Restore with verbose output for debugging
cwcli restore my-project -v

# Provide password via command line (not recommended for production)
cwcli restore my-project --mariadb-root-password "secret123"
```

**Example Session:**

```
Using default site: development.localhost

? Select a backup to restore: (Use arrow keys)

=== Backups from site: development.localhost ===
 > 2025-11-12 10:56:38  [FILES] [PRIVATE]
   2025-11-12 09:30:01  [DATABASE ONLY]
   2025-11-11 23:15:42  [FILES] [PRIVATE]

⚠ Warning: This will replace all data in site 'development.localhost'
Backup: 20251112_105638-development_localhost-database.sql.gz
From: 2025-11-12 10:56:38
Will restore: Database, Public files, Private files

? Are you sure you want to restore? (y/N) y
MariaDB root password: ********

✓ Successfully restored site 'development.localhost'
From backup: 20251112_105638-development_localhost-database.sql.gz
Including file archives
Updated encryption_key from backup site_config
```

**P2P Backup Transfer:**

Share backups between machines using peer-to-peer connections (powered by sendme/Iroh):

**Sending a Backup:**
```bash
# On the source machine
cwcli restore my-project --send

# Select backup from interactive menu
# Ticket automatically copied to clipboard
✓ Transfer ticket copied to clipboard!

Instructions for the receiver:
  1. Run: cwcli restore <project_name> --receive
  2. Paste the ticket when prompted
```

**Receiving a Backup:**
```bash
# On the destination machine
cwcli restore my-project --receive

# Paste the ticket from sender
? Enter the sendme ticket: blob...

# Files download with hash verification
✓ Files downloaded successfully
# Automatic restore process begins
```

**P2P Transfer Features:**
- **Hash-verified transfers** - BLAKE3 cryptographic verification ensures data integrity
- **Resumable downloads** - Interrupted transfers can resume from where they stopped
- **NAT traversal** - Works behind firewalls and corporate networks
- **No cloud intermediary** - Direct peer-to-peer connections
- **Cross-platform** - Works on macOS, Linux, and Windows
- **Automatic setup** - sendme binary auto-installed on first use
- **Multiple receivers** - Same ticket can be used by multiple machines

**Use Cases:**
- Share production backups with development team
- Transfer large backups without cloud storage limits
- Migrate data between data centers
- Distribute backups to multiple environments simultaneously

**Security:**

- Passwords validated to prevent shell injection
- All inputs sanitized for command injection prevention
- Backup file existence verified before restore
- Passwords masked in verbose output
- P2P transfers are hash-verified (BLAKE3) to prevent tampering
- Treat transfer tickets like passwords (they grant download access)

**When to Use:**

- Restore from scheduled backups after issues
- Migrate data between environments
- Recover from data corruption or accidental deletion
- Test backup integrity
- **Share backups between machines without cloud storage**

---

### `run` - Execute Bench Commands

Executes bench commands inside a project's frappe container.

```bash
cwcli run [OPTIONS] PROJECT_NAME BENCH_ARGS...
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name (required) |
| `BENCH_ARGS` | Bench command and arguments to run (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-p`, `--path TEXT` | Path to the bench directory inside the container (default: `/workspace/frappe-bench`) |
| `-v`, `--verbose` | Enable verbose output |

**Examples:**

```bash
# Run bench migrate
cwcli run frappe-one migrate

# Run bench with specific site
cwcli run frappe-one --site development.localhost migrate

# Execute a custom bench command
cwcli run frappe-one console

# Use custom bench path
cwcli run frappe-one migrate --path /workspace/custom-bench
```

---

### `status` - Check Health Status

Checks the health status of a Frappe project instance.

```bash
cwcli status [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name to check (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Show the health-check command, raw curl output, and explain the reported status |

**Status Values:**

- **`offline`** - Container is not running
- **`online`** - Container is running but HTTP probe failed
- **`running`** - Container is running and HTTP probe succeeded

**Example:**

```bash
cwcli status frappe-one
```

---

### `config` - Manage Configuration

Manages the CLI configuration and cache.

```bash
cwcli config [SUBCOMMAND]
```

#### Subcommands

##### `config path` - Show Config Path

Displays the path to the configuration file.

```bash
cwcli config path
```

##### `config add-path` - Add Custom Bench Path

Adds a custom bench search path to the configuration.

```bash
cwcli config add-path PATH
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PATH` | The absolute path to add to the custom search paths (required) |

**Example:**

```bash
cwcli config add-path /home/user/custom-bench
```

##### `config remove-path` - Remove Custom Bench Path

Removes a custom bench search path from the configuration.

```bash
cwcli config remove-path PATH
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PATH` | The path to remove from the custom search paths (required) |

**Example:**

```bash
cwcli config remove-path /home/user/custom-bench
```

##### `config cache` - Manage Cache

Manages the cache for project inspection data.

```bash
cwcli config cache [SUBCOMMAND]
```

**Cache Subcommands:**

- **`clear [PROJECT_NAME]`** - Clear cache for a specific project or the entire cache
  - Options: `-a`, `--all` - Clear the entire cache
  - Example: `cwcli config cache clear frappe-one`
  - Example: `cwcli config cache clear --all`

- **`path`** - Display the path to the cache file
  - Example: `cwcli config cache path`

- **`list`** - List all projects currently in the cache
  - Example: `cwcli config cache list`

##### `config auto-inspect` - Automatic Project Inspection

Manages automatic background inspection of running Frappe projects to keep cached data fresh.

```bash
cwcli config auto-inspect [SUBCOMMAND]
```

**Auto-Inspect Subcommands:**

- **`enable`** - Enable automatic project inspection
  - Options:
    - `--interval INTEGER` - Inspection interval in seconds (minimum 60, default 3600)
    - `--startup` - Also enable automatic startup on system boot/login
  - Example: `cwcli config auto-inspect enable --interval 1800 --startup`

- **`disable`** - Disable automatic inspection and stop background process
  - Example: `cwcli config auto-inspect disable`

- **`start`** - Start the auto-inspect background process
  - Options:
    - `--startup` - Also enable automatic startup on system boot
  - Example: `cwcli config auto-inspect start --startup`

- **`stop`** - Stop the auto-inspect background process
  - Example: `cwcli config auto-inspect stop`

- **`restart`** - Restart the auto-inspect background process
  - Example: `cwcli config auto-inspect restart`

- **`status`** - Show detailed status (enabled, interval, process state, PID, startup)
  - Example: `cwcli config auto-inspect status`

- **`logs`** - View recent background process logs
  - Options: `--lines INTEGER` - Number of log lines to show (default 20)
  - Example: `cwcli config auto-inspect logs --lines 50`

- **`set-interval`** - Change the inspection interval
  - Example: `cwcli config auto-inspect set-interval 7200`

- **`install-startup`** - Install platform-specific startup configuration
  - Creates LaunchAgent (macOS), systemd service (Linux), or Task Scheduler task (Windows)
  - Example: `cwcli config auto-inspect install-startup`

- **`uninstall-startup`** - Remove startup configuration
  - Example: `cwcli config auto-inspect uninstall-startup`

**What it does:**

The auto-inspect feature runs a background daemon process that periodically inspects all running Frappe projects. This keeps your project cache fresh for:
- Tab completion (project names, apps, sites)

##### `config tips` - Manage Contextual Tips

Control the display of helpful tips during long-running operations.

```bash
cwcli config tips [enable|disable|status]
```

**Subcommands:**

- **`enable`** - Enable contextual tips during long operations
  - Example: `cwcli config tips enable`

- **`disable`** - Disable contextual tips
  - Example: `cwcli config tips disable`

- **`status`** - Show whether contextual tips are enabled
  - Example: `cwcli config tips status`

**What it does:**

When enabled (default), cwcli displays rotating helpful tips alongside spinners during long-running operations like `inspect`, `update`, and `open`. Tips help you discover features and best practices while waiting for operations to complete.

**Examples of tips shown:**

- 💡 Add VS Code to PATH via Command Palette: 'Shell Command: Install code command in PATH'
- 💡 Install tab completion with 'cwcli --install-completion' for faster workflows
- 💡 Use 'cwcli inspect <project>' to cache project structure for faster commands
- 💡 cwcli automatically detects and resolves port conflicts when starting projects
- Project status queries
- Other commands that rely on cached data

**Quick Setup:**

```bash
# Enable with 1-hour interval and auto-start on boot
cwcli config auto-inspect enable --interval 3600 --startup

# Start the background process
cwcli config auto-inspect start

# Check status
cwcli config auto-inspect status

# View logs
cwcli config auto-inspect logs
```

**Notes:**
- Background process survives terminal closure
- Process stops on system restart unless startup is enabled
- Logs stored in `~/.cwcli/run/auto-inspect.log`
- PID file stored in `~/.cwcli/run/auto-inspect.pid`

---

## Tips and Tricks

### Piping Commands

Many commands support piping project names from stdin:

```bash
# Start all projects
cwcli ls --quiet | cwcli start

# Stop specific projects using grep
cwcli ls --quiet | grep "frappe-" | cwcli stop
```

### Scripting with JSON

Use JSON output for programmatic access:

```bash
# Get project data as JSON
cwcli ls --json | jq '.[] | select(.status=="running")'

# Parse inspect output
cwcli inspect frappe-one --json | jq '.benches[0].sites'
```

### Verbose Mode for Debugging

Use `-v` flag on any command to see detailed diagnostic output:

```bash
cwcli start frappe-one -v
cwcli update frappe-one --app erpnext -v
```

### Shell Completion

cwcli supports intelligent tab completion for project names, apps, and sites across all shells (Bash, Zsh, Fish, PowerShell).

**One-time setup:**

```bash
# Install completion for your current shell
cwcli --install-completion

# Restart your shell or source your shell config
source ~/.bashrc  # For Bash
source ~/.zshrc   # For Zsh
```

**What gets completed:**

- **Project names** - All commands that accept project names (start, stop, restart, inspect, logs, open, status, run, update, unlock)
- **App names** - Commands with `--app` option (open, update)
- **Site names** - Commands with `--site` option (unlock)

**Examples:**

```bash
# Press TAB after typing partial project name
cwcli start frap<TAB>
# Completes to: cwcli start frappe-one

# Press TAB to see available apps for a project
cwcli update frappe-one --app <TAB>
# Shows: erpnext  frappe  hrms  custom_app

# Press TAB to see available sites
cwcli unlock frappe-one --site <TAB>
# Shows: site1.localhost  site2.localhost
```

**How it works:**

- **Projects**: Queried from Docker containers in real-time
- **Apps/Sites**: Loaded from cached project data (run `cwcli inspect` first)
- **Fast & Context-aware**: Completions adapt based on the project specified

**Troubleshooting:**

If completion doesn't work:
1. Ensure you've run `cwcli --install-completion`
2. Restart your shell
3. For apps/sites, run `cwcli inspect <project>` to populate the cache

## Architecture

The CLI uses:

- **Docker SDK for Python** - Container management
- **Typer** - CLI framework with type hints
- **Rich** - Terminal formatting and spinners
- **Questionary** - Interactive prompts
- **Peewee ORM** - SQLite-based caching

**Data Directories:**
- **Projects**: `~/.cwcli/projects/` - Project directories created by `cwcli init`
- **Config**: `~/.cwcli/config/` - Configuration files
- **Cache**: `~/.cwcli/cache/cwc-cache.db` - Project inspection cache
- **Runtime**: `~/.cwcli/run/` - PID and log files for background services

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please see our [Contributing Guide](./CONTRIBUTING.md) for detailed information.

**Quick Links:**
- [Git Workflow](./docs/contributing/git-workflow.md) - Complete contribution workflow
- [Commit Messages](./docs/contributing/commit-messages.md) - Conventional commit standards
- [Code Quality](./docs/contributing/code-quality.md) - Formatting with Black, linting with Ruff
- [Testing Guide](./docs/testing/guide.md) - How to write and run tests
- [CI/CD](./docs/contributing/ci-cd.md) - GitHub Actions workflows

**Getting Started:**

```bash
# Clone the repository
git clone https://github.com/karotkriss/caffeinated-whale-cli.git
cd caffeinated-whale-cli

# Install dependencies
uv sync --all-extras

# Run tests
uv run pytest --cov

# Format and lint
uv run black src/ tests/
uv run ruff check src/ --fix
```

For questions or issues, please open an issue on [GitHub](https://github.com/karotkriss/caffeinated-whale-cli).
