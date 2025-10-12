# Caffeinated Whale CLI

A simple command-line interface (CLI) to help manage Frappe Docker instances for local development.

## Installation

Ensure you have Python 3.10+ and `pip` installed.

```bash
pip install caffeinated-whale-cli
```

### Troubleshooting

After installation, if you see an error like `'cwcli' is not recognized...`, it means the installation directory is not in your system's `PATH`. This is a common issue, especially on Windows.

To fix this:

1.  **Find the script's location:** Run `pip show -f caffeinated-whale-cli` and look for the location of `cwcli.exe` (or `cwcli` on macOS/Linux) in the output. It is typically in a `Scripts` or `bin` folder within your Python installation directory.
2.  **Add the location to your PATH:** Follow the instructions for your operating system to add this directory to your `PATH` environment variable.
3.  **Restart your terminal:** You must close and reopen your terminal for the changes to take effect.

## Usage

The Caffeinated Whale CLI provides a main command, `cwcli`, which serves as the entry point for all operations.

```bash
cwcli [COMMAND]
```

### Commands

#### `ls`

Lists all your Frappe/ERPNext projects and their status.

**Usage:**

```bash
cwcli ls
```

**Expected Output:**

A table displaying the key details of your managed projects.

| Project Name | Status  | Ports         |
|--------------|---------|---------------|
| frappe-one   | running | 8000->8000/tcp|
| frappe-two   | exited  |               |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--verbose`, `-v` | Display all ports individually, without condensing them into ranges. |
| `--quiet`, `-q`   | Only display project names, one per line. Useful for scripting. |
| `--json`      | Output the list of instances as a raw JSON string. |

---

#### `start`

Starts a stopped project's containers.

**Usage:**

```bash
cwcli start [PROJECT_NAME]...
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The name(s) of the project(s) to start. Can be piped from stdin. |

**Expected Output:**

A confirmation message indicating the project has started.

```
Starting frappe-one...
frappe-one started successfully.
```

---

#### `stop`

Stops a running project's containers.

**Usage:**

```bash
cwcli stop [PROJECT_NAME]...
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The name(s) of the project(s) to stop. Can be piped from stdin. |

**Expected Output:**

A confirmation message indicating the project has stopped.

```
Stopping frappe-one...
frappe-one stopped successfully.
```

---

#### `restart`

Restarts a project's containers and bench instance.

**Usage:**

```bash
cwcli restart [PROJECT_NAME]...
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The name(s) of the project(s) to restart. Can be piped from stdin. |

**Expected Output:**

A confirmation message indicating the project has restarted.

```
Attempting to restart 1 project(s)...
Instance 'frappe-one' stopped.
Instance 'frappe-one' started.
✓ Started bench (logs: /tmp/bench-frappe-one.log)
View logs with: cwcli logs frappe-one

Restart command finished.
```

---

#### `logs`

View bench logs in real-time from the log file.

**Usage:**

```bash
cwcli logs [PROJECT_NAME]
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The name of the Frappe project to view logs for. |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--follow/--no-follow`, `-f` | Follow log output in real-time (default: true). |
| `--lines`, `-n` | Number of lines to show from the end of the logs (default: 100). |
| `--verbose`, `-v` | Enable verbose diagnostic output. |

**Expected Output:**

Real-time log output from the bench instance.

```
Viewing bench logs for 'frappe-one'...
Press Ctrl+c to exit

[timestamp] Log output...
```

---

#### `inspect`

Shows detailed information about a specific project.

**Usage:**

```bash
cwcli inspect [PROJECT_NAME]
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The Docker Compose project to inspect. |

**Expected Output:**

A detailed view of the project's configuration and status.

```
frappe-one
├── Bench: bench
│   ├── Site: frappe-one.localhost
│   │   ├── App: frappe
│   │   │   └── Version: 15.0.0
│   │   └── App: erpnext
│   │       └── Version: 15.0.0
│   └── Site: site2.localhost
│       ├── App: frappe
│       │   └── Version: 15.0.0
│       └── App: erpnext
│           └── Version: 15.0.0
└── Bench: bench2
    └── Site: site3.localhost
        ├── App: frappe
        │   └── Version: 15.0.0
        └── App: erpnext
            └── Version: 15.0.0
```

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--verbose`, `-v` | Enable verbose diagnostic output. |
| `--json`, `-j`    | Output the result as a JSON object. |
| `--update`, `-u`  | Update the cache by re-inspecting the project. |
| `--show-apps`, `-a` | Show available apps in the output tree. |
| `--interactive`, `-i` | Prompt to name each bench instance interactively. |

---

#### `open`

Opens a Frappe project instance in VS Code (with Dev Containers) or executes into the container with Docker.

**Usage:**

