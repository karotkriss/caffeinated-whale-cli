# Technical Documentation

This directory contains technical documentation, API references, and deep-dives into the caffeinated-whale-cli architecture.

## Current Documentation

| Document | Purpose | Topics Covered |
|----------|---------|----------------|
| **[Bench Management](./bench.md)** | Bench system deep-dive | Bench instances, sites, apps, cache system |
| **[Frappe Backup & Restore](./frappe-backup-restore.md)** | Frappe's `bench backup`/`bench restore` mechanics | Backup file naming, restore process, what cwcli provides today |
| **[iroh-blobs and sendme](./iroh-blobs-and-sendme.md)** | P2P transfer protocol background | Iroh blobs, BLAKE3 verification, the sendme protocol |
| **[sendme CLI Reference](./sendme-doc.md)** | The underlying `sendme` binary | `sendme send`/`sendme receive` flags and output |

## Overview

Caffeinated Whale CLI is a command-line tool for managing Frappe/ERPNext Docker instances during local development.

### Architecture

```
caffeinated-whale-cli/
├── src/caffeinated_whale_cli/
│   ├── main.py                 # CLI entry point
│   ├── update_notice.py        # Passive stderr-only "update available" notice, over core.version
│   ├── commands/               # Command implementations (human-facing frontends)
│   │   ├── start.py           # Thin frontend over core.start (idempotent) + port detection
│   │   ├── status.py          # Thin frontend over core.status (per-process health)
│   │   ├── restart.py         # Whole-stack restart, or one program via core.restart_process
│   │   ├── logs.py            # Renderer over core.logs_plan; performs the `docker exec -it ... tail` itself
│   │   ├── stop.py            # Stop containers
│   │   ├── inspect.py         # Renderer over core.inspect (tree/JSON, the -i labeling loop)
│   │   ├── update.py          # App updates + migrations
│   │   ├── backup.py          # Thin frontend over core.backup
│   │   ├── list.py            # Thin frontend over core.list_instances (`cwcli ls`)
│   │   ├── where.py           # Thin frontend over core.where
│   │   ├── axi.py             # Agent-facing `cwcli axi` frontend
│   │   ├── self_update.py     # Install-method-aware `cwcli self-update`, over core.version
│   │   └── ...                # Other commands
│   ├── core/                   # UI-pure logic core (no rich/questionary/typer)
│   │   ├── envelope.py         # Result/Status/Message/Choice DTOs
│   │   ├── errors.py           # CwcliError + ErrorKind
│   │   ├── resolvers.py        # Pure container-state/bench resolvers
│   │   ├── docker.py           # Core frappe-container accessor
│   │   ├── backup.py           # core.backup - the reference migrated command
│   │   ├── unlock.py           # core.unlock - backup's near-twin, zero new primitives
│   │   ├── stop.py             # core.stop - stop containers
│   │   ├── label.py            # core.list_benches/set_label/clear_label - bench-label read/set/clear
│   │   ├── list.py             # core.list_instances - the read-only instance listing
│   │   ├── where.py            # core.where - the read-only cached-instance search
│   │   ├── exec_stream.py      # core.exec_stream - the ONE way to exec-and-stream (typed ExecChunk/ExecDone events)
│   │   ├── run.py              # core.run_plan/run_stream - the default run path's exec-stream reference slice
│   │   ├── apps.py             # core.list_apps/install_apps/uninstall_apps - per-bench and multi-site app management
│   │   ├── update.py           # core.update - the app-update state machine (fan-out, maintenance mode, migrations)
│   │   ├── supervision.py      # Shared tracked-state contract plus the Console's one-exec health probe
│   │   ├── start.py            # core.start - idempotent bench start under supervisord (discovered-PID no-op, --autorestart, blocks on web-readiness before reporting running)
│   │   ├── status.py           # core.status - per-process health + pre-computed overall
│   │   ├── restart.py          # core.restart_process - restart ONE supervised program, siblings untouched
│   │   ├── logs.py             # core.logs_plan/read_logs - resolves container/bench/program selection; the interactive tail stays in the frontend, the bounded axi read runs in the core
│   │   ├── inspect.py          # core.inspect/inspect_raw - the T1/T2/T3 freshness tiers AND the cache write, one contract
│   │   ├── open.py             # core.open_plan - resolves container/bench/app/editor for `cwcli open`; the handover stays in the frontend
│   │   ├── init.py             # core.init_instance/init_bench - the two-call provisioning slice; the prompts/spinners/secret UX stay in the frontend
│   │   ├── config.py           # core.config - settings/search-paths/cache-inventory decisions (validation, clear-cache consent)
│   │   ├── auto_inspect.py     # core.auto_inspect - fused desired-state enable/disable/stop/status/log_tail over the daemon+boot-hook utils
│   │   └── version.py          # core.version - install-method detection + PyPI lookup (self-update, passive notice) + build_info for --version's build tag
│   └── utils/                  # Utility modules
│       ├── docker_utils.py    # Docker client management
│       ├── port_utils.py      # Port conflict detection
│       ├── db_utils.py        # SQLite cache
│       ├── completion_utils.py # Tab completion
│       ├── toon.py            # Dependency-free TOON encoder (axi stdout)
│       ├── agent_hooks.py     # `cwcli axi setup` SessionStart-hook installer (Claude Code/Codex/OpenCode)
│       └── ...                # Other utilities
└── tests/                      # Test suite

# Repo-only; AGENTS.md's Packaging convention owns the artifact contract:
# scripts/build_skill.py        # Generates skills/cwcli/SKILL.md from the live `cwcli axi` registry
# skills/cwcli/SKILL.md         # The installable Agent Skill (`npx skills add ... --skill cwcli`)
```

