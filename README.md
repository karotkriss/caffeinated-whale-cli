# Caffeinated Whale CLI

A command-line interface (CLI) for managing Frappe/ERPNext Docker instances during local development. Simplify container management, bench operations, and development workflows with an intuitive set of commands.

## Features

- **Smart Port Management** - Automatic port conflict detection and resolution
- **Project Discovery** - Scan and list all Frappe Docker projects
- **Container Lifecycle** - Start, stop, and restart projects with ease
- **Development Tools** - VS Code integration, log viewing, and command execution
- **Cache System** - Fast project inspection with SQLite-based caching
- **Update Management** - App updates with automatic migrations and lock cleanup

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

**Example Output:**

```
frappe-one
├── Bench: bench
│   ├── Site: frappe-one.localhost
│   │   ├── App: frappe (v15.0.0, develop)
│   │   └── App: erpnext (v15.0.0, version-15)
│   └── Site: site2.localhost
│       ├── App: frappe (v15.0.0, develop)
│       └── App: erpnext (v15.0.0, version-15)
└── Bench: bench2
    └── Site: site3.localhost
        └── App: frappe (v15.0.0, develop)
```

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
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Features:**

- Auto-detects VS Code and VS Code Insiders installations
- Interactive editor selection menu
- Automatically installs required VS Code extensions (Docker and Dev Containers)
- Uses cached bench paths from `inspect` command
- Falls back to Docker exec if VS Code is unavailable

**Examples:**

```bash
# Open project (uses cached bench path)
cwcli open frappe-one

# Open specific app directory
cwcli open frappe-one --app erpnext

# Open custom path
cwcli open frappe-one --path /workspace/custom-bench
```

**Interactive Prompt:**

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

**What It Does:**

1. Runs `git pull` in each specified app directory
2. Identifies all sites where the updated apps are installed
3. Runs `bench --site <site> migrate` for each affected site
4. (Optional) Runs `bench build --app <app>` for successfully updated apps
5. (Optional) Runs `bench --site <site> clear-cache` for affected sites
6. (Optional) Runs `bench --site <site> clear-website-cache` for affected sites
7. Automatically clears locks folder for all affected sites to prevent stale locks

**Examples:**

```bash
# Update a single app
cwcli update frappe-one --app erpnext

# Update multiple apps
cwcli update frappe-one --app frappe --app erpnext

# Update with build and cache clearing
cwcli update frappe-one --app erpnext --build --clear-cache --clear-website-cache

# Update with verbose output
cwcli update frappe-one --app custom_app -v
```

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
| `-s`, `--site TEXT` | Site name to unlock (removes the locks folder) - **required** |
| `-p`, `--path TEXT` | Path to the bench directory inside the container (uses cached path from inspect if not specified) |
| `-v`, `--verbose` | Enable verbose output and stream rm command output |

**What It Does:**

Removes the `{bench_path}/sites/{site_name}/locks` directory, which can help resolve issues when a site is stuck in a locked state due to incomplete migrations or background jobs.

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

**Cache Location:** `~/caffeinated-whale-cli/cache/cwc-cache.db`

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
