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

## `inspect` command: 3-tier freshness model (cache / partial / full)

`inspect` is cache-backed and used to be cache-first with no staleness check, so an app installed after the cache was written (it lands in the bench `apps/` dir immediately, but the cache still held the old list) stayed invisible until `inspect --update` rebuilt the cache.
`open --app <name>` read the same stale cache and errored "App not found".
This was issue #27 ("need to inspect -uv on new projects to see installed apps"; `-uv` = `--update --verbose`, only `--update` matters).

The fix (`commands/inspect.py`) is a 3-tier model, chosen so `inspect` stays fast while serving fresh data:

- **T1 - cache return (unchanged, instant):** no container calls. Used with the new `--no-refresh` flag, or automatically when the containers are not running (the running check is `ensure_containers_running(..., prompt=False)`, which never prompts or starts - non-disruptive).
- **T2 - partial inspect (`partial_inspect_known_benches`):** the default for a cached-and-running project, and a pure READ-ONLY drift detector that NEVER writes the cache. For each KNOWN bench path already in the cache it cheaply re-reads only `ls apps` (available_apps) and `ls sites`, plus a `test -d` bench check. It deliberately SKIPS `_find_bench_instances` (the `find`-based instance discovery), the deep per-site `bench list-apps` (which boots Frappe), AND any config re-read: the cached per-site `installed_apps`, the cached per-site `site_config`, and the cached `common_site_config` are all carried forward from the cached structures (so a transient unreadable/half-written config can never silently drop `common_site_config`/`default_site`). It returns `(refreshed, drift)`. A freshly installed app shows up in `apps/`, so the cheap `ls` catches it.
- **T3 - full inspect (unchanged):** the existing `--update` / cache-miss path: `find` discovery + per-site `bench list-apps` + re-cache.

Escalate-on-drift: when T2 sees the on-disk available-apps/site set diverge from the cache (or a known bench vanished) it returns `drift=True` and `inspect` sets `bench_instances_data = None` to fall through to a full T3 inspect, so the deep per-site lists and any brand-new bench are refreshed AND persisted. No drift -> `inspect` serves the cached data UNCHANGED and does NOT write the cache (the former write-on-read that bumped `last_updated` on every cache hit is gone). T2 is wrapped so any failure degrades to serving the cached data (never worse than the old behavior).
Guarded escalation: the drift-triggered full inspect is NOT allowed to hard-fail a previously-working read. Before falling through, `inspect` remembers the cached benches in `drift_fallback_benches`; if the full inspect's `_find_bench_instances` then discovers NO bench (e.g. a custom search path was removed via `config remove-path`, or the bench was never under the default search roots), `inspect` degrades to that cached data and serves it WITHOUT persisting, instead of `raise typer.Exit(1)`. The hard "No Bench Instances found" error is preserved ONLY for the `--update` / cache-miss paths, which have no valid cache to fall back on (`drift_fallback_benches is None`). To keep the raise out of the spinner, the no-benches case is captured as a flag inside the `TipSpinner` and branched on after the `with` block, and `cache_project_data` is called only when benches were actually gathered.
`open --app` reuses this via `partial_inspect_known_benches(frappe_container, cached_data["bench_instances"], verbose=verbose)` IN-MEMORY only - it does NOT persist (degrading to the cached available-apps on any error). It matches the refreshed entry to the chosen bench BY PATH (`b["path"] == bench_instance["path"]`), not by indexing `refreshed[0]`, because the partial pass drops any vanished bench and so can be index-shifted relative to the cached list; on no match or an empty result it keeps the cached available-apps. Leaving the cache untouched is deliberate: the next plain `cwcli inspect` still self-heals via escalate-on-drift (which also refreshes the deep per-site installed lists), instead of `open` consuming the drift signal by writing fresh `available_apps` while carrying stale per-site `installed_apps` forward.

