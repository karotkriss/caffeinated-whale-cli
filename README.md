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
