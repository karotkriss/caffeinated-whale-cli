# Caffeinated Whale CLI

A command-line interface (CLI) for managing Frappe/ERPNext Docker instances during local development. Simplify container management, bench operations, and development workflows with an intuitive set of commands.

## Features

- **One-Command Setup** - Initialize complete Frappe/ERPNext environments with `cwcli init`
- **Smart Port Management** - Automatic port conflict detection and resolution
- **Project Discovery** - Scan and list all Frappe Docker projects
- **Cross-Project Search** - Find apps and sites across all instances with `cwcli where`
- **Container Lifecycle** - Start, stop, and restart projects with ease
- **Development Tools** - VS Code integration, log viewing, and command execution
- **Cache System** - Fast project inspection with SQLite-based caching and configuration storage
- **Multi-Bench Support** - Address individual benches in a multi-bench instance by numeric index or a durable label with `--bench`
- **Default Site Support** - Optional `--site` flag when default site is configured
- **Backup & Restore** - Interactive site restoration with automatic file archive detection and P2P transfer support
- **App Management** - List, install, uninstall, and update Frappe apps per bench and per site with `cwcli apps` (multi-site by default, `--json`, honest exit codes)
- **Update Management** - App updates with automatic migrations and lock cleanup
- **Self-Update** - Upgrade cwcli itself to the latest release with `cwcli self-update` (install-method aware)
- **Update Notices** - A passive, once/day "a newer cwcli is available" hint on stderr, shown only to a human at a TTY
- **Auto-Inspection** - Background process to keep project cache fresh automatically
- **System Integration** - Auto-start on system boot with platform-specific configurations
- **Contextual Tips** - Helpful tips displayed during long-running operations to help you discover features

## Installation

`cwcli` requires Python 3.10+.

### With uv (recommended)