A sharp edge: T2 cannot cheaply refresh per-site `installed_apps` (that needs the deep `bench list-apps`); it relies on escalate-on-drift to bring those up to date when `apps/` or the site set changes. A pure `install-app` of an app already present in `apps/` (no new dir) would not trip drift, but in practice a newly installed app appears in `apps/` first, which is exactly the reported case.

Calling `inspect` directly in tests is a trap: it is a Typer command, so any omitted parameter keeps its `typer.Option(...)` default OBJECT (truthy) - e.g. an unspecified `update` silently forces a full inspect, and an unspecified `no_refresh` silently takes the T1 serve-cache-verbatim branch. Real callers (`recache_project`, `auto_inspect`, `start`, `update`, `restore`, `open`) pass ALL params explicitly, including `no_refresh=False`; tests must too. Regression coverage is in `tests/test_inspect_partial_refresh.py` (uses a `FakeFrappeContainer` that records every `exec_run` to assert which tier ran, and asserts T2 no-drift performs no cache write).

## CI quality gates

- `.github/workflows/lint.yml` runs `black --check` + `ruff check`.
- `.github/workflows/test.yml` runs `pytest` (with `pytest-cov`) and `mypy src/`. It triggers on pushes to all branches and PRs to all branches (the default branch is `develop`, not `master`).
- The `Pytest` job is the intended required gate. develop has no branch protection, so the admin must tick `Pytest` as a required status check in the develop branch-protection settings for it to actually block merges.
- The `Mypy` job is now a zero-error blocking gate. The historical ~50 errors were burned down to zero and `continue-on-error` was dropped from the step, so any new type error fails the job's status check. To make it *required to merge*, the repo admin still has to tick `Mypy` as a required status check in the develop branch-protection settings (same outstanding admin step as `Pytest`).
- Keep `uv run mypy src/` at zero errors. Two `types-*` stub packages are dev deps for this (`types-requests`, `types-toml`); add the matching `types-*` stub rather than ignoring an untyped third-party import. There are currently no `# type: ignore` comments in `src/` - prefer accurate annotations (e.g. assign a dynamic `json.loads(...)`/`exec_run(...)` result to a typed local, use `str | None` for implicit-Optional defaults) over silencing. Do not loosen `[tool.mypy]` in `pyproject.toml` to make errors disappear.
- `build.yml` triggers on pushes to `master` (the historical default) and on `workflow_dispatch`; it builds and uploads artifacts, no tests.
- `release.yml` triggers on pushes to `master`, on `v*.*.*` tag pushes, on published releases, and on `workflow_dispatch`; it builds and publishes to PyPI and does not run tests (see "Cutting a release" below).

## Cutting a release