### Technology Stack

- **Python 3.10+** - Runtime
- **Typer** - CLI framework
- **Rich** - Terminal formatting
- **Questionary** - Interactive prompts
- **Docker SDK for Python** - Container management
- **Peewee ORM** - SQLite caching
- **pytest** - Testing framework

### Core Systems

#### 1. Docker Container Management

**Modules:** `core/docker.py`, `utils/docker_utils.py`

Manages Docker container lifecycle:
- Container discovery via labels (`core/docker.py`, UI-pure so the logic core can call it directly)
- Status checking
- Start/stop operations
- Error handling (`utils/docker_utils.py`'s CLI-only `handle_docker_errors`)

#### 2. Port Conflict Detection

**Module:** `utils/port_utils.py`, `commands/start.py`

Intelligent port conflict detection:
- Detects Frappe project conflicts
- Detects external process conflicts
- Interactive resolution
- Cross-platform process identification

#### 3. Project Inspection & Caching

**Modules:** `core/inspect.py`, `commands/inspect.py`, `utils/db_utils.py`

Project structure discovery and caching, on the UI-pure core (`core/inspect.py`); `commands/inspect.py` is a renderer over it:
- Finds bench instances
- Discovers sites and apps
- SQLite-based caching - the cache write lives in the core, not the renderer
- Read-only freshness pass on cached reads, escalating to a full re-inspect on drift
- Cache invalidation

See [Bench Management](./bench.md) for detailed documentation.

#### 4. Tab Completion

**Module:** `utils/completion_utils.py`

Context-aware tab completion:
- Project names from Docker
- Apps/sites from cache
- 2-second TTL caching
- Cross-shell support

See [Testing Guide](../testing/guide.md) for test coverage details.

#### 5. VS Code Integration

**Modules:** `core/open.py`, `utils/vscode_utils.py`

Development container integration:
- Auto-detects VS Code/Insiders/Cursor (`core/open.py`, stdlib `shutil.which`, run once)
- Extension installation
- Container attachment
- Fallback to docker exec

#### 6. UI-Pure Logic Core + `cwcli axi`

**Modules:** `core/`, `commands/axi.py`, `utils/toon.py`

Business logic and I/O live in `core/`, which imports no `rich`/`questionary`/`typer` and never prompts or calls `typer.Exit` (enforced by an AST-scan unit test):
- A core function returns the typed envelope `Result[T]` (`status` OK/WARNING/NEEDS_CHOICE, `data`, `warnings`, `choice`) or raises a typed `CwcliError`
- A decision the core can't make from its params comes back as `NEEDS_CHOICE`, never a prompt; each frontend resolves it its own way
- No live Docker object crosses a `core.<verb>` return boundary - DTOs carry only serializable data
- `cwcli axi` is a thin agent-facing frontend over the same core: it never prompts, emits [TOON](https://toonformat.dev) on stdout via the dependency-free `utils/toon.py` encoder, and maps outcomes to exit codes 0/1/2
- `backup` was the first command migrated onto this pattern (`core/backup.py`), followed by `unlock` (`core/unlock.py`), `stop` (`core/stop.py`), `label` (`core/label.py`), `run` (`core/run.py`; its default path shares the `core/exec_stream.py` contract, while `commands/run.py` owns the dedicated `--interactive` Docker CLI passthrough), the read-only `ls`/`list` (`core/list.py`) and `where` (`core/where.py`), `start`+`status`+`restart` (`core/start.py`, `core/status.py`, `core/restart.py`, sharing the tracked-state contract in `core/supervision.py`, which runs the bench under supervisord), `logs` (`core/logs.py`), `apps`+`update` (`core/apps.py`, `core/update.py`), `inspect` (`core/inspect.py` - the T1/T2/T3 freshness tiers AND the cache write), `open` (`core/open.py` - `open_plan -> Result[LaunchTarget]`, the declarative plan carrying the editor target and optional resolved host URL; the frontend performs the handover, and there is deliberately no `axi open` verb because `execvp` destroys the process that owes `axi` its TOON document), `init` (`core/init.py` - TWO sequential plain functions, `init_instance` then `init_bench`, the seam sitting at init's one mid-flow user decision; the `bench new-site` secrets ride `exec_stream(environment=)` byte-exactly, and `cwcli axi init` is a thin TOON frontend over the same two calls, non-interactive choice surfaces becoming usage/operational errors), `config` (`core/config.py` + `core/auto_inspect.py` - the settings/search-path/cache decisions and the fused auto-inspect desired-state verbs; `commands/config.py` is a renderer, its frozen deprecated aliases deliberately bypass the core, and `cwcli axi config` is the one read-only agent verb), `restore` (`core/restore.py` - a plan/apply split `restore_plan`/`receive_plan -> restore_apply`, a read/destroy safety separation for cwcli's most destructive path; the destructive execs stay buffered `exec_run` with secrets off the argv via `environment=`, the streamed copies move to the core, the sendme subprocess + confirms + menu stay frontend, and there is deliberately no `axi restore` verb - deferred, asserted by test), `rm` (`core/rm.py` - `core.remove -> Result[RemovalOutcome]`, ONE plain function carrying the verified copy-out backup gate and the container, volume, network, and directory deletion; `commands/rm.py` is a renderer, the stopped-project transient-start orchestration stays frontend because it prompts, plan/apply was DECLINED on rm's real behavior, and `cwcli axi rm` is a thin TOON frontend over the same core function, with `--yes` granting consent only - never an auto-start - and no `--no-backup` bypass), and `rm-site` (`core/rm_site.py` - `drop_site -> Result[DropSiteOutcome]`, shared by the human and `axi` frontends, with explicit site targeting, core-owned consent, and fail-closed archive relocation). The shared bench-op resolvers were split into `core/resolvers.py`/`core/docker.py` (pure) plus thin CLI wrappers in `commands/utils.py`/`utils/docker_utils.py`

See `openspec/changes/core-logic-foundation/design.md` for the seven locked architecture decisions.

#### 7. AXI Cross-Cutting Shell

**Modules:** `commands/axi.py` (`axi setup`), `utils/agent_hooks.py`, `scripts/build_skill.py`, `skills/cwcli/SKILL.md`

The two complementary ways an agent learns the `cwcli axi` surface exists:
- `cwcli axi setup` installs a SessionStart hook into every detected agent harness (Claude Code, Codex, OpenCode) via `utils/agent_hooks.py`; it is ambient and carries live instance state but needs harness support and costs tokens every session
- `skills/cwcli/SKILL.md` is an installable Agent Skill, generated by `scripts/build_skill.py` from the live `cwcli axi` Typer registry; it loads on demand in any agent and costs nothing, but is static
- `scripts/build_skill.py --check` runs as a unit test (`tests/test_axi_skill.py`), so the existing `Pytest` CI job blocks a stale skill without a dedicated workflow step
- `scripts/` and `skills/` are repo-only; see the Packaging convention in `AGENTS.md` for the built-artifact contract
- The `.claude/skills/` deep-dives (internal source-debugging notes, not part of the shipped `skills/cwcli` skill) carry `metadata: internal: true` in their frontmatter, because `skills add karotkriss/caffeinated-whale-cli` scans `.claude/skills/` too and would otherwise publish all six to every user on a bare install; `tests/test_axi_skill.py` enforces the marker on every internal skill and its absence on the public one

### Command Architecture

Each command follows this pattern:

```python
import typer
from ..utils.docker_utils import handle_docker_errors

@handle_docker_errors
def command_name(
    project_name: str = typer.Argument(...),
    option: bool = typer.Option(False),
):
    """Command description."""
    # 1. Validate inputs
    # 2. Get Docker containers
    # 3. Perform operations
    # 4. Handle errors
    # 5. Display results
```

### Error Handling

**Decorator Pattern:**

```python
@handle_docker_errors
def my_command():
    # Automatically handles:
    # - Docker not installed
    # - Docker daemon not running
    # - Connection errors
    pass
```

### Cache System

**Location:** `~/.cwcli/cache/cwc-cache.db` (or `$CWCLI_HOME/cache/cwc-cache.db` when the `CWCLI_HOME` override is set - see `config_utils.cwcli_home()`)

**Schema:**
```sql
Project
  └── Bench
      ├── AvailableApp
      └── Site
          └── InstalledAppDetail
```

**Operations:**
- `db_utils.cache_project_data()` - Store
- `db_utils.get_cached_project_data()` - Retrieve
- `db_utils.clear_cache_for_project()` - Invalidate

### Configuration

**Location:** `~/.cwcli/config/` (or `$CWCLI_HOME/config/` when the `CWCLI_HOME` override is set)

**Customization:**
- Custom bench search paths
- Project-specific settings

### Performance Considerations

1. **Docker Queries** - Use sparse=True for faster queries
2. **Caching** - 2-second TTL for completion, persistent for project data
3. **Process Detection** - Platform-specific optimizations

### Security

1. **Docker Socket** - Requires Docker daemon access
2. **No Credentials** - CLI doesn't store credentials
3. **Local Only** - Designed for local development

## API Reference

### Docker Utilities

```python
from caffeinated_whale_cli.core.docker import get_project_containers
from caffeinated_whale_cli.utils.docker_utils import handle_docker_errors

# Get containers for a project
containers = get_project_containers("frappe-one")

# Use error handling decorator
@handle_docker_errors
def my_function():
    pass
```

### Database Utilities

```python
from caffeinated_whale_cli.utils.db_utils import (
    cache_project_data,
    get_cached_project_data,
    clear_cache_for_project,
)

# Cache project data
cache_project_data(project_name, bench_instances_data)

# Retrieve cached data
cached_data = get_cached_project_data(project_name)

# Clear cache
clear_cache_for_project(project_name)
```

### Completion Utilities

```python
from caffeinated_whale_cli.utils.completion_utils import (
    complete_project_names,
    complete_app_names,
    complete_site_names,
)

# Get project names (from Docker)
projects = complete_project_names()

# Get app names (from cache, requires context)
apps = complete_app_names(typer_context)
```

## Design Patterns

### 1. Decorator Pattern

Used for:
- Error handling (`@handle_docker_errors`)
- Caching (internal)

### 2. Factory Pattern

Used for:
- Docker client creation
- Database connections

### 3. Repository Pattern

Used for:
- Cache operations (db_utils)

### 4. Command Pattern

Used for:
- CLI commands (Typer structure)

## Development Guidelines

### Adding a New Command

1. Create file in `src/caffeinated_whale_cli/commands/`
2. Implement command function with Typer decorators
3. Add `@handle_docker_errors` decorator
4. Register in `main.py`
5. Add tests
6. Update documentation

Example:
```python
# commands/mycommand.py
import typer
from ..utils.docker_utils import handle_docker_errors

@handle_docker_errors
def mycommand(
    project_name: str = typer.Argument(...),
):
    """Description of command."""
    # Implementation
    pass

# main.py
from .commands.mycommand import mycommand
app.command("mycommand")(mycommand)
```

### Adding a New Utility

1. Create module in `src/caffeinated_whale_cli/utils/`
2. Follow existing patterns
3. Add comprehensive docstrings
4. Write tests (80%+ coverage)
5. Update technical docs

## Testing

See [Testing Documentation](../testing/) for comprehensive testing guides.

**Quick reference:**
- **Unit tests** - Test individual functions
- **Integration tests** - Test command workflows
- **Mocking** - Mock Docker, filesystem, external APIs

## Future Architecture Plans

Verified as still unshipped as of 0.34.0 (2026-07-08); re-check this list for staleness the next time this doc is substantially edited.

### Planned Enhancements

1. **Plugin System** - Allow custom commands
2. **Configuration API** - Programmatic configuration
3. **Async Operations** - Parallel container operations
4. **Webhook Support** - Event notifications
5. **API Mode** - Run as HTTP API

### Scalability

Current design supports:
- Multiple projects simultaneously
- Large bench instances
- Many containers
- Cross-platform operation

## Debugging

### Enable Verbose Mode

Most commands support `-v` or `--verbose`:

```bash
cwcli start frappe-one -v
cwcli inspect frappe-one -v
```

### Check Cache

```bash
# View cache database
sqlite3 ~/.cwcli/cache/cwc-cache.db

# List cached projects
.tables
SELECT * FROM project;
```

### Docker Issues

```bash
# Check Docker is running
docker ps

# Check Docker version
docker --version

# Test Docker SDK
python -c "import docker; docker.from_env().ping()"
```

## Performance Metrics

### Typical Operation Times

| Operation | Time | Notes |
|-----------|------|-------|
| `ls` | <1s | Fast label queries |
| `start` | 2-5s | Container startup time |
| `inspect` (first) | 3-8s | Docker exec queries |
| `inspect` (cached) | <1s | SQLite lookup + read-only freshness pass (`--no-refresh` for a pure lookup) |
| Tab completion | <200ms | 2s TTL cache |

### Optimization Opportunities

Also unverified/unshipped as of 0.34.0 (2026-07-08).

1. **Parallel container operations** - Start/stop multiple at once
2. **Batch Docker queries** - Reduce API calls
3. **Incremental cache updates** - Don't re-inspect everything

## Contributing

See [Contributing Guide](../contributing/) for:
- Code style guidelines
- Commit message format
- Testing requirements
- Review process

## Resources

- [Bench Management](./bench.md) - Detailed bench system docs
- [Testing Guide](../testing/guide.md) - Testing documentation
- [API Documentation](https://docs.python.org/3/) - Python standard library
- [Typer Documentation](https://typer.tiangolo.com/) - CLI framework
- [Docker SDK Documentation](https://docker-py.readthedocs.io/) - Docker Python SDK

## Questions?

- **Issues:** Report on [GitHub Issues](https://github.com/karotkriss/caffeinated-whale-cli/issues)
- **Discussions:** Start a discussion on GitHub
- **Documentation:** Check other guides in `docs/`
