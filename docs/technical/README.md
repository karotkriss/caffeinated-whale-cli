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
│   ├── commands/               # Command implementations (human-facing frontends)
│   │   ├── start.py           # Thin frontend over core.start (idempotent) + port detection
│   │   ├── status.py          # Thin frontend over core.status (per-process health)
│   │   ├── logs.py            # Reads the shared bench-start log path
│   │   ├── stop.py            # Stop containers
│   │   ├── inspect.py         # Project inspection
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
│   │   ├── list.py             # core.list_instances - the read-only instance listing
│   │   ├── where.py            # core.where - the read-only cached-instance search
│   │   ├── supervision.py      # Shared tracked-state contract for start+status (honcho discovery, marker, bounded log)
│   │   ├── start.py            # core.start - idempotent bench start (discovered-PID no-op)
│   │   ├── status.py           # core.status - per-process health + pre-computed overall
│   │   └── version.py          # core.version - install-method detection + PyPI lookup for self-update
│   └── utils/                  # Utility modules
│       ├── docker_utils.py    # Docker client management
│       ├── port_utils.py      # Port conflict detection
│       ├── db_utils.py        # SQLite cache
│       ├── completion_utils.py # Tab completion
│       ├── toon.py            # Dependency-free TOON encoder (axi stdout)
│       └── ...                # Other utilities
└── tests/                      # Test suite
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

**Module:** `utils/docker_utils.py`

Manages Docker container lifecycle:
- Container discovery via labels
- Status checking
- Start/stop operations
- Error handling

#### 2. Port Conflict Detection

**Module:** `utils/port_utils.py`, `commands/start.py`

Intelligent port conflict detection:
- Detects Frappe project conflicts
- Detects external process conflicts
- Interactive resolution
- Cross-platform process identification

#### 3. Project Inspection & Caching

**Modules:** `commands/inspect.py`, `utils/db_utils.py`

Project structure discovery and caching:
- Finds bench instances
- Discovers sites and apps
- SQLite-based caching
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

**Module:** `utils/vscode_utils.py`

Development container integration:
- Auto-detects VS Code/Insiders
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
- `backup` was the first command migrated onto this pattern (`core/backup.py`), followed by the read-only `ls`/`list` (`core/list.py`) and `where` (`core/where.py`), and `start`+`status` (`core/start.py`, `core/status.py`, sharing the tracked-state contract in `core/supervision.py`); the shared bench-op resolvers were split into `core/resolvers.py`/`core/docker.py` (pure) plus thin CLI wrappers in `commands/utils.py`/`utils/docker_utils.py`

See `openspec/changes/core-logic-foundation/design.md` for the seven locked architecture decisions.

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
from caffeinated_whale_cli.utils.docker_utils import (
    get_project_containers,
    handle_docker_errors,
)

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