The release convention, as practiced for 0.31.1 (PR #22) and 0.32.0:

- A version bump touches exactly four files: `version` in `pyproject.toml`, `__version__` in `src/caffeinated_whale_cli/__init__.py`, the project's own entry in `uv.lock` (regenerate via `uv lock`, do not hand-edit), and a new `CHANGELOG.md` section.
- Both `build.yml` and `release.yml` hard-fail when `pyproject.toml` and `__init__.py` disagree, so the two version strings must always be bumped together.
- `CHANGELOG.md` is manually maintained in Keep a Changelog format: a `## [x.y.z] - YYYY-MM-DD` section with `### Added`/`### Changed`/`### Fixed` subsections, entries shaped like ``- **`cmd` command** - description`` with indented sub-bullets, user-facing changes only (no CI/typing internals), no issue numbers.
- Version choice follows semver as this repo practices it: minor for new flags or behavior changes (0.30.0, 0.31.0, 0.32.0), patch for a narrow compatibility fix (0.31.1).
- The bump lands as a normal PR to `develop` with a `chore: bump version to x.y.z` commit.
- Publishing to PyPI is done by `release.yml`, not from a dev machine.
  Since the default branch is `develop` but the workflow's branch trigger is `master`, the working path is: merge the bump PR into `develop`, then push a `vX.Y.Z` tag pointing at the merge commit (the tag trigger fires regardless of branch).
  The workflow verifies the tag matches the package version, publishes to PyPI, and creates a GitHub release with the built artifacts.

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

### Data-safety gates: backup gate, name validation, honest exit codes

Three fail-open data-safety gaps were closed in `commands/rm.py` (audit findings C1/H5/M11). Regression coverage is in `tests/test_rm_safety.py`, which drives the previously-untested backup path with a `FakeFrappeContainer` (records every `exec_run`); every pre-existing rm test passed `no_backup=True`, so the backup gate had zero coverage.

- **C1 - a failed/unverified backup must NOT allow volume/DB deletion.** `_backup_sites`' return value is now captured into `result["backup_ok"]` and gates volume+directory removal, mirroring the existing `_archive_project_directory` (`conf/`) gate. Critically, `_backup_sites`' success signal was made trustworthy: a zero `bench backup` exit only means the dump was written INSIDE the container (the very volume about to be deleted), so each artifact is copied out via `cat` and VERIFIED on the host - the database dump (`_DB_DUMP_MARKER = "database.sql"`, matched case-insensitively) must be present and non-empty, and ANY copy-out `cat` failure fails the site closed (a backup missing a part is not trusted). Non-DB artifacts (e.g. a files tar) may legitimately be small, so only the DB dump is size-checked. `_backup_sites` now returns True only if EVERY site fully backed up (no more `backed_up_count > 0` partial-success). The copy mechanism was kept as `cat` (not swapped to `get_archive`) because the corruption/OOM concern is "needs live repro" and the load-bearing fix is host-artifact verification. `--no-backup` opts out of the whole gate (no backup attempted, `backup_ok` stays True) - it is the documented escape hatch to force removal without a backup.
- **H5 - project-name validation before any `rmtree`/volume op.** `_is_valid_project_name` (rejects empty/`.`/`..`/absolute/separator/NUL names) and `_is_safe_project_dir` (asserts `(PROJECTS_DIR / name).resolve()` is strictly inside `PROJECTS_DIR.resolve()`, which also catches a symlink-escape) guard three layers: the CLI filters names up front (mirroring the existing empty-name filter), `_remove_project` re-checks at entry (defense-in-depth for any internal caller), and `_delete_project_directory`/`_archive_project_directory` hard-guard right before the `rmtree`/copytree. Without this, `cwcli rm ..` resolved `PROJECTS_DIR / ".."` to `~/.cwcli` and `rmtree`'d every project + cache + config.
- **M11 - honest exit codes on partial failure.** `_remove_project`'s result dict gained `failures: list[str]`; volume-removal, dir-removal, container-removal, backup, invalid-name, and Docker-connection failures all append to it. `_remove_named_volumes` and `_delete_project_directory` take an optional `failures` collector (their int/bool returns are unchanged, so the existing `test_rm_truth.py` unit tests stay green). `rm()` raises `typer.Exit(1)` whenever any project reported failures (or a name was rejected) and no longer prints the green `✓ Successfully removed` on a partial failure. Two sharp edges fixed: `container_name` is bound to `"<unknown>"` BEFORE reading `container.name` (a docker-py property that can itself raise, which previously left `container_name` unbound → NameError in the except handler); and a caught container-removal error now sets `container_removal_failed`, which is part of the same gate as backup/archive, so it does NOT fall through to destroy the volumes/dir.

`_remove_project`'s destructive gate is a single combined check (`container_removal_failed or archive_failed or backup_failed`) placed AFTER container removal, exactly where the `conf/` archive gate already sat - it protects the irreversible data (named volumes + local dir); removing containers first is intentional and non-destructive (containers are recreatable, the named volumes hold the data). When calling `_remove_project`/`rm()` directly in tests, note it is `@handle_docker_errors`-decorated (patch `docker_utils.shutil.which` + `docker.from_env`) and, like other Typer commands here, real callers pass every param explicitly.
