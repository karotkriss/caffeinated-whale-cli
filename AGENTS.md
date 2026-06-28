# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## `init` command: Frappe/bench version gating

`bench new-site` MariaDB flag depends on the Frappe branch (see `_select_mariadb_flag` in `src/caffeinated_whale_cli/commands/init.py`):

- `version-13` and `version-14` -> `--no-mariadb-socket`
- `version-15`+ -> `--mariadb-user-host-login-scope=%` (this flag only exists in bench/Frappe 15+; passing it to bench 14 makes `bench new-site` fail)

Other per-branch settings nearby in `init.py`: Python version (`branch_python`: 15->3.12, 14->3.10, 13->3.9), Node major (`branch_node`: 14->16, 13->14), and `setuptools<82` is pinned only for `version-13`.

### `init` existing-bench flow: decline must continue, not dead-end

The devcontainer image ships a `/workspace/frappe-bench`, so a fresh `cwcli init` usually finds an already-existing bench at the default `bench_parent/bench_name`.
`_resolve_bench_target(frappe_container, bench_parent_path, bench_name)` in `init.py` owns this branch and returns `(bench_name, bench_full_path, bench_exists)`.
When the bench exists it asks "Reuse the existing bench ...?": Yes reuses it (returns `bench_exists=True` so `bench init` is skipped); No now prompts for a different bench name and loops, so site setup continues on a fresh bench (returns `bench_exists=False`).
A blank replacement name or a cancelled prompt (`.ask()` returns `None`) exits cleanly with code 0 and "No changes made.".
This replaced the old behavior where declining reuse just `raise typer.Exit(0)` and aborted the whole command (issue #20).
The resolver runs after the setup spinner has exited, so its prompts own the terminal; keep it out of any `console.status`/`TipSpinner` block.
`inputs.bench_name` is reassigned from the resolver's return (so `bench init` and the site paths use the chosen name), which is valid because `InitInputs` is a plain mutable `@dataclass`.
Regression coverage is in `tests/test_init_reuse_bench.py`.

## CI quality gates

- `.github/workflows/lint.yml` runs `black --check` + `ruff check`.
- `.github/workflows/test.yml` runs `pytest` (with `pytest-cov`) and `mypy src/`. It triggers on pushes to all branches and PRs to all branches (the default branch is `develop`, not `master`).
- The `Pytest` job is the intended required gate. develop has no branch protection, so the admin must tick `Pytest` as a required status check in the develop branch-protection settings for it to actually block merges.
- The `Mypy` job is now a zero-error blocking gate. The historical ~50 errors were burned down to zero and `continue-on-error` was dropped from the step, so any new type error fails the job's status check. To make it *required to merge*, the repo admin still has to tick `Mypy` as a required status check in the develop branch-protection settings (same outstanding admin step as `Pytest`).
- Keep `uv run mypy src/` at zero errors. Two `types-*` stub packages are dev deps for this (`types-requests`, `types-toml`); add the matching `types-*` stub rather than ignoring an untyped third-party import. There are currently no `# type: ignore` comments in `src/` - prefer accurate annotations (e.g. assign a dynamic `json.loads(...)`/`exec_run(...)` result to a typed local, use `str | None` for implicit-Optional defaults) over silencing. Do not loosen `[tool.mypy]` in `pyproject.toml` to make errors disappear.
- `build.yml` / `release.yml` trigger on `master` (the historical default); they build and publish to PyPI and do not run tests.

## `rm` command: what removal actually deletes

`docker-py`'s `Container.remove(v=True)` only removes a container's *anonymous* volumes. The named compose volumes that frappe-docker creates (e.g. `sites`, `db-data`) are NOT touched by it, so they must be removed explicitly or the databases/sites survive a "delete" (this was the issue #19 / "rm lies" bug).

In `src/caffeinated_whale_cli/commands/rm.py`:

- `_remove_named_volumes` enumerates volumes via `get_project_volumes` (label `com.docker.compose.project={name}` in `docker_utils.py`) and calls `volume.remove(force=True)`. It runs only when `remove_volumes` is true (`--volumes`, the default); `--no-volumes` leaves named volumes untouched. When `get_project_volumes` returns `None` (a Docker error, not an empty list) it warns instead of silently reporting 0 removed.
- `_delete_project_directory` deletes the local cwcli instance dir `~/.cwcli/projects/{name}/` (`PROJECTS_DIR / name`). This runs regardless of `--no-volumes` because the directory is config, not data; leaving it behind was the lingering-project half of issue #19.
- Both run after the existing backup/archive safety step inside `_remove_project`, gated on `_archive_project_directory` succeeding. Keep the confirmation text in `rm()` in sync with what is actually deleted.

### Archive only `conf/`, NOT the whole project dir (sharp edge)

`~/.cwcli/projects/{name}/` is the frappe-docker devcontainer's bind mount (mounted as `/workspace`), so it is NOT just config - it is the whole multi-hundred-MB `frappe-bench` (venv, `node_modules`, `sites`). That tree contains dangling symlinks (e.g. `frappe-bench/env/bin/python*` -> the container's python, `sites/assets/frappe`) that do not resolve on the host. `_archive_project_directory` therefore archives ONLY the small reliable `conf/` subdir (the generated `docker-compose.yml`) into `archive_dir/project_files/conf/`, with `copytree(symlinks=True, ignore_dangling_symlinks=True)`. An earlier version copytree'd the whole dir; `copytree` (default `symlinks=False`) follows and raises on the dangling symlinks, the archive-before-delete gate then skipped the volume + dir deletion, and #19 came back in real conditions while the banner still promised deletion. The data safety net is the live `bench backup --with-files` output, which `_backup_sites` writes into the SAME timestamped `archive_dir/backups/`; `conf/` is only the config safety net. `rmtree` (which does not follow symlinks) deletes the full bench fine.

- `_recover_trailing_flags` makes flag order forgiving: a variadic `typer.Argument` greedily eats options that trail it, so `cwcli rm myproj --yes` would otherwise treat `--yes` as a second project name. It pulls `-v/--verbose`, `-y/--yes`, `--no-backup`, `--volumes/--no-volumes` back out of the name list.
- Orphan path: when `get_project_containers` returns an empty list (containers already gone, distinct from a `None` Docker error) but a named volume or the project dir still exists, `rm` still archives `conf/`, removes the volume + dir, and warns that no live DB backup could be taken. This resolves already-orphaned projects (the literal #19 report).

### Recache must never prompt under the spinner (stopped-project deadlock)

The pre-removal recache runs inside a Rich `console.status("Re-caching project '{project}'...")` spinner.
The recache chain is `rm()` -> `cache.recache_project()` -> `commands/inspect.py:inspect()` -> `commands/utils.py:ensure_containers_running(require_running=True)`.
When the frappe container is NOT running, `ensure_containers_running` used to call `questionary.confirm("Would you like to start the containers...?")`.
Issuing an interactive prompt while a `console.status` spinner owns the terminal paints the prompt over and starves it of input -> `cwcli rm <stopped-project>` hangs forever on the spinner.
A stopped project is a normal rm case (a live `bench backup` is impossible, so rm warns and still archives `conf/`, removes named volumes, and deletes the dir), so this must degrade, not block.

The fix has two layers; keep both:

- `ensure_containers_running(..., prompt: bool = True)` (`commands/utils.py`): with `prompt=False` it NEVER prompts - when the containers are not running (and `auto_start` is False) it returns `False` instead of asking. `inspect` forwards this via a hidden `--prompt-start/--no-prompt-start` option (`prompt_to_start`, default True) and, when `ensure_containers_running` returns False, prints a clear error and `raise typer.Exit(1)` (a stopped bench can't be inspected: every probe is an `exec_run`). `cache.recache_project` calls `inspect(..., prompt_to_start=False)`, so the recache is ALWAYS non-interactive and degrades to a `False` return. Standalone `cwcli inspect` keeps `prompt_to_start=True`, so it still prompts interactively as before (this is independent of `--interactive`, which only governs bench-alias naming).
- `_frappe_container_running(project)` (`commands/rm.py`): `rm()` checks this BEFORE entering the recache spinner and SKIPS the recache for a stopped project (printing "Containers for '{project}' are not running; skipping recache"). The recache only refreshes site info for a live backup, which is moot when nothing runs - and rm must NOT auto-start containers it is about to delete. This keeps the spinner off the stopped path entirely; the `ensure_containers_running` layer is defense-in-depth for any other non-interactive caller.

Regression coverage is in `tests/test_rm_stopped.py` (asserts no `questionary.confirm` is reachable from the recache path and that `rm` skips recache for a stopped project).