[uv](https://docs.astral.sh/uv/) installs `cwcli` into an isolated environment and puts the `cwcli` command on your `PATH`, without touching your system or project Python.

```bash
# Install (adds the `cwcli` command to your PATH)
uv tool install caffeinated-whale-cli

# Upgrade to the latest release
uv tool upgrade caffeinated-whale-cli

# Uninstall
uv tool uninstall caffeinated-whale-cli
```

Or let cwcli upgrade itself - `cwcli self-update` detects your install method and runs the right command (see [`self-update`](#self-update---upgrade-cwcli-itself)).

If this is your first `uv tool install`, uv may print a note about adding its tool-bin directory to your `PATH`; run `uv tool update-shell` (then restart your terminal) to do so.

To run a one-off command without installing, use `uvx`.
Because the command name (`cwcli`) differs from the package name, pass it via `--from`:

```bash
uvx --from caffeinated-whale-cli cwcli --version
uvx --from caffeinated-whale-cli cwcli ls
```

### With pip

```bash
pip install caffeinated-whale-cli
```

### Troubleshooting

After installation, if you see an error like `'cwcli' is not recognized...`, the installation directory is not in your system's `PATH`.

To fix this:

1. **uv:** Run `uv tool update-shell` and restart your terminal.
2. **pip:** Run `pip show -f caffeinated-whale-cli` and look for the location of `cwcli.exe` (or `cwcli` on macOS/Linux) - typically a `Scripts` or `bin` folder within your Python installation - then add that directory to your `PATH`.
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

# View recent bench logs (add -f to follow in real-time)
cwcli logs my-project

# Open project in VS Code
cwcli open my-project

# Update apps and migrate sites
cwcli apps update my-project erpnext
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
| `--frappe-branch TEXT` | Frappe branch or tag for bench init, e.g. `version-16` or `v16.26.3` (default: version-16). Mutually exclusive with `--version` |
| `--version TEXT` | Frappe version for bench init, resolved by shape: a bare major (`16` → `version-16` branch) or a full semantic version (`16.26.3` → `v16.26.3` tag). Malformed values are rejected with a non-zero exit. Mutually exclusive with `--frappe-branch` |
| `--db-root-password TEXT` | MariaDB root password (default: 123) |
| `--admin-password TEXT` | Administrator password for the site, used verbatim. If omitted, a strong password is generated and printed once (interactive runs only); a non-interactive run must supply this flag |
| `--install-erpnext` | Install ERPNext application after initialization |
| `--erpnext-branch TEXT` | ERPNext branch to use (default: version-16) |
| `--auto-start` | Automatically start containers if not running |
| `--reuse-bench` / `--no-reuse-bench` | Pre-answer the existing-bench question non-interactively: `--reuse-bench` reuses the bench and skips `bench init`; `--no-reuse-bench` requires a fresh `--bench` name and errors if it already exists. Default: ask interactively (a non-TTY without either flag refuses). Distinct from `--auto-start`, which controls container startup |
| `-v`, `--verbose` | Show verbose output with streaming command execution |

**What It Does:**

1. Creates project directory at `~/.cwcli/projects/{project_name}/conf/`
2. Downloads `docker-compose.yml` from frappe_docker GitHub repository
3. Customizes port mappings based on `--port` flag
4. Resolves the latest stable `frappe/bench` image tag from Docker Hub (never uses `:latest`)
5. Pulls Docker images and starts containers
6. Pins the correct Python version via `PYENV_VERSION` for the branch (installs via pyenv if missing)
7. Pins the correct Node.js version via nvm for older branches (installs via nvm if missing)
8. Installs `yarn` globally for the activated Node.js version (older branches only)
9. Initializes Frappe bench with specified branch
10. Pins `setuptools<82` inside the bench virtualenv for `version-13` (retains `pkg_resources`)
11. Configures database and Redis connections
12. Creates site with admin credentials (admin password generated and printed once when `--admin-password` is omitted in an interactive run; required as a flag non-interactively)
13. Enables developer mode and server scripts
14. Optionally installs ERPNext

**Branch-Specific Runtime Setup:**

| Branch | Python | Node.js | Bench Image |
|--------|--------|---------|-------------|
| `version-16` (default) | Default | Default | Latest stable from Docker Hub |
| `version-15` | 3.12.x (via pyenv) | Default | Latest stable from Docker Hub |
| `version-14` | 3.10.x (via pyenv) | 16 (via nvm) + yarn | Latest stable from Docker Hub |
| `version-13` | 3.9.x (via pyenv) | 14 (via nvm) + yarn | Latest stable from Docker Hub |
| Other | Default | Default | Latest stable from Docker Hub |

Version gating keys on the major version parsed from the ref, so a semantic-version tag
(e.g. `v14.80.0` from `--version 14.80.0`) is gated the same way its branch equivalent
(`version-14`) is.

Missing Python or Node.js versions are automatically installed inside the container.
`version-13` also pins `setuptools<82` in the bench virtualenv after init.

Site creation picks the MariaDB flag per major version: `14` and older use `--no-mariadb-socket`,
while `15` and newer use `--mariadb-user-host-login-scope=%` (a flag that only exists in bench/Frappe 15+).

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

# Pick a Frappe version by shape: a bare major -> version-N branch
cwcli init my-project --version 16

# ...or a full semantic version -> vX.Y.Z tag
cwcli init my-project --version 16.26.3

# Full customization
cwcli init my-project \
  --port 12000 \
  --bench custom-bench \
  --site myapp.localhost \
  --frappe-branch version-16 \
  --install-erpnext \
  --erpnext-branch version-16 \
  --admin-password secretpass

# Verbose mode for debugging
cwcli init my-project -v
```

**Example Output:**

```
✓ Successfully initialized bench 'frappe-bench' in 8m 32s
Bench path: /workspace/frappe-bench
Next steps: Run `cwcli open my-project` to open the project in vscode or exec with docker.

Administrator password (generated): 3sK9nQx7Lm-2pT4vWbY6Za
Shown once and not stored anywhere. To change it later, run `bench --site development.localhost set-admin-password <new-password>`.
```

The generated administrator password prints only when `--admin-password` is omitted in an interactive run; supply `--admin-password` to set it yourself (and to run non-interactively).

**Port Conflict Handling:**

If the requested ports are in use, you'll see:

```
Error: The following ports are already in use: 8000-8005

Tip: Use the --port flag to select a different starting port.
Example: cwcli init my-project --port 10000
```

**Reusing Existing Bench:**

If a bench already exists at the specified path (the devcontainer image ships a default `/workspace/frappe-bench`, so a fresh `init` usually hits this):

```
Bench 'frappe-bench' already exists at /workspace/frappe-bench.
? Reuse the existing bench 'frappe-bench' and continue with site setup? (Y/n)
```

Answer `Y` to reuse it as-is (bench initialization is skipped and site setup continues on the existing bench).
Answer `n` to set up a fresh bench instead; you're prompted for a different bench name and site setup continues there:

```
? Enter a different bench name to create (leave blank to cancel): my-bench
```

Leaving the name blank (or cancelling either prompt) makes no changes and exits cleanly:

```
No changes made.
```

**Non-interactive (agents/automation):** pass a flag so `init` never prompts.
Re-running `init` against the devcontainer's shipped bench, pass `--reuse-bench`:

```bash
# Reuse the existing bench and run site setup with zero prompts (skips bench init)
cwcli init my-project --reuse-bench --auto-start

# Require a brand-new bench; error out if the name already exists
cwcli init my-project --bench fresh-bench --no-reuse-bench
```

Without a reuse flag, a non-interactive session (non-TTY) where the bench already exists refuses honestly with exit 1 instead of hanging, and names `--reuse-bench`/`--no-reuse-bench`.

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

### `where` - Search Apps and Sites

Searches all cached instances for apps or sites matching a string. Useful for finding which projects have a specific app installed or contain a particular site.

```bash
cwcli where [OPTIONS] SEARCH
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `SEARCH` | Search string to match against app or site names (case-insensitive) |

**Options:**

| Option | Description |
|--------|-------------|
| `-a`, `--apps` | Search only for apps |
| `-s`, `--sites` | Search only for sites |
| `-i`, `--installed` | Show only installed apps (not just available). Only applies to app search |
| `--json` | Output results as JSON |

**Examples:**

```bash
# Find all instances with 'erpnext'
cwcli where erpnext

# Find apps matching 'payments'
cwcli where payments --apps

# Find sites matching 'local'
cwcli where local --sites

# JSON output for scripting
cwcli where frappe --json
```

**Example Output:**

```
                Apps matching 'frappe'
┏━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Project    ┃ App     ┃ Version  ┃ Branch     ┃ Site                  ┃
┡━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ my-project │ frappe  │ 15.93.0  │ version-15 │ development.localhost │
│ test       │ frappe  │ 15.88.2  │ version-15 │ development.localhost │
└────────────┴─────────┴──────────┴────────────┴───────────────────────┘

Found 2 matches.
```

**Notes:**
- Searches use cached project data. Run `cwcli inspect <project>` to populate/refresh the cache.
- When an app exists both as "available" and "installed" in the same project, only the installed version is shown (with version/branch info).
- Use `--installed` to exclude apps that are available but not yet installed on any site.

---

### `start` - Start Containers

Starts a project's containers and runs the bench under **supervisord**, with
**automatic port conflict detection and resolution**. It is **idempotent**:
re-running against an already-running bench reports "already running" and does
nothing - it never spawns a second supervisor stack. On first supervise it
installs `supervisor` into the bench environment; each Procfile process becomes a
supervised program that can be restarted or self-healed individually (see
[`restart`](#restart---restart-containers)).

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
| `--bench TEXT` | Which bench to run: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)). On a multi-bench project with no `--bench` it prompts interactively and refuses (non-zero) on a non-TTY |
| `--autorestart` / `--no-autorestart` | Self-heal crashed processes (default on): supervisord restarts a program that crashes (not one that exits cleanly), surfacing a crash-loop as `FATAL`. `--no-autorestart` leaves a crashed program down until an explicit restart. Set at launch |
| `-y`, `--yes` | Auto-confirm stopping conflicting Frappe projects to free their ports (non-interactive) |
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Features:**

- **Idempotent:** a re-run on an already-running bench is a clean no-op ("already running: N/N processes up"), never a second supervisor stack
- **Per-process supervision:** each Procfile process runs under supervisord, so one can be restarted or auto-healed without disturbing the others
- **Port Conflict Detection:** Automatically checks if required ports are available
- **Interactive Resolution:** Offers to stop conflicting Frappe projects (use `--yes` to auto-confirm)
- **Process Identification:** Shows which processes are using ports (cross-platform)
- **Smart Error Messages:** Provides actionable guidance for resolution
- **Multi-Bench Aware:** On a multi-bench project with no `--bench`, it prompts which bench interactively and refuses non-interactively; pick one explicitly with `--bench <index|label>`

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

Restarts a project. Without `--process` it restarts the whole stack (stop + start
the containers, relaunching the supervisor). With `--process <label>` it restarts
just that **one** supervised program while its siblings keep running - so a stuck
`web` or `worker` can be cycled without a full-stack bounce.

**Known limitation:** a whole-stack `cwcli restart` (and the restart `cwcli
restore` runs after a migration) always relaunches with the `--autorestart`
default (on). It does NOT persist a prior `cwcli start --no-autorestart`
choice - re-run `cwcli start --no-autorestart` afterward to reassert it.

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
| `-p`, `--process TEXT` | Restart ONE Procfile process (e.g. `web`, `worker`, `socketio`), leaving its siblings running. Omit to restart the whole stack |
| `--bench TEXT` | Which bench the `--process` belongs to: its numeric index or label (multi-bench projects) |
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Example:**

```bash
# Whole-stack restart
cwcli restart frappe-one

# Restart just the web process (siblings keep running)
cwcli restart frappe-one --process web
```

**Example Output:**

```
# whole stack
Attempting to restart 1 project(s)...
✓ Instance 'frappe-one' stopped.
✓ Instance 'frappe-one' started.
✓ Started bench (logs: /workspace/frappe-bench/logs)
View logs with: cwcli logs frappe-one

# single process
Instance 'frappe-one': restarted process web (pid 123 -> 456, RUNNING)
```

---

### `rm` - Remove Project

Removes a Frappe project: its containers, its named Docker volumes, and its local project directory.

**WARNING:** This action is destructive and cannot be undone.
Before deleting anything, the command re-caches the project, backs up the databases and files for all sites across every bench (a live `bench backup --with-files`, run once per bench on a multi-bench project), and archives the `docker-compose.yml`, `site_config.json`, and the project's `conf/` directory into a timestamped folder under `~/.cwcli/archive/` (or `$CWCLI_HOME/archive/` when the `CWCLI_HOME` override is set). The backup and the config archive are written first, and the named volumes and project directory are only deleted once both have succeeded. When volumes are being deleted (the default `--volumes`), a backup that cannot be fully created and verified blocks removal: each backup artifact is copied out of the container to the host archive and the database dump must be present and non-empty, and if it is not, the command aborts before any container is removed and exits non-zero, so data is never destroyed without a confirmed backup. Aborting before removal keeps the whole project intact (containers, volumes, directory, and cache), so a retry can still take a live backup from the running container. Under `--no-volumes` no volume data is destroyed, so a failed backup does not block the container, directory, and cache cleanup.

A live `bench backup` needs a running frappe and MariaDB, so when you remove a **stopped** project on the `--volumes` path, `cwcli rm` transiently **starts** it, takes a verified backup, and only then deletes it (start → back up → delete), reusing the same port-conflict handling as `cwcli start`.
The confirmation prompt discloses this before you confirm.
If the project cannot be started, or the backup fails, the removal is aborted: all data is kept, the project is returned to its stopped state, and the command exits non-zero - fix the problem and retry, or pass `--no-backup` to delete without a backup.
An orphan project (no containers left to start) is likewise refused on the `--volumes` path unless `--no-backup` is given.
Under `--no-volumes` no volume data is destroyed, so a stopped project is cleaned up without a backup as before.

```bash
cwcli rm [OPTIONS] [PROJECT_NAME]...
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The name(s) of the Frappe project(s) to remove (can be piped from stdin) |

**Options:**

| Option | Description |
|--------|-------------|
| `--volumes` / `--no-volumes` | Remove the named Docker volumes (databases, sites, files). Default: `--volumes`. Use `--no-volumes` to keep them |
| `--no-backup` | Skip database backups before removal, including the backup safety gate (also skips recaching; faster but risky) |
| `-y`, `--yes` | Skip the confirmation prompt and proceed with removal |
| `-v`, `--verbose` | Enable verbose diagnostic output |

By default `cwcli rm` removes the containers, removes the project's named Docker volumes (where the databases, sites, and files live), deletes the local project directory at `~/.cwcli/projects/{project_name}/`, and clears the project from the cache.

`--no-volumes` preserves the named Docker volumes so the project can be recreated from existing data, but still removes the containers, the local project directory, and the cache entry.
The project directory is always removed because it is cwcli configuration, not data.

If a project has no running containers but still has orphaned named volumes or a lingering local directory (for example after a partial removal), `cwcli rm` cleans up that leftover state instead of reporting the project as not found.

An invalid project name (empty, `.`, `..`, an absolute path, or one containing a path separator) is rejected before anything is removed, since such a name could otherwise escape the projects directory. `cwcli rm` exits non-zero whenever any removal step fails - a blocked backup, a rejected name, or a volume, directory, or container that could not be removed - and does not print the success summary for a project that was not fully removed; the project stays in the cache (and `cwcli ls`) so the leftover state remains visible and can be retried.

**Examples:**

```bash
# Remove everything (with confirmation)
cwcli rm my-project

# Keep the named volumes, remove containers and project directory only
cwcli rm my-project --no-volumes

# Skip backups (not recommended)
cwcli rm my-project --no-backup

# Skip the confirmation prompt
cwcli rm my-project --yes

# Remove multiple projects via pipe
cwcli ls | cwcli rm
```

---

### `logs` - View Bench Logs

View the bench's process logs. supervisord writes one log file per Procfile
program under `<bench>/logs/<program>.supervisor.log` on the workspace volume (so
they survive a container restart, with built-in rotation). With no `--process`,
`cwcli logs` synthesizes a combined view across every program; `--process <label>`
tails just one program's log.

**Not under cwcli supervision:** if a bench is running but was NOT started by
cwcli's supervisord - a plain `bench start`, or an instance already running before
cwcli's supervisor existed - `cwcli logs` does not falsely report no logs. It falls
back to discovering and tailing the bench's real `<bench>/logs/*.log` files
(honcho's log names differ from supervisord's per-process files), with `--process`
filtered by file stem. `cwcli logs` only reads - it never launches or installs
supervisord.

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
| `-f`, `--follow` / `--no-follow` | Follow log output in real-time (default: no-follow - print the tail and exit) |
| `-n`, `--lines INTEGER` | Number of lines to show from the end of the logs (default: 100) |
| `-p`, `--process TEXT` | Tail ONE process's log (e.g. `web`, `worker`, `socketio`). Omit for a combined view of every process |
| `--bench TEXT` | Which bench's logs to view: its numeric index or label (multi-bench projects) |
| `-y`, `--yes` | Auto-start stopped containers without prompting |
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Examples:**

```bash
# Combined view: last 100 lines of every process, then exit (default)
cwcli logs frappe-one

# Tail just the web process's log
cwcli logs frappe-one --process web

# Show last 50 lines and exit
cwcli logs frappe-one --lines 50

# Follow logs in real-time
cwcli logs frappe-one --follow

# Show last 200 lines and follow
cwcli logs frappe-one -n 200 --follow
```

**Note:** Per-process logs are stored at `<bench>/logs/<program>.supervisor.log` on the workspace volume inside the container.

`cwcli logs` propagates `tail`'s own exit code rather than always reporting
success (a killed `tail` exits 137, and so does `cwcli logs`). Stopping with
Ctrl-C is not a failure: `tail` exiting 130 (docker forwards `^C` into the
container on the interactive path) and a direct `KeyboardInterrupt` (the
non-TTY path) both print `Stopped viewing logs.` and exit 0.

---

### `inspect` - Inspect Project Structure

Inspects a project to find all bench instances, sites, and apps within it. Results are cached for faster subsequent operations.

**Freshness:** When a project is already cached and its containers are running, `inspect` runs a lightweight, read-only freshness pass over the known benches (a quick `ls` of `apps/`/`sites/`) before showing results.
This means an app you just installed is picked up automatically, without a manual `cwcli inspect --update`.
If nothing changed, the cached data is served unchanged; if that pass detects a change, it transparently falls back to a full re-inspect so the per-site installed-app lists are refreshed too.
Use `--no-refresh` to skip the pass and return the cached data as-is (fastest, but possibly stale), or `--update` to force a full re-inspect.

**Security Note:** The cache never stores Frappe secrets. Site and common configurations are whitelist-filtered before they are written, so database passwords, per-site encryption keys, admin/root passwords, and Redis URLs are stripped and never persisted (nothing reads them back from the cache - every credential consumer reads live from the container or from CLI flags/prompts). Any cache written before this behavior shipped is cleaned in place on the next run. As defense-in-depth, the cache is also stored with restricted filesystem permissions (directory: `0700`, database: `0600`) so only the current user can read it; still, do not share the cache directory (`~/.cwcli/cache/`) with untrusted users.

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
| `--no-refresh` | Return cached data as-is, skipping the lightweight freshness pass (fastest; may be stale) |
| `-i`, `--interactive` | Prompt for a durable [label](#working-with-multiple-benches) for each bench (persisted to the cache and a marker file inside the bench). Requires a running container to write the marker |
| `-y`, `--yes` | Auto-start stopped containers without prompting (non-interactive) |

**What It Caches:**
- Bench instances and their paths
- Sites and installed apps for each bench
- Site configurations (non-secret keys only, e.g. database name, developer mode settings; passwords and encryption keys are stripped)
- Common site configuration (non-secret keys only, e.g. ports, default site, developer mode; Redis URLs and other secrets are stripped)
- Default-site pointer from `sites/currentsite.txt` (written by `bench use`), so the `(default)` marker and default-site resolution work even when `common_site_config.json` has no `default_site` key
- Default site is labeled with `(default)` in output (resolved from either source)

**Example Output:**

Each bench is shown with its numeric index (its default `--bench` selector) and any user label:

```
Project frappe-one
├── Bench [0] 'primary' at /workspace/frappe-bench
│   ├── Site: frappe-one.localhost (default)
│   │   ├── App: frappe (v15.0.0, develop)
│   │   └── App: erpnext (v15.0.0, version-15)
│   └── Site: site2.localhost
│       ├── App: frappe (v15.0.0, develop)
│       └── App: erpnext (v15.0.0, version-15)
└── Bench [1] at /workspace/frappe-bench-2
    └── Site: site3.localhost
        └── App: frappe (v15.0.0, develop)
```

**Benefits:**
- Enables default site feature: `unlock` command can omit `--site` flag
- Faster subsequent operations (uses cached data)
- Stores configurations for programmatic access
- Assigns numeric indices to benches so `--bench` can target one in a multi-bench project (see [Working with Multiple Benches](#working-with-multiple-benches))

**Examples:**

```bash
# Inspect and cache project structure
cwcli inspect frappe-one

# Force refresh the cache
cwcli inspect frappe-one --update

# Return cached data as-is, skipping the freshness pass (fastest)
cwcli inspect frappe-one --no-refresh

# Get JSON output
cwcli inspect frappe-one --json

# Interactively label each bench (durable handle for --bench)
cwcli inspect frappe-one --interactive
```

---

### `label` - Manage Bench Labels

Assigns, clears, or lists per-bench user labels for a project. A user label is a durable, human-friendly handle for a bench in a [multi-bench](#working-with-multiple-benches) project (numeric indices are positional and can shift), and is the value you pass to `--bench` on the bench-operating commands.

```bash
cwcli label [OPTIONS] PROJECT_NAME [SELECTOR] [NEW_LABEL]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name (required) |
| `SELECTOR` | Which bench to label: its numeric index or an existing label. Omit to list the benches |
| `NEW_LABEL` | The new label to assign. Omit and pass `--clear` to remove the label |

**Options:**

| Option | Description |
|--------|-------------|
| `--clear` | Remove the selected bench's user label (revert to its numeric index) |
| `-v`, `--verbose` | Print the resolved marker file path and any operation notes on completion |

**Label rules:**

- May contain only letters, digits, dot (`.`), dash (`-`), and underscore (`_`), up to 64 characters
- May **not** be purely numeric (numeric selectors are reserved for bench indices)
- Must be unique within the project

**Behavior:**

- Run `cwcli inspect <project>` first so the benches are cached; without a cache the command errors.
- Listing benches (bare `cwcli label <project>`) is read-only and needs no running container.
- Setting or clearing a label writes it to **both** the SQLite cache and a marker file (`<bench-root>/.cwcli/.bench-label`) inside the bench, so labels survive a cache wipe and can be rebuilt by a full `cwcli inspect --update`. Because the marker lives inside the bench, setting or clearing a label requires a running frappe container; it will **not** auto-start a stopped project.

**Examples:**

```bash
# List benches with their indices and labels
cwcli label my-project

# Label bench index 1 as 'staging'
cwcli label my-project 1 staging

# Rename label 'staging' to 'prod'
cwcli label my-project staging prod

# Remove bench 1's label
cwcli label my-project 1 --clear
```

---

### `open` - Open in VS Code, Cursor, or Docker Exec

Opens a project's frappe container in VS Code/Cursor (with Dev Containers) or executes into it.

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
| `--bench TEXT` | Which bench to open: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`; cannot be combined with it) |
| `-a`, `--app TEXT` | App name to open (opens the app's directory within the bench) |
| `--code` | Open with VS Code directly (skips interactive prompt) |
| `--code-insiders` | Open with VS Code Insiders directly (skips interactive prompt) |
| `--cursor` | Open with Cursor directly (skips interactive prompt) |
| `--docker` | Open with Docker exec directly (skips interactive prompt) |
| `-y`, `--yes` | Auto-start stopped containers without prompting |
| `-v`, `--verbose` | Enable verbose diagnostic output |

**Features:**

- Auto-detects VS Code, VS Code Insiders, and Cursor installations
- Interactive editor selection menu (when no editor flag specified)
- Direct editor selection via `--code`, `--code-insiders`, `--cursor`, or `--docker` flags
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

### `apps` - Manage Frappe Apps

First-class app management: list, install, uninstall, and update Frappe apps per bench and per site.
This replaces dropping to the raw `cwcli run <project> bench get-app ...` escape hatch: every subcommand resolves the target bench (`--bench <index|label>`/`--path`), returns honest non-zero exit codes, and honors the non-interactive contract (a non-TTY without the required flag refuses non-zero; auto-start gated by `-y`/`--yes`). Every subcommand, `update` included, supports `--json` machine-readable output.

**Multi-site by default:** `install`, `uninstall`, and `apps update` apply to **all** sites on the resolved bench when no `--site` is given; `--site` is repeatable and narrows to the named site(s).
The fan-out runs per site, aggregates the results, and exits non-zero if any site fails (printing a per-site report) - a partial failure is never hidden behind a success banner.
After a successful mutation the bench's cached app lists are refreshed so `where`/`open`/`inspect` reflect the new state.

```bash
cwcli apps list [OPTIONS] PROJECT_NAME
cwcli apps install [OPTIONS] PROJECT_NAME APPS...
cwcli apps uninstall [OPTIONS] PROJECT_NAME APPS...
cwcli apps update [OPTIONS] PROJECT_NAME APPS...
```

**`apps list`** - lists apps available in the bench (live `ls apps/`); with `--installed`/`--site` it also lists the apps installed per site (all sites by default, grouped by site).

**`apps install`** - fetches (`bench get-app`, honoring `--branch`) and installs each app on the target site(s). Each `APP` is a known app name **or** a git URL (passed straight to `bench get-app`, so custom apps not in bench's registry work). `--fetch-only` fetches without installing on any site.

**`apps uninstall`** - removes each app from the target site(s) (`bench --site <site> uninstall-app`). This destroys site data, so it is gated by `-y`/`--yes` or an interactive confirmation (a non-TTY without `--yes` refuses).

**`apps update`** - the canonical app-update path (what the deprecated `cwcli update` now delegates to). Updating the `frappe` framework app runs `bench update --reset`; other apps use the normal git-pull + migrate flow. `--site` narrows which affected sites are migrated; if none of the named site(s) actually have the app installed, the command refuses and exits non-zero rather than silently migrating nothing (a genuine typo/mismatch guard - a bench with no affected sites at all still exits zero). It accepts the same migration flags as the [deprecated `update` command](#update---update-apps-and-migrate) (`--clear-cache`, `--clear-website-cache`, `--build`, `--skip-maintenance`, `--no-recache`). When updating the `frappe` framework app the flow runs the bench-wide `bench update --reset`, so `--site` and those per-app migration flags do not apply and are reported as ignored.

**Common Options:**

| Option | Description |
|--------|-------------|
| `--bench TEXT` | Which bench to target: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`) |
| `--site TEXT` | Target site(s); repeatable. Omit for all sites (list/install/uninstall) or all affected sites (update) |
| `--json` | Machine-readable JSON output (for `install`/`uninstall`, includes the per-`(app, site)` results; for `update`, the full per-phase report). Stdout carries only the document - bench output never corrupts it |
| `-y`, `--yes` | Auto-start stopped containers without prompting; for `uninstall`, also skip the destructive confirmation |
| `-v`, `--verbose` | Enable verbose output |

**Examples:**

```bash
# List apps available in the (only/selected) bench
cwcli apps list frappe-one

# List apps installed across every site, as JSON
cwcli apps list frappe-one --installed --json

# Install ERPNext on every site of the bench
cwcli apps install frappe-one erpnext

# Install a custom app from a git URL on one site
cwcli apps install frappe-one https://github.com/example/custom_app --site dev.localhost

# Fetch an app into the bench without installing it anywhere
cwcli apps install frappe-one payments --fetch-only

# Uninstall an app from all sites, non-interactively
cwcli apps uninstall frappe-one payments --yes

# Update an app (or the framework) - multi-bench aware
cwcli apps update frappe-one erpnext
cwcli apps update frappe-one frappe        # runs 'bench update --reset'
```

---

### `update` - Update Apps and Migrate

> **Deprecated:** use [`cwcli apps update`](#apps---manage-frappe-apps) instead. `cwcli update` keeps working as an alias (and now prints a deprecation notice), so existing scripts are unaffected.

Updates specified Frappe apps and migrates all sites where they are installed.
Updating the `frappe` framework app runs `bench update --reset`.

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
| `--bench TEXT` | Which bench to target: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`; cannot be combined with it) |
| `--site TEXT` | Narrow migration to the named site(s); repeatable. Omit to migrate all affected sites. Refuses non-zero if none of the named sites are actually affected |
| `-v`, `--verbose` | Enable verbose output with streaming command execution |
| `-c`, `--clear-cache` | Clear cache for all affected sites after migration |
| `-w`, `--clear-website-cache` | Clear website cache for all affected sites after migration |
| `-b`, `--build` | Build assets after updating apps |
| `--skip-maintenance` | Skip enabling maintenance mode for affected sites during update |
| `-y`, `--yes` | Auto-start stopped containers without prompting |

**What It Does:**

1. Runs `git pull` in each specified app directory
2. Identifies all sites where the updated apps are installed
3. Enables maintenance mode for affected sites (to prevent user access during updates)
4. Runs `bench --site <site> migrate` for each site that entered maintenance mode (a site that could not be put into maintenance is skipped, reported as an error, and the command exits non-zero - it is never migrated unmaintained)
5. (Optional) Runs `bench build --app <app>` for successfully updated apps
6. (Optional) Runs `bench --site <site> clear-cache` for each migrated site
7. (Optional) Runs `bench --site <site> clear-website-cache` for each migrated site
8. Automatically clears locks folder for each migrated site to prevent stale locks
9. Disables maintenance mode for affected sites after completion (a site that cannot be taken back out of maintenance is reported as an error and the command exits non-zero)

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
| `-s`, `--site TEXT` | Site name to unlock. If not provided, uses the default site (from `common_site_config.json`'s `default_site` or `sites/currentsite.txt`) |
| `--bench TEXT` | Which bench to target: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`; cannot be combined with it) |
| `-y`, `--yes` | Auto-start stopped containers without prompting |
| `-v`, `--verbose` | Enable verbose output and print the removed paths on completion |

**What It Does:**

Removes the `{bench_path}/sites/{site_name}/locks` directory, which can help resolve issues when a site is stuck in a locked state due to incomplete migrations or background jobs.
A site with no locks folder is reported as an honest "already unlocked" success rather than a generic "unlocked" message.

**Smart Defaults:**
- If `--site` is not specified, automatically uses your bench's default site (from `common_site_config.json`'s `default_site`, or `sites/currentsite.txt` when that key is absent)
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

### `backup` - Back Up a Site

Runs `bench backup` for a site inside the project's frappe container, optionally including public and private files.

```bash
cwcli backup [OPTIONS] PROJECT_NAME
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROJECT_NAME` | The Docker Compose project name (required) |

**Options:**

| Option | Description |
|--------|-------------|
| `-s`, `--site TEXT` | Site name to back up. If not provided, uses the default site (from `common_site_config.json`'s `default_site` or `sites/currentsite.txt`) |
| `--bench TEXT` | Which bench to target: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`; cannot be combined with it) |
| `--with-files` | Include public and private files in the backup |
| `-y`, `--yes` | Auto-start stopped containers without prompting |
| `-v`, `--verbose` | Enable verbose output |

**Examples:**

```bash
# Back up the default site (database only)
cwcli backup my-project

# Back up a specific site
cwcli backup my-project --site example.com

# Include public and private files
cwcli backup my-project --with-files

# Back up a specific bench in a multi-bench project
cwcli backup my-project --bench staging --with-files
```

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
| `-s`, `--site TEXT` | Site name to restore. If not provided, uses the default site (from `common_site_config.json`'s `default_site` or `sites/currentsite.txt`) |
| `--latest` | Non-interactive: select the most recent backup set for the target site (bypasses the backup menu). A non-TTY without any selector exits non-zero instead of hanging |
| `--backup-file TEXT` | Non-interactive: select the backup set whose database file matches this filename (or full container path). Bypasses the backup menu |
| `--bench TEXT` | Which bench to target: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`; cannot be combined with it) |
| `--mariadb-root-username TEXT` | MariaDB root username (defaults to `root`; prompted interactively when omitted) |
| `--mariadb-root-password TEXT` | MariaDB root password. Prompted interactively when omitted; a non-TTY without this flag refuses rather than proceeding with an empty password |
| `--admin-password TEXT` | Set administrator password after restore |
| `--send` | **P2P Mode:** Share backup with another machine via peer-to-peer transfer |
| `--receive` | **P2P Mode:** Receive backup from another machine via peer-to-peer transfer |
| `--ticket TEXT` | The sendme ticket for `--receive`. When supplied, the ticket prompt is skipped; a non-TTY without `--ticket` refuses with a non-zero exit |
| `--no-recache` | **Deprecated no-op:** the missing-apps check now reads app availability live from the bench, so it never re-caches. Kept for backward compatibility |
| `--no-migrate` | Skip the post-restore `bench migrate` and instance restart. By default a successful restore runs `bench migrate` (bringing the restored DB to the code's schema) then restarts the instance |
| `-y`, `--yes` | Skip the interactive confirmation prompts on both the normal and `--receive` restore paths (the destructive-restore confirmation and the missing-apps prompt). A non-TTY without `--yes` refuses these and exits non-zero. Does not remove the sendme-ticket or MariaDB-credential prompts |
| `-v`, `--verbose` | Enable verbose output and show restore command details |

**What It Does:**

1. Scans all backup files across all sites in the bench
2. Presents an interactive menu with backups grouped by target site
3. Shows badges indicating backup contents: `[FILES]`, `[PRIVATE]`, `[DATABASE ONLY]`
4. Warns if the selected backup needs apps this bench does not have (read from the backup's own database dump, so a missing app is caught before the restore)
5. Automatically detects and restores public/private file archives
6. Restores encryption key from backup's site_config if available
7. Runs `bench migrate` and restarts the instance after a successful restore, so the restored database is brought to the code's schema and the app comes back up cleanly (skip with `--no-migrate`; a failed migrate is surfaced and exits non-zero but does not undo the restore)
8. Displays helpful error messages on failure with common causes

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

**Non-interactive / scripted restores:**

The backup-selection menu and both confirmation prompts (the destructive-restore and missing-apps warnings) can be skipped so a `cwcli restore` runs to completion with no prompts.
Pass `--latest` or `--backup-file <name>` to pick a backup without the menu, `--yes` to skip the confirmations, and `--mariadb-root-password` (and `--mariadb-root-username` if not `root`) for credentials.
A non-TTY missing any required selector or confirmation exits non-zero with a message naming the flag to pass, so a missing piece never hangs or silently succeeds.

```bash
# Restore the newest backup for the default site, fully non-interactive
cwcli restore my-project --latest --yes --mariadb-root-password "secret123"

# Restore a specific backup file by name or full container path
cwcli restore my-project --backup-file 20251112_105638-development_localhost-database.sql.gz \
  --yes --mariadb-root-password "secret123"

# Restore into a specific site non-interactively
cwcli restore my-project --site production.localhost --latest --yes \
  --mariadb-root-password "secret123"
```

> A declined confirmation (interactive `n`, or Ctrl-C), or a non-TTY run without `--yes` at a confirmation, exits non-zero rather than `0`.
> `--latest` and `--backup-file` are mutually exclusive, and neither applies to `--send`/`--receive` (those modes never reach the backup menu).


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
? MariaDB root username: root
MariaDB root password: ********

✓ Successfully restored site 'development.localhost'
From backup: 20251112_105638-development_localhost-database.sql.gz
Including file archives
Updated encryption_key from backup site_config

✓ Migrated site 'development.localhost'

Restarting instance...
✓ Instance restarted (logs: /workspace/frappe-bench/logs/web.dev.log)
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

# Or pass the ticket non-interactively (required on a non-TTY):
cwcli restore my-project --receive --ticket blob... \
  --mariadb-root-password secret --yes

# Files download with hash verification
✓ Files downloaded successfully

# Before overwriting the site, cwcli confirms the destructive restore
⚠ Warning: This will replace all data in site 'development.localhost'
Backup: 20251112_105638-development_localhost-database.sql.gz
? Are you sure you want to restore? (y/N) y
# Restore process begins
```

For scripted (non-interactive) receives, pass `-y`/`--yes` to skip the confirmation and the missing-apps prompt. Without a TTY and without `--yes`, cwcli refuses and exits non-zero rather than silently overwriting the site's data.

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

- All command inputs (site name, paths, MariaDB username) are shell-quoted to prevent command injection
- MariaDB and admin passwords are passed to the container via the environment and referenced as `$VAR`s, so they never appear in the command string itself or in verbose output; the leaf `bench`/`frappe` process still briefly shows the plaintext value on its own argv (visible via `docker top`) while the operation runs, since `bench` accepts passwords only as a flag - an inherent bench limitation, not a cwcli gap
- Backup file existence verified before restore
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
| `--bench TEXT` | Which bench to target: its numeric index or label (see [Working with Multiple Benches](#working-with-multiple-benches)) |
| `-p`, `--path TEXT` | Explicit bench directory inside the container (lower-level alternative to `--bench`; cannot be combined with it). Falls back to the single cached bench, or `/workspace/frappe-bench` when nothing is cached |
| `-y`, `--yes` | Auto-start stopped containers without prompting |
| `-v`, `--verbose` | Enable verbose output |

**Passing bench's own flags:** a flag `cwcli run` does not define itself, such as
`--site`, `--branch`, or `--force`, is passed straight through to bench.
The options in the table above are the exception: they stay `cwcli`'s wherever
they appear, so `cwcli run frappe-one migrate --bench staging` targets the
`staging` bench rather than passing `--bench staging` to bench.
To send a flag that collides with one of those names, put it after a `--`
separator, which hands everything following it to bench verbatim:

```bash
# `--verbose` reaches bench instead of turning on cwcli's verbose output
cwcli run frappe-one -- build --verbose
```

For app management, prefer the [`apps`](#apps---manage-frappe-apps) group, which
takes these flags directly.

**Examples:**

```bash
# Run bench migrate
cwcli run frappe-one migrate

# Run bench with specific site (bench's own flags pass straight through)
cwcli run frappe-one --site development.localhost migrate

# Install an app from a branch
cwcli run frappe-one get-app --branch develop https://github.com/user/app.git

# Execute a custom bench command
cwcli run frappe-one console

# Target a specific bench in a multi-bench project
cwcli run frappe-one migrate --bench staging

# Use custom bench path
cwcli run frappe-one migrate --path /workspace/custom-bench
```

`cwcli run` exits with the bench command's own exit code. If the connection to
the command's output stream is lost, it reports that and exits non-zero rather
than claiming success.

---

### `status` - Check Health Status

Reports the health of a Frappe project instance: a single aggregate token on
stdout, with the per-process breakdown on stderr.

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
| `--bench` | Which bench to report: its numeric index or label (multi-bench projects) |
| `-v`, `--verbose` | Show the per-process detail and the web HTTP probe on stderr |
| `-w`, `--watch` | Live, continuously-refreshing per-process view (Ctrl-C to exit) |
| `--interval` | Seconds between `--watch` refreshes (default 2, floored at 1s) |

**Aggregate values** (printed on stdout, one token, exit 0):

- **`offline`** - a real-but-stopped instance: the containers exist but the frappe container is not running
- **`online`** - the container is up, but the bench was never started (no supervisor)
- **`running`** - supervisord is up, every expected program is healthy, and the web server answers on `:8000`
- **`degraded`** - the bench was started but the supervisor is down (e.g. after a
  container restart), OR it is up but a program is not healthy (a `FATAL`/`BACKOFF`
  worker while the rest keeps serving - a stable partial stack), OR the web server
  is not answering

A truly-nonexistent project name (a typo, or one never created - no containers
with the label at all) is NOT `offline`: it exits non-zero with a "no such
project" error on stderr, so a script can tell a stopped instance apart from a
name that does not exist.

**Not under cwcli supervision:** if a bench is running but was NOT started by
cwcli's supervisord - a plain `bench start`, or an instance already running before
cwcli's supervisor existed - `status` does not report every process as down. It
falls back to detecting the honcho / `bench start` process tree and reports each
process's true `up`/PID/uptime, aggregating to the honest `running`/`degraded`. The
report is flagged `not_cwcli_supervised` (shown as `(not under cwcli supervision)`
in the human heading, `not_cwcli_supervised: true` in `cwcli axi status`) with a
hint to run `cwcli start` to bring it under cwcli's supervisor. In this mode the
per-process supervisord `state` is unavailable (there is no supervisord to ask);
`up` comes straight from `ps`. `status` only reads - it never launches supervisord.

**Per-process detail (stderr):** each Procfile process (`web`, `socketio`,
`worker`, `schedule`, `watch`, `redis_cache`, `redis_queue`) with up/down plus
its PID, uptime, CPU%, RSS, and supervisord's authoritative `state`
(`RUNNING`/`STARTING`/`BACKOFF`/`EXITED`/`FATAL`/`STOPPED`) - so a crash-looping or
give-up program is visible, not just "down". The stdout token stays a single word
so it is safe to script against; the detail is on stderr for humans (and in
structured form via `cwcli axi status`).

**Live view (`--watch`):** `cwcli status --watch frappe-one` shows a continuously
refreshing per-process table (rendered on stderr; Ctrl-C exits cleanly). Unlike
the one-shot command, the watch loop **never probes the web server** - it reads
process health from `ps` + supervisord's control socket each tick (neither touches
`:8000`), so watching leaves ZERO `GET localhost:8000` requests in the bench's
access logs. When stdout or stderr
is not a TTY (piped or redirected - the live view renders to stderr, so both
streams must be interactive), `--watch` degrades to a single quiet snapshot
instead of starting the live loop. (`cwcli axi status` stays one-shot - agents
re-invoke it for fresh reads rather than consuming a live stream.)

**Example:**

```bash
cwcli status frappe-one              # -> running
cwcli status frappe-one -v           # token on stdout, per-process table on stderr
cwcli status --watch frappe-one      # live table, no web-log spam; Ctrl-C to exit
cwcli status -w --interval 5 frappe-one   # refresh every 5s
```

---

### `config` - Manage Configuration

Manages the CLI configuration and cache.

```bash
cwcli config [SUBCOMMAND]
```

#### Subcommands

##### `config show` - Show the Effective Configuration

The one read for everything: the custom search paths, the auto-inspect settings together with live daemon state and boot-hook state, the tips setting, and the config-file and cache-DB locations.

```bash
cwcli config show
cwcli config show --json    # stable machine-readable object on stdout
```

##### `config path` - Show Config Path

Prints the configuration file's path - bare, unwrapped at any terminal width, safe for command substitution.

```bash
cwcli config path
$EDITOR "$(cwcli config path)"
```

##### `config edit` - Edit the Config File

Opens the configuration file in `$EDITOR`.

```bash
cwcli config edit
```

##### `config paths` - Manage Bench Search Paths

Lists and edits the custom directories the `inspect` command searches for benches.

```bash
cwcli config paths                          # list, one path per line
cwcli config paths --json                   # the same list as JSON
cwcli config paths add /home/user/benches   # absolute paths only
cwcli config paths remove /home/user/benches
```

`add` refuses a non-absolute path with a usage error (exit 2) after `~` expansion, and normalizes before duplicate detection, so `/a/b` and `/a/b/` are one entry, not two.
Adding an already-present path and removing an absent one are both no-op successes (exit 0, saying which no-op occurred).

##### `config cache` - Manage Cache

Manages the cache for project inspection data.

```bash
cwcli config cache [SUBCOMMAND]
```

**Cache Subcommands:**

- **`clear [PROJECT_NAME]`** - Clear cache for a specific project or the entire cache
  - Options:
    - `-a`, `--all` - Clear the entire cache
    - `-y`, `--yes` - Skip the confirmation prompt (required to clear `--all` non-interactively; a non-TTY without `--yes` refuses rather than wiping the cache silently)
  - A project name combined with `--all` is a usage error (exit 2, nothing cleared) - contradictory targets are never resolved toward the more destructive reading. No target at all is likewise exit 2.
  - Example: `cwcli config cache clear frappe-one`
  - Example: `cwcli config cache clear --all --yes`

- **`path`** - Print the cache file's path (bare, substitution-safe)
  - Example: `cwcli config cache path`

- **`list [--json]`** - List all projects currently in the cache
  - Example: `cwcli config cache list --json`

##### `config auto-inspect` - Automatic Project Inspection

Manages automatic background inspection of running Frappe projects to keep cached data fresh (tab completion, project status queries, and other commands that rely on cached data).

```bash
cwcli config auto-inspect [SUBCOMMAND]
```

Auto-inspect has three state stores - the config flag, the live daemon process, and the OS boot hook - and the verbs are desired-state: one `enable` reaches "enabled and running", one `disable` tears all three down.

**Auto-Inspect Subcommands:**

- **`enable`** - Enable auto-inspect AND start the background process, in one verb
  - Options:
    - `-i`, `--interval INTEGER` - Inspection interval in seconds (minimum 60, default 3600)
    - `--startup` / `--no-startup` - Also install (or remove) the automatic start on system boot/login; omit both to leave the boot hook untouched
  - Validates every input BEFORE persisting anything: a failing `enable` leaves the config file untouched.
  - Idempotent: re-running applies changed settings (restarting the running daemon when the interval changed) and reports already-satisfied state as a no-op.
  - Example: `cwcli config auto-inspect enable --interval 1800 --startup`

- **`disable`** - Stop the background process, set enabled = false, AND remove the boot hook, reporting each action taken
  - Example: `cwcli config auto-inspect disable`

- **`stop`** - Stop the background process only (stays enabled; a boot-hooked daemon returns at the next boot)
  - Example: `cwcli config auto-inspect stop`

- **`status [--json]`** - Show all three stores separately: enabled + interval (config), process state + PID (daemon), start-on-boot (OS hook)
  - Example: `cwcli config auto-inspect status --json`

- **`logs`** - View recent background process logs (a failed read exits 1)
  - Options: `-n`, `--lines INTEGER` - Number of log lines to show (default 20)
  - Example: `cwcli config auto-inspect logs --lines 50`

**Notes:**
- Background process survives terminal closure
- Process stops on system restart unless the boot hook is installed (`enable --startup`; LaunchAgent on macOS, systemd user service on Linux, Task Scheduler on Windows)
- Logs stored in `~/.cwcli/run/auto-inspect.log`
- PID file stored in `~/.cwcli/run/auto-inspect.pid`

##### `config tips` - Manage Contextual Tips

Control the display of helpful tips during long-running operations.

```bash
cwcli config tips [enable|disable]
```

When enabled (default), cwcli displays rotating helpful tips alongside spinners during long-running operations like `inspect`, `update`, and `open`. The current setting shows in `cwcli config show`.

#### Deprecated `config` spellings

These verbs remain as hidden aliases with byte-identical behavior plus a one-line stderr deprecation warning. They will not be removed before 1.0, and no earlier than two minor releases after the rework shipped - whichever is later.

| Deprecated | Use instead |
|------------|-------------|
| `config add-path P` | `config paths add P` (both now refuse non-absolute paths, exit 2) |
| `config remove-path P` | `config paths remove P` |
| `config auto-inspect start` | `config auto-inspect enable` |
| `config auto-inspect restart` | `config auto-inspect enable` (idempotent; restarts on interval change) |
| `config auto-inspect set-interval N` | `config auto-inspect enable --interval N` |
| `config auto-inspect install-startup` | `config auto-inspect enable --startup` |
| `config auto-inspect uninstall-startup` | `config auto-inspect enable --no-startup` (keep running, drop the hook) or `disable` |
| `config tips status` | `config show` |

**Boot-unit note:** startup units installed before this change (systemd/LaunchAgent/schtasks) exec `cwcli config auto-inspect start` verbatim.
The `start` alias keeps BOTH that argv and its refuse-when-disabled guard, so an existing unit keeps working and a stale hook left behind after `disable` stays inert.
Re-running `cwcli config auto-inspect enable --startup` rewrites the unit against the current executable path.

### `self-update` - Upgrade cwcli Itself

Upgrades cwcli to the latest version published on PyPI. It detects how cwcli was installed and runs the matching upgrade for you, so you don't have to remember whether you used `uv` or `pip`.

```bash
cwcli self-update [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--check` | Dry run: report current vs latest and print the upgrade command without executing. Exits non-zero when an update is available (so scripts/CI can gate on it) |
| `--no-cache` | Force a fresh PyPI check, ignoring the shared ≤1-day version cache. Use right after publishing a release |

**How it behaves:**

- **uv tool install** → runs `uv tool upgrade caffeinated-whale-cli`.
- **pip install** → runs `python -m pip install --upgrade caffeinated-whale-cli` (using the current interpreter).
- **dev/editable checkout** → a no-op; it tells you to `git pull` instead of self-modifying your source tree.
- **`uvx --from ... cwcli`** (ephemeral) → a no-op; there is nothing persistent to upgrade.

**Exit codes:**

| Code | When |
|------|------|
| `0` | Already up to date (including a dev checkout that is ahead of PyPI), upgraded successfully, or a dev/uvx no-op. Also a `--check` that found no update, or a `--check` whose network lookup failed open |
| `1` | The upgrade subprocess failed, a network failure blocked an actual upgrade, or `--check` found that an update is available |

**Examples:**

```bash
# Upgrade cwcli if a newer version exists
cwcli self-update

# Report only; exit 1 if an update is available (handy in CI)
cwcli self-update --check

# Force a fresh PyPI check, bypassing the version cache
cwcli self-update --no-cache
```

**Passive update notices:**

Every `cwcli` and `cwcli axi` run also does a passive, cache-only check: if a newer release is already known (from the same ≤1-day cache `self-update`/`--check` share) and you're at an interactive terminal, cwcli prints a one-line "a newer cwcli is available" hint - with the right upgrade command for how you installed it - to stderr. It never makes a blocking network call itself: PyPI is actually re-checked at most once/day, via a detached background refresh kicked off whenever the cache is missing or stale, so this never delays a command; until that refresh lands, the hint keeps showing on every run. It never touches stdout, so it's invisible to pipes, scripts, CI, and `cwcli axi`'s TOON output. Set `CWCLI_NO_UPDATE_CHECK=1` to suppress it entirely.

---

## Working with Multiple Benches

A single cwcli project (one Docker Compose instance) can hold more than one bench directory inside its frappe container. This is a **multi-bench** instance, and cwcli lets you address an individual bench without spelling out its full container path.

**How benches are addressed:**

- **Numeric index** - Every bench discovered by `cwcli inspect` gets an index (`0`, `1`, `2`, ...) from a stable, sorted-by-path order. `cwcli inspect` shows it next to each bench (for example `Bench [1] at /workspace/frappe-bench-2`). An index is positional, so it can shift when a bench is added or removed.
- **User label** - A durable, human-friendly handle you assign with the [`label`](#label---manage-bench-labels) command or `cwcli inspect -i`. Because it does not move when benches are renumbered, a label is the reliable way to target a bench in scripts. Labels may not be purely numeric (that would collide with an index) and must be unique within a project.

**The `--bench` selector:**

Pass `--bench <index|label>` to target one bench. The selector resolves a matching user label first, then a numeric index:

```bash
cwcli run my-project migrate --bench 1          # by index
cwcli update my-project --app erpnext --bench staging   # by label
```

`--bench` is available on `run`, `backup`, `update`, `open`, `unlock`, `restore`, and `start`. Setting labels lives in the [`label`](#label---manage-bench-labels) command.

**No silent guessing on data commands:**

If a project has more than one bench and you do not pass `--bench` (or `--path`), the data commands (`run`, `backup`, `update`, `open`, `unlock`, `restore`) stop and list the benches instead of guessing:

```
Error: project 'my-project' has multiple benches; specify one with --bench <index|label>:
  [0] 'primary' /workspace/frappe-bench
  [1] 'staging' /workspace/frappe-bench-2
```

Single-bench projects are unaffected: with only one bench, that bench is used automatically and `--bench` is optional.

`start`, `status`, and `cwcli restart --process` follow the same family rule: on a multi-bench project with no `--bench` they prompt which bench interactively and refuse (non-zero) on a non-TTY. A whole-stack `cwcli restart` (no `--process`) restarts each project's default bench.

**`--path` escape hatch:** `-p`/`--path` still accepts an explicit bench directory for cases outside the cached set. It takes precedence over `--bench`, but the two cannot be combined (that is an error).

**Label persistence and recovery:** A user label is stored in both the SQLite cache and a marker file (`<bench-root>/.cwcli/.bench-label`) inside the bench. Because the marker lives in the bench itself, a full `cwcli inspect --update` rebuilds labels from the markers even after the cache is cleared.

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

# Parse inspect output (each bench carries its numeric "index" for --bench)
cwcli inspect frappe-one --json | jq '.bench_instances[0].sites'
```

### For agents: the `cwcli axi` surface

`cwcli axi` is an agent-facing surface built for autonomous tools that drive cwcli through shell execution.
It sits on the same logic core as the human commands but renders differently:

- **Structured output on stdout** in [TOON](https://toonformat.dev) (a token-efficient, agent-readable format); progress and diagnostics go to stderr, so stdout is always clean, parseable data.
- **No prompts, ever.** Every operation completes from flags alone; a decision it cannot make (an ambiguous multi-bench project, a stopped instance) is reported as a structured usage error naming the exact flag to pass, not an interactive question. An unrecognized flag or a missing required argument is likewise a structured `error:`+`help:` usage error on stdout - never a leaked stack trace or empty output - with the `help:` line naming the command's valid flags.
- **Conventional exit codes:** `0` success (including no-ops), `1` error, `2` usage error.

```bash
# Content-first home: the binary path, a description, live instances, and next steps
cwcli axi

# List all Frappe/ERPNext instances as a TOON table
cwcli axi ls

# Search cached instances for an app or site by name
cwcli axi where erpnext

# Back up a site's database; the outcome prints as TOON on stdout
cwcli axi backup frappe-one --site development.localhost

# Include files too
cwcli axi backup frappe-one --with-files

# Remove a site's locks folder; the removed paths print as a structured TOON list.
# A site that was not locked is a clean success (already_unlocked: true).
cwcli axi unlock frappe-one --site development.localhost

# Stop a project's containers; the outcome prints as TOON. Idempotent: an
# already-stopped project is a definitive success (already_stopped: true).
cwcli axi stop frappe-one

# Start a project's containers + bench; the outcome (including already_running)
# prints as TOON. --bench selects a bench on a multi-bench project; --yes
# auto-resolves a port conflict by stopping the conflicting Frappe project.
cwcli axi start frappe-one --yes

# Report per-process health as TOON, with the "overall" aggregate up front
cwcli axi status frappe-one

# Restart ONE supervised process (siblings keep running); the outcome prints as
# TOON. --process is required; --bench selects a bench on a multi-bench project.
cwcli axi restart frappe-one --process web

# Inspect a project's benches, sites, and apps; the report prints as ONE TOON
# document. ONE tiered read (cache/partial/full, whichever answers the request),
# not a read/refresh pair. --update forces a full re-inspect; --no-refresh
# serves the cache verbatim with zero container calls.
cwcli axi inspect frappe-one
cwcli axi inspect frappe-one --update

# List a project's benches with their indices and labels. This is what answers
# "pass --bench <index|label>" from any other verb - no other verb can.
cwcli axi benches frappe-one

# Set or clear a bench's durable user label. Exactly one of --set/--clear is
# required; listing is `cwcli axi benches`, not a mode of this verb.
cwcli axi label frappe-one --bench 0 --set staging
cwcli axi label frappe-one --bench staging --clear

# List a bench's available apps, and (with --installed/--site) which apps are
# installed on which site. This answers what is ON the bench that
# `cwcli axi benches` names.
cwcli axi apps list frappe-one
cwcli axi apps list frappe-one --installed
cwcli axi apps list frappe-one --site erp.localhost

# Update app(s) and migrate every affected site; the full per-phase report
# prints as ONE TOON document. Blocks until it finishes (like axi backup) -
# there is no progress stream, because an agent needs a verdict to branch on.
cwcli axi apps update frappe-one erpnext
cwcli axi apps update frappe-one frappe          # runs 'bench update --reset'

# Provision a new instance, bench, and site; the report prints as ONE TOON
# document. BLOCKS for the full 10-20 minute run (like axi apps update) and
# narrates coarse phases to stderr - run `cwcli logs`/`cwcli status` from a
# second shell for live progress. The admin password comes from the
# CWCLI_ADMIN_PASSWORD env var (recommended - off the argv) or --admin-password;
# omitted is a usage error (never generated, never prompted).
CWCLI_ADMIN_PASSWORD=changeme cwcli axi init frappe-two --version 16
cwcli axi init frappe-two --admin-password changeme --install-erpnext

# Report whether a newer cwcli is published. READ-ONLY: --check is required and
# this verb never upgrades. Exits 0 on any successful read - the answer is the
# is_outdated field, not the exit code.
cwcli axi self-update --check

# The effective cwcli configuration in one call. READ-ONLY: search paths,
# auto-inspect state (config, live daemon, boot hook - reported separately),
# tips, and the config-file/cache-DB locations.
cwcli axi config
```

`cwcli axi ls`, `cwcli axi where`, `cwcli axi backup`, `cwcli axi unlock`, `cwcli axi stop`, `cwcli axi start`, `cwcli axi status`, `cwcli axi restart`, `cwcli axi inspect`, `cwcli axi benches`, and `cwcli axi label` run on the same logic core as their human counterparts; only the output (always TOON, never JSON) and choice-handling differ. `cwcli axi start` never prompts: an ambiguous multi-bench project is a `--bench` usage error (exit 2), and an unresolved port conflict is a `CONFLICT` error naming `--yes` (exit 1). `cwcli axi unlock` follows `cwcli axi backup`'s conventions exactly: an ambiguous multi-bench project is a `--bench` usage error (exit 2) and a stopped instance is a usage error pointing at `cwcli start` (exit 2). `cwcli axi stop` is idempotent for an agent - stopping an already-stopped project is a success, not an error. `cwcli axi status` always exits 0, leading with the `overall` aggregate (`offline`/`online`/`running`/`degraded`). `cwcli axi restart` requires `--process`; an unknown/ambiguous process is a usage error listing the valid labels (exit 2). `cwcli axi inspect` serves whichever freshness tier answers the request (`served_from` names it) and, unlike every other bench-scoped verb, has deliberately NO `--yes` - a stopped project on the refresh path is a usage error (exit 2) naming `cwcli start`, and a drift escalation that can no longer discover the bench serves the cached data with `degraded: true` and a warning (exit 0). `cwcli axi benches` is the discovery verb behind every other verb's `--bench`: when a bench-scoped verb reports "multiple benches; pass `--bench <index|label>`", this is what tells you the valid values, and a project that has never been inspected is a structured error naming `cwcli axi inspect` rather than an empty list. `cwcli axi label` never starts a stopped project (the label marker lives inside the bench), and listing is deliberately `cwcli axi benches` rather than a mode of the mutation verb. `cwcli axi self-update --check` is read-only and **exits 0 whenever the check succeeds, including when an update is available** - the answer is the `is_outdated` field, not the exit code, because on the agent surface a non-zero exit means an error. This deliberately differs from the human `cwcli self-update --check`, which exits 1 when an update is available so shell scripts can gate on it; the mutating `cwcli axi self-update` is deliberately not offered. `cwcli axi config` is the read-only counterpart of `cwcli config show`: one TOON document carrying the search paths, all three auto-inspect state stores, the tips setting, and the file locations. **No config-mutating axi verbs exist** (no `paths add`/`remove`, no `cache clear`, no `auto-inspect enable`/`disable`): an agent rewriting the user's search paths or wiping the cache is a product decision that deserves its own evidence, so mutations stay on the human `cwcli config` surface (whose reads all have `--json`).

`cwcli axi apps update` blocks until the update finishes and emits ONE terminal document, exactly as `cwcli axi backup` does for a minutes-long `bench backup`: progress is deliberately not streamed, because N documents on stdout would break the one-TOON-document contract and an agent needs a verdict it can branch on rather than a progress bar (use `cwcli logs`/`cwcli status --watch` if you want live progress). Its exit code is `0` only when the report's `ok` is true; any failed phase, any stuck site, or any unknown outcome exits `1`, and an ambiguous multi-bench project is a `--bench` usage error (exit 2). A stopped project is likewise a usage error pointing at `cwcli start` (exit 2) - there is deliberately no `--yes` on this verb, because starting a container is UI-coupled and the core stays UI-pure about it, so an agent composes `cwcli axi start` then this verb, exactly as `cwcli axi unlock`/`cwcli axi backup` already document. **`failed_*` and `unknown_*` are not the same thing and must not be collapsed:** a `failed_*` item ran and failed, so retrying it is safe, while an `unknown_*` item's output stream was lost - its exit code is unknowable and **it may still be running**, so retrying it (a migration above all) can do real harm. Check before retrying. There is deliberately no `cwcli axi update`: the deprecated `cwcli update` spelling does not get an agent-facing verb. JSON output stays on the human commands (`cwcli ls --json`, `cwcli where --json`, `cwcli apps update --json`).

`cwcli axi apps list` is the read that answers what is *on* the bench `cwcli axi benches` names: the bench's available apps, and with `--installed`/`--site` which apps are installed on which site (only the app name, never the version column `bench list-apps` prints). A site whose read FAILED is reported as `null` and exits `1`, never as an empty list - "has no apps" and "could not tell" are different facts, and only one of them is safe to act on. Like every other bench-scoped verb it takes `--bench`, has no `--yes`, and reports a stopped project as a usage error naming `cwcli start` (exit 2). **`cwcli axi apps install` and `cwcli axi apps uninstall` deliberately do not exist:** letting an agent install into - or drop the tables of - a real site is a product decision that deserves its own evidence, not something settled as a side effect of moving code onto the logic core. Use the human `cwcli apps install`/`cwcli apps uninstall` (both have `--json` and honest exit codes) until that decision is taken.

`cwcli axi init` provisions a new instance, bench, and site the way `cwcli init` does, but non-interactively and as ONE terminal TOON document. It **blocks** for the full 10-20 minute run - the same block-and-emit-one-document shape as `cwcli axi apps update`, because a progress stream would break the one-TOON-document contract - narrating coarse phase labels to stderr; run `cwcli logs <project>` / `cwcli status <project>` from a second shell for live progress. The site administrator password comes from the **`CWCLI_ADMIN_PASSWORD` environment variable (recommended)** or `--admin-password`; the flag wins if both are set, and with neither set the verb refuses with a usage error (exit 2) naming both - it never generates a password and never prompts. The env var keeps the secret off the process argv (visible in `ps`/shell history) that the flag exposes; the container-side `bench new-site` still receives it, so treat the value as one-time. The MariaDB root password takes the same shape via `CWCLI_DB_ROOT_PASSWORD` / `--db-root-password` (default `123`). The interactive decisions the human `cwcli init` prompts for become non-prompting errors: an existing bench with neither `--reuse-bench` nor `--no-reuse-bench` is a usage error (exit 2) naming both; a port conflict names `--port` (exit 1); containers that do not come up point at `cwcli status`/`cwcli logs` (exit 1). There is no `--auto-start` (compose `cwcli axi start` then re-run) and no `--verbose` (stdout is always TOON). `--bench` here is the NAME of the bench to create, distinct from the `--bench <index|label>` selector the other verbs use.

#### Making agents aware of the surface

An agent cannot use a surface it does not know exists.
There are two ways to tell it, and **you only need one**:

**1. The SessionStart hook** (recommended - ambient, plus live state):

```bash
cwcli axi setup
```

This installs a session-start hook into every agent harness it detects - Claude Code (`~/.claude/settings.json`), Codex (`~/.codex/hooks.json`, plus `[features] hooks = true` in `config.toml`), and OpenCode (a managed plugin in `~/.config/opencode/plugins/`).
The hook runs `cwcli axi` once per session and feeds the home view - the verb list *and* the live instance list - into the agent's opening context, so it can act without a discovery call.
It is idempotent (re-running it reports `unchanged`), it repairs the recorded path in place after a reinstall or a move, and it skips any harness that is not installed rather than creating config directories for it.
Restart the agent session to pick it up.

**2. The installable skill** (no per-session token cost, works in any agent):

```bash
npx skills add karotkriss/caffeinated-whale-cli --skill cwcli
```

The skill ([`skills/cwcli/SKILL.md`](./skills/cwcli/SKILL.md)) loads on demand when the agent recognises a Frappe/ERPNext task, and costs nothing on sessions that never touch one.
It is static, so it teaches the surface but cannot show live state - that is the hook's advantage.
It is generated from the CLI's own verb registry (`uv run python scripts/build_skill.py`) and a test fails if the committed copy goes stale, so it cannot drift from the real surface.

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

- **Project names** - All commands that accept project names (start, stop, restart, inspect, label, logs, open, status, run, update, unlock, apps)
- **App names** - Commands with `--app` option or an `APP` argument (open, update, `apps uninstall`, `apps update`)
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

**Logic core:** business logic and I/O live in a UI-pure `core/` package that carries no `rich`/`questionary`/`typer`; it returns a serializable typed envelope (or raises a typed error) so the human CLI, the `cwcli axi` agent surface, and any future GUI are all thin frontends over one implementation.
`backup`, `unlock`, `stop`, `label`, `run`, `ls`/`list`, `where`, `start`/`status`/`restart`, `logs`, `inspect`, `apps`, `update`, `open`, `init`, `config`, `restore`, and `rm` are migrated onto it so far.

**Data Directories:**
- **Projects**: `~/.cwcli/projects/` - Project directories created by `cwcli init`
- **Config**: `~/.cwcli/config/` - Configuration files
- **Cache**: `~/.cwcli/cache/cwc-cache.db` - Project inspection cache
- **Runtime**: `~/.cwcli/run/` - PID and log files for background services
- **Archive**: `~/.cwcli/archive/` - Pre-deletion backups and config snapshots written by `cwcli rm`

**Relocating cwcli's data (`CWCLI_HOME`):**

Set the `CWCLI_HOME` environment variable to move cwcli's entire on-disk footprint - projects, config, cache, runtime, and archive files - out of `~/.cwcli` and into a directory of your choice.
When it is set, cwcli uses `$CWCLI_HOME/projects`, `$CWCLI_HOME/config`, `$CWCLI_HOME/cache`, `$CWCLI_HOME/run`, and `$CWCLI_HOME/archive` in place of the `~/.cwcli/*` locations above.
When it is unset (or empty), cwcli uses the default `~/.cwcli` locations, so existing installs are unaffected.

Unlike repointing `HOME`, `CWCLI_HOME` redirects only cwcli's own state - it does not change your process `HOME`, so `git`, `ssh`, and other tools that read `HOME` are untouched.
The relocated cache keeps the same restrictive permissions as the default (`0700` directory, `0600` database file).

```bash
# Keep a separate, sandboxed cwcli state for one shell session
export CWCLI_HOME=/tmp/cwcli-sandbox
cwcli ls        # reads/writes /tmp/cwcli-sandbox instead of ~/.cwcli

# Or per-invocation, without exporting
CWCLI_HOME=/tmp/cwcli-sandbox cwcli ls
```

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