```bash
cwcli open [PROJECT_NAME]
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The Docker Compose project name to open. |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--path`, `-p` | Path inside the container to open (uses cached bench path from inspect if not specified). |
| `--verbose`, `-v` | Enable verbose diagnostic output. |

**Features:**

- Auto-detects VS Code and VS Code Insiders installations
- Interactive editor selection menu
- Automatically installs required VS Code extensions (Docker and Dev Containers)
- Uses cached bench paths from `inspect` command
- Fallback to Docker exec if VS Code is not available

**Expected Behavior:**

If VS Code is installed, you'll be prompted to choose:
```
How would you like to open this instance?
> VS Code - Open in development container
  Docker - Execute interactive shell in container
```

---

#### `update`

Updates specified Frappe apps and migrates all sites where they are installed.

**Usage:**

```bash
cwcli update [PROJECT_NAME] --app [APP_NAME]...
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The name of the project to update. |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--app`, `-a` | App name(s) to update (required). Specify multiple apps with `--app app1 --app app2` or `--app app1 app2`. |
| `--build`, `-b` | Build assets after updating apps using `bench build`. |
| `--clear-cache`, `-c` | Clear cache for all affected sites after migration. |
| `--clear-website-cache`, `-w` | Clear website cache for all affected sites after migration. |
| `--path`, `-p` | Path to the bench directory inside the container (uses cached path from inspect if not specified). |
| `--verbose`, `-v` | Enable verbose output with streaming command execution. |

**What it does:**

1. Runs `git pull` in each specified app directory
2. Identifies all sites where the updated apps are installed
3. Runs `bench --site <site> migrate` for each affected site
4. (Optional) Runs `bench build --app <app>` for each successfully updated app
5. (Optional) Runs `bench --site <site> clear-cache` for each affected site
6. (Optional) Runs `bench --site <site> clear-website-cache` for each affected site
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

**Expected Output:**

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

**Error Handling:**

The command tracks failures at each step and provides detailed error messages if any operation fails. The command exits with code 1 if any step fails.

---

#### `unlock`

Removes the locks folder for a specified site to unlock it.

**Usage:**

```bash
cwcli unlock [PROJECT_NAME] --site [SITE_NAME]
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The Docker Compose project name. |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--site`, `-s` | Site name to unlock (removes the locks folder). Required. |
| `--path`, `-p` | Path to the bench directory inside the container (uses cached path from inspect if not specified). |
| `--verbose`, `-v` | Enable verbose output and stream rm command output. |

**What it does:**

Removes the `{bench_path}/sites/{site_name}/locks` directory, which can help resolve issues when a site is stuck in a locked state due to incomplete migrations or background jobs.

**Example:**

```bash
# Unlock a site
cwcli unlock my-project --site development.localhost

# Unlock with verbose output to see files being removed
cwcli unlock my-project --site development.localhost -v
```

**Expected Output:**

```
✓ Successfully unlocked site 'development.localhost'
Removed locks folder: /workspace/frappe-bench/sites/development.localhost/locks
```

**When to use:**

- After a migration fails or is interrupted
- When you see "This document is currently locked and queued for execution" errors
- When background jobs don't complete properly

**Note:** The `update` command automatically clears locks after completion, so manual unlocking is typically only needed for interrupted operations.

---

#### `run`

Executes bench commands inside a project's frappe container.

**Usage:**

```bash
cwcli run [PROJECT_NAME] [BENCH_COMMANDS]...
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The Docker Compose project name. |
| `BENCH_COMMANDS` | Bench command and arguments to run. |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--path`, `-p` | Path to the bench directory inside the container (default: /workspace/frappe-bench). |
| `--verbose`, `-v` | Enable verbose output. |

**Example:**

```bash
cwcli run frappe-one migrate
```

---

#### `status`

Checks the health status of a Frappe project instance.

**Usage:**

```bash
cwcli status [PROJECT_NAME]
```

**Arguments:**

| Argument       | Description                               |
|----------------|-------------------------------------------|
| `PROJECT_NAME` | The Docker Compose project name to check. |

**Options:**

| Option      | Description                               |
|-------------|-------------------------------------------|
| `--verbose`, `-v` | Show the health-check command, raw curl output, and explain the reported status. |

**Expected Output:**

- `offline`: Container is not running
- `online`: Container is running but HTTP probe failed
- `running`: Container is running and HTTP probe succeeded

---

#### `config`

Manages the CLI configuration.

**Usage:**

```bash
cwcli config [SUBCOMMAND]
```

**Subcommands:**

*   **`path`**: Displays the path to the configuration file.
*   **`add-path`**: Adds a custom bench search path to the configuration.
*   **`remove-path`**: Removes a custom bench search path from the configuration.
*   **`cache`**: Manages the cache.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
