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

Calling `inspect` directly in tests is a trap: it is a Typer command, so any omitted parameter keeps its `typer.Option(...)` default OBJECT (truthy) - e.g. an unspecified `update` silently forces a full inspect, and an unspecified `no_refresh` silently takes the T1 serve-cache-verbatim branch. Real callers (`recache_project`, `auto_inspect`, `start`, `update`, `restore`, `open`) pass ALL params explicitly, including `no_refresh=False` and `yes=False` (the `--yes` auto-start flag added with the bench-UX work); tests must too. Regression coverage is in `tests/test_inspect_partial_refresh.py` (uses a `FakeFrappeContainer` that records every `exec_run` to assert which tier ran, and asserts T2 no-drift performs no cache write).

## Multi-bench UX: labels, `--bench` selector, marker-file recovery

A cwcli "instance" is one docker-compose project; its frappe container can hold more than one bench directory (a multi-bench instance). The addressing model lives in `utils/bench_labels.py` and the shared resolver `commands/utils.py:resolve_bench_path`.

### Label model
- **Numeric label = position.** `_find_bench_instances` now returns `sorted(set(...))` (was `list(set(...))`, which was non-deterministic). A bench's numeric label is its index into that stable sorted-by-path order (0, 1, 2, ...). Positions can shift when a bench is added/removed, so numeric labels are NOT durable - a user label is.
- **User label = durable handle.** Optional per-bench string. `--bench <selector>` resolves a user label FIRST, then a numeric index (`bench_labels.resolve_bench`). The two namespaces can't overlap because a user label may NOT be purely numeric (`validate_user_label` rejects `^\d+$`, enforces charset `[A-Za-z0-9._-]`, max 64). Duplicate labels within a project are rejected by the caller (`label` command / `inspect -i`), which has the sibling benches to check against.
- `get_cached_project_data` returns benches ordered by `Bench.id` (== insertion == sorted discovery order), so `bench_instances[i]` is always numeric label `i`. Do not reorder it.

### Storage + marker-file recovery (the load-bearing contract)
- The user label is persisted in BOTH the SQLite cache AND a per-bench marker file `<bench-root>/.cwcli/.bench-label` (JSON `{"schema": 1, "label": "..."}`, room for future fields). The marker lives INSIDE the container/bench, so it survives a cache wipe. This is the recovery mechanism: if the SQLite DB is lost, a **full inspect** (`--update` / cache-miss) rebuilds each label from its marker (`_gather_bench_data` calls `bench_labels.read_label_marker`) and re-derives the rest of the bench config live, then re-persists. The marker is the source of truth for labels; T1/T2 serve the DB label (kept in sync by every writer), and `partial_inspect_known_benches` carries the cached label forward (it does NOT re-read the marker).
- **DB schema:** `Bench.label` is a nullable column added AFTER the initial schema. `create_tables(safe=True)` never adds a column to an existing table, so `initialize_database()` runs `_migrate_bench_label_column()` (a `PRAGMA table_info(bench)` check + `ALTER TABLE bench ADD COLUMN label VARCHAR`). This is idempotent and keeps old (`label`-less) caches working - existing rows get NULL. `cache_project_data` stores `bench_data.get("label") or None`; `set_bench_label(project, path, label)` updates one bench by path without rewriting the whole project.
- **Marker I/O is container-side.** `write_label_marker`/`read_label_marker`/`clear_label_marker` go through `container.exec_run` with LIST-form commands (`["cat", ...]`, `["rm","-f", ...]`, `["sh","-c", script]`). The write base64-encodes the JSON on the host and `base64 -d`s it in the container, so no label content is ever interpolated into a shell command. Reads fail SAFE (missing/garbage marker -> `None`, never an error). Tests use `tests/bench_fakes.py:MarkerFakeContainer`, which really base64-decodes the write script into an in-memory fs.

### `--bench` selector and the shared resolver
- `resolve_bench_path(project, bench_selector, path_override, *, on_ambiguous="error")` is the single replacement for the old copy-pasted "use `--path` if given, else `bench_instances[0]`, else `/workspace/frappe-bench`" logic (which silently guessed in multi-bench projects). Precedence: `--path` wins (escape hatch; `--bench`+`--path` together is an error); else `--bench` resolves against the cache; else the DEFAULT bench: single-bench -> that bench; multi-bench -> **error listing the benches** (`on_ambiguous="error"`, the data-op default) OR first-bench-plus-note (`on_ambiguous="first"`); no cache at all -> returns `None` so the caller keeps its inspect/default fallback.
- Commands with `--bench`: `run`, `backup`, `update`, `open`, `unlock`, `restore` (all use `on_ambiguous="error"` - a multi-bench op with no selector errors instead of guessing), and `start` (uses `on_ambiguous="first"` per captain decision: `cwcli start` KEEPS WORKING on multi-bench by starting `bench start` in the first sorted bench and printing a note; `--bench` picks another). `run`'s `--path` default changed from a hardcoded `/workspace/frappe-bench` to `None` so the resolver runs. `start` recovers `-y/--yes`, `-v`, and `--bench <val>` from its variadic project-name argument (the arg greedily eats trailing options), same trick as `rm`'s `_recover_trailing_flags`.
- **Setting labels:** `cwcli label <project> [<selector> [<new-label>]] [--clear]` (`commands/label.py`). Bare `cwcli label <project>` lists benches (read-only, no container). Setting/clearing writes BOTH stores and REQUIRES a running frappe container (the marker lives inside the bench; it does NOT auto-start - a label change should not spin up a stopped project). BOTH the set and the clear paths write the MARKER FIRST and only touch the DB on marker success: because the marker is the source of truth for recovery, clearing the DB while the marker survives would let a later full inspect resurrect the just-cleared label, and writing the DB while the marker write failed would drop the durable handle on the next cache wipe. A marker failure prints an error and exits non-zero, leaving the DB unchanged. `inspect -i` was rebuilt on the same model: it prompts a validated user label per bench, writes the marker + persists the DB (was the old ephemeral `bench["alias"]`, which was set in memory and then dropped by `cache_project_data` - it never actually persisted).

### `--yes`/`-y` contract
- Shared `commands/utils.py:confirm_or_exit(prompt, *, assume_yes, refuse_message)` mirrors the restore/rm pattern: `--yes` proceeds; a non-TTY WITHOUT `--yes` on a destructive op refuses and exits non-zero; an interactive decline exits non-zero.
- `commands/utils.py:ensure_containers_running` mirrors that same contract on its interactive not-running branch: on a **non-TTY without `auto_start`** it refuses (prints a message naming `--yes`, exits 1) instead of hanging on questionary or crashing on EOF, and an **interactive decline or Ctrl-C** exits 1 (was Exit(0)). This one root fix cures the inherited non-TTY defect across every caller (`run`/`backup`/`update`/`open`/`unlock`/`logs`/`inspect`). The `prompt=False` path is unchanged and load-bearing: it still returns `False` silently (no TTY check, no prompt, no Exit) for inspect Tier-2 and the `rm` recache path.
- **Destructive confirm gaining `--yes`:** `config cache clear --all` (uses `confirm_or_exit`).
- **Auto-start commands gaining `--yes`:** `run`, `backup`, `update`, `open`, `unlock`, `logs`, `inspect` thread `--yes` into `ensure_containers_running(auto_start=yes)`, so a stopped project auto-starts without the "start the containers?" prompt (non-destructive; enables non-interactive driving). For `inspect` this auto-start is scoped to the **Tier 3 full-inspect path** (`--update` / cache-miss / drift-escalation), which persists fresh data; the **Tier 2** cache-hit freshness pass stays passive and calls `ensure_containers_running(prompt=False, auto_start=False)` even under `--yes`, so a plain cache-hit `inspect` never starts a stopped project (preserving the T2 READ-ONLY contract above).
- **`start --yes`:** auto-confirms stopping conflicting Frappe projects to free their ports (`_check_port_conflicts(assume_yes=...)`). That port-conflict confirm also guards `sys.stdin.isatty()` before prompting: a non-TTY without `--yes` refuses (exit 1) rather than hanging, and Ctrl-C exits 1.
- **Honest exit codes (issue #39):** `start`/`stop`/`restart` exit 1 on a nonexistent project (never printing "started"/"stopped" for it); multi-project loops process every name then exit 1 if any failed (the `rm` failures-collector pattern - `_start_project` raises Exit(1) on not-found, `_stop_project`/`_restart_project` signal not-found with a `None` return distinct from a legitimate zero count). `config`'s error paths (`Error:` prints, invalid interval < 60, "not enabled" for `start`/`restart`, no-target `cache clear`, and every `except Exception`) now `raise typer.Exit(1)`; the deliberate carve-out is **idempotent success** (exit 0) for already-in-the-requested-state no-ops: `stop`/`restart`/`start`/`install-startup`/`uninstall-startup` "already stopped/running/installed". `update` folds a maintenance-mode-disable failure into `has_errors` and names the stuck site (`bench --site X set-maintenance-mode off`). `open`'s editor-select refuses (exit 1) on a non-TTY with multiple editors and no `--code/--code-insiders/--cursor/--docker` flag, and an interactive cancel exits 1.
- `status` issues no confirmation and needs no auto-start, so it has no `--yes`. `restore`/`rm` already had `--yes` (unchanged).

Regression coverage: `tests/test_bench_labels.py` (validation/resolution/marker I/O), `tests/test_bench_selector.py` (resolver cases incl. ambiguous/not-found), `tests/test_bench_label_db_and_command.py` (migration, `set_bench_label`, `label` command), `tests/test_inspect_label_recovery.py` (DB-loss recovery from markers via full inspect), `tests/test_yes_flag.py` (`confirm_or_exit`, `config cache clear --all`, `start`, auto-start).

## Cache never stores secrets

The SQLite cache (`~/.cwcli/cache/cwc-cache.db`) must never persist Frappe secrets (DB passwords, per-site `encryption_key`, admin/root passwords, Redis URLs).
Nothing ever reads secret values back from the cache: every credential consumer reads live from the container or from CLI flags/prompts (`restore.py` reads `encryption_key` live from a file in the container; MariaDB creds come from `_prompt_mariadb_credentials`), so a cached secret is pure write-only risk.

The single enforcement point is `_redact_config_for_cache` in `utils/db_utils.py`, applied at the ONLY write chokepoint (`cache_project_data`) before `_validate_config_json` and `json.dumps`, for BOTH `common_site_config` and per-site `site_config`.
It is a whitelist, not a blacklist: only `_COMMON_CONFIG_CACHE_KEYS` / `_SITE_CONFIG_CACHE_KEYS` are kept, everything else is dropped, so a future unknown Frappe secret key fails closed.
`default_site` MUST stay in the common whitelist or `restore`/`backup`/`unlock` lose default-site resolution (`get_default_site`).
Redact ONLY here, never in `inspect.py` (redacting at the inspect layer would strip in-memory dicts the same run may use and leave the write path fail-open).

Old caches written before this shipped still hold secrets, so `initialize_database()` runs a one-shot `_scrub_cached_config_secrets()` (idempotent, warn-and-continue, never raises) that re-filters every config row through the same whitelist, rewrites only rows that change, touches only `config_json` (never bumps `last_updated`), and replaces unparseable JSON with `"{}"`.

Rule: any NEW cached config field must be added to the relevant whitelist deliberately.
Filesystem permissions (0700 dir / 0600 file, `_set_secure_db_permissions`) remain as defense-in-depth, not the primary control.
Regression coverage: `tests/test_db_security.py` (`TestCacheRedaction` proves secrets stripped at write, scrub cleans an old row + is a no-op on clean rows, and `get_default_site` still resolves; `test_models_document_redaction` locks in that the old encryption TODO stays paid).

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
- The publish step runs `uv publish --trusted-publishing automatic --check-url https://pypi.org/simple/` inside the job's `ghcr.io/astral-sh/uv` container.
  It authenticates via GitHub OIDC trusted publishing (the `id-token: write` permission plus the `pypi` `environment:` block in the workflow), so there is NO `PYPI_API_TOKEN` secret to manage.
  `--check-url` is NOT twine's `skip-existing`: it skips ONLY a byte-identical re-upload (content-hash dedup, for retry/parallel-upload safety within the SAME build), and a same-name but different-content upload ERRORS with `Local file and index file do not match` (exit 2).
  Because Python builds are non-reproducible, re-tagging an already-published version rebuilds a byte-different artifact and FAILS rather than skipping - so each release must be a genuinely NEW version; do not expect re-tagging an existing version to skip gracefully.
  This replaced the old `pypa/gh-action-pypi-publish@release/v1` step, which is a Docker container action and cannot run from inside a job `container:` (GitHub tries to bootstrap it via `create-docker-action.py`, which is absent in the nested-container filesystem, and dies with `[Errno 2] No such file or directory`, exit 2 - the v0.33.0 tag never reached PyPI for this reason).
- One-time prerequisite (captain-only, not automatable in CI): a PyPI **trusted publisher** must be registered for this project on pypi.org - owner `karotkriss`, repo `caffeinated-whale-cli`, workflow `release.yml`, environment `pypi`.
  Trusted publishing fails with an auth error until that publisher exists; CI cannot create it.

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

- `ensure_containers_running(..., prompt: bool = True)` (`commands/utils.py`): with `prompt=False` it NEVER prompts - when the containers are not running (and `auto_start` is False) it returns `False` instead of asking. `inspect` forwards this via a hidden `--prompt-start/--no-prompt-start` option (`prompt_to_start`, default True) and, when `ensure_containers_running` returns False, prints a clear error and `raise typer.Exit(1)` (a stopped bench can't be inspected: every probe is an `exec_run`). `cache.recache_project` calls `inspect(..., prompt_to_start=False)`, so the recache is ALWAYS non-interactive and degrades to a `False` return. Standalone `cwcli inspect` keeps `prompt_to_start=True`, so it still prompts interactively as before (this is independent of `--interactive`, which only governs per-bench label naming).
- `_frappe_container_running(project)` (`commands/rm.py`): `rm()` checks this BEFORE entering the recache spinner and SKIPS the recache for a stopped project (printing "Containers for '{project}' are not running; skipping recache"). The recache only refreshes site info for a live backup, which is moot when nothing runs - and rm must NOT auto-start containers it is about to delete. This keeps the spinner off the stopped path entirely; the `ensure_containers_running` layer is defense-in-depth for any other non-interactive caller.

Regression coverage is in `tests/test_rm_stopped.py` (asserts no `questionary.confirm` is reachable from the recache path and that `rm` skips recache for a stopped project).

### Data-safety gates: backup gate, name validation, honest exit codes

Three fail-open data-safety gaps were closed in `commands/rm.py` (audit findings C1/H5/M11). Regression coverage is in `tests/test_rm_safety.py`, which drives the previously-untested backup path with a `FakeFrappeContainer` (records every `exec_run` and serves each backup artifact through a `get_archive` that streams a single-member tar in small chunks - the real copy primitive); every pre-existing rm test passed `no_backup=True`, so the backup gate had zero coverage. The streamed copy itself is proved by `TestStreamedCopy` (chunked consumption, exact reassembly, and truncated-stream/missing-file fail-closed) plus a `_backup_sites`-level assertion that the copy goes through `get_archive`, never a whole-file `cat`.

- **C1 - a failed/unverified backup must NOT allow volume/DB deletion.** `_backup_sites`' return value is now captured into `result["backup_ok"]` and gates deletion. Critically, `_backup_sites`' success signal was made trustworthy: a zero `bench backup` exit only means the dump was written INSIDE the container (the very volume about to be deleted), so each artifact is copied out of the container and VERIFIED on the host - the database dump (`_DB_DUMP_MARKER = "database.sql"`, matched case-insensitively) must be present and non-empty, and ANY copy-out failure fails the site closed (a backup missing a part is not trusted). The copy/verify set is scoped to the CURRENT backup run only - the artifacts sharing the newest `ls -1t` file's leading `<YYYYMMDD_HHMMSS>` timestamp token (`f.split("-", 1)[0]`), NOT a fixed top-N window. A fixed window (the former `backup_files[:5]`) bleeds into prior runs once a site has been backed up more than once, so a stale, unrelated artifact whose copy-out fails would falsely fail an otherwise complete fresh backup (and a stale non-empty dump could mask a fresh empty one); scoping by timestamp prefix keeps the strict fail-closed guarantee while verifying exactly this run's files. Non-DB artifacts (e.g. a files tar) may legitimately be small, so only the DB dump is size-checked. `_backup_sites` now returns True only if EVERY site fully backed up (no more `backed_up_count > 0` partial-success). The copy mechanism STREAMS each artifact out of the container with `get_archive` via the `_stream_container_file` helper - a `_ChunkStreamReader` (an `io.RawIOBase`) feeds `get_archive`'s tar chunks to `tarfile` in streaming mode (`mode="r|"`) and the single member is written to the host file in fixed-size chunks - NOT the former whole-file `cat` + `write_bytes`, which materialised the entire artifact in host RAM as one bytes blob and spiked memory on exactly the multi-GB `bench backup --with-files` backups that matter most. It stays fully fail-closed: `get_archive` raising (missing/unreadable path), a tar/read error, or a truncated stream (bytes written != the tar member's declared size) all return False and fail the site closed, and the load-bearing host-artifact verification (DB dump present and non-empty) is unchanged. `--no-backup` opts out of the whole gate (no backup attempted, `backup_ok` stays True) - it is the documented escape hatch to force removal without a backup.
  - **The backup gate is an EARLY abort, before any container is removed.** `backup_failed = remove_volumes and not no_backup and not result["backup_ok"]` is evaluated right after the backup/archive-config block and BEFORE the stop-and-remove-each-container loop; when true, `_remove_project` prints the refusal + retry hint, appends a failure, and `return`s immediately - no container is stopped or removed, no volume/dir is touched, and (because `failures` is non-empty) the cache is preserved. This closes the retry-orphan data-loss path: if the containers were torn down first and the gate blocked only afterward, a naive retry (`cwcli rm proj` again) would find no running container, attempt no backup (so `backup_ok` defaults True), pass the gate, and delete the DB volumes with NO backup - defeating C1. Aborting before container removal leaves the whole project (containers + volumes + dir + cache) intact and retryable, so a retry can still take a live backup from the still-running frappe container. The stopped/orphan path is unchanged: no container is running, so no backup is attempted, `backup_ok` stays True, and the early abort never fires there (a stopped DB cannot be dumped live - a previously-accepted tradeoff).
  - **The backup gate is scoped to volume deletion.** `backup_failed = remove_volumes and not no_backup and not result["backup_ok"]`. Under `--no-volumes` no volume data is destroyed, so a failed live backup does NOT abort (backup_failed is False) and does NOT force a non-zero exit - blocking it there would be a false failure. The default `--volumes` path stays fully fail-closed. The LATE gate's `container_removal_failed` and `archive_failed` conditions still apply regardless of `--no-volumes`.
  - **Site detection is fail-safe (`_list_sites`), not a denylist.** A real Frappe site is a directory containing `site_config.json`; each entry from `ls sites` is classified by a single `sh -c` probe into SITE / NOTASITE / AMBIGUOUS. An entry is EXCLUDED only on a positive NOTASITE (a non-directory, or a readable dir with no config); SITE, AMBIGUOUS (unreadable dir / probe error), unexpected output, or a non-zero probe exit all fail closed -> treated as a real site that must be backed up. This fixes the earlier fragile denylist that misclassified stray files like `currentsite.txt` (written by `bench use`) as sites and made the default `rm` wrongly refuse to delete a normal bench, while keeping C1 fail-closed under ambiguity (worst case blocks a delete, recoverable via `--no-backup`; never deletes a site's volume with no backup). `_backup_sites` and `_archive_project_config` both use `_list_sites`.
  - **Cache-clear is keyed on `not result["failures"]`, never on gate-not-blocked alone.** Containers are removed BEFORE the LATE (container/archive) gate, so `removed_count > 0` is true even when that gate blocks or a *post-gate* volume/dir/enumeration removal fails; the EARLY backup abort instead returns before removing any container (so `removed_count` stays 0), but it too records a failure. Clearing the cache in any of these cases would make a half-removed (or untouched-but-blocked) project vanish from `cwcli ls`/`inspect` while its data still occupies disk. Gating the `clear_cache_for_project` call on an empty `failures` list covers the early-abort, late-gate-blocked, AND post-gate failures uniformly, while still clearing on a clean full/orphan/`--no-volumes` removal.
- **H5 - project-name validation before any `rmtree`/volume op.** `_is_valid_project_name` (rejects empty/`.`/`..`/absolute/separator/NUL names) and `_is_safe_project_dir` (asserts `(PROJECTS_DIR / name).resolve()` is strictly inside `PROJECTS_DIR.resolve()`, which also catches a symlink-escape) guard three layers: the CLI filters names up front (mirroring the existing empty-name filter), `_remove_project` re-checks at entry (defense-in-depth for any internal caller), and `_delete_project_directory`/`_archive_project_directory` hard-guard right before the `rmtree`/copytree. Without this, `cwcli rm ..` resolved `PROJECTS_DIR / ".."` to `~/.cwcli` and `rmtree`'d every project + cache + config.
- **M11 - honest exit codes on partial failure.** `_remove_project`'s result dict gained `failures: list[str]`; volume-removal, dir-removal, container-removal, backup, invalid-name, and Docker-connection failures all append to it. `_remove_named_volumes` and `_delete_project_directory` take an optional `failures` collector (their int/bool returns are unchanged, so the existing `test_rm_truth.py` unit tests stay green). `rm()` raises `typer.Exit(1)` whenever any project reported failures (or a name was rejected) and no longer prints the green `✓ Successfully removed` on a partial failure. Two sharp edges fixed: `container_name` is bound to `"<unknown>"` BEFORE reading `container.name` (a docker-py property that can itself raise, which previously left `container_name` unbound → NameError in the except handler); and a caught container-removal error now sets `container_removal_failed`, which is part of the same LATE gate as archive, so it does NOT fall through to destroy the volumes/dir.

`_remove_project`'s data-destruction protection is split into two gates. The backup gate is an EARLY abort (`backup_failed = remove_volumes and not no_backup and not result["backup_ok"]`) evaluated BEFORE the container-removal loop; it `return`s without touching anything so a failed backup never tears down the containers a retry needs. The LATE gate is a combined check (`container_removal_failed or archive_failed`) placed AFTER container removal, exactly where the `conf/` archive gate already sat - it protects the irreversible data (named volumes + local dir); removing containers first is intentional and non-destructive (containers are recreatable, the named volumes hold the data). When calling `_remove_project`/`rm()` directly in tests, note it is `@handle_docker_errors`-decorated (patch `docker_utils.shutil.which` + `docker.from_env`) and, like other Typer commands here, real callers pass every param explicitly.

## `restore` command: receive-mode data-safety semantics

`restore --receive` (`restore_receive_mode` in `commands/restore.py`) downloads a peer's backup over sendme and then runs `bench restore --force`, which drops and recreates the live default site's database.
That is the most destructive path in the codebase, and it used to run the instant the download finished with no "are you sure" gate (the only interactive prompt was the *conditional* missing-apps confirm, which fires only when apps are missing).

The receive path now carries the same destructive-restore gate the normal path has, placed right before the `bench ... restore ... --force` command is built:

- It prints the `⚠ This will replace all data in site '{site}'` warning + the backup filename, then compares the backup's origin site (parsed from the database filename via `parse_backup_filename(...)["site_name"]`, which is already dots-to-underscores) against `transform_site_name_to_backup_format(site)` and prints an `⚠ Origin mismatch` line when they differ.
  A failed parse yields `origin_parsed is None`, so the mismatch check is skipped (fail-safe, no false alarm); this is the same parser that must already have matched to set `database_file`, so a hyphen-bearing site that the parser can't represent would have errored earlier.
- The confirmation honors the new `--yes/-y` flag on `restore`: `--yes` proceeds without prompting, an interactive TTY asks `questionary.confirm("Are you sure you want to restore?")`, and a **non-TTY without `--yes` refuses and exits non-zero** rather than silently proceeding or exiting 0.
  This confirm exits **non-zero on any refusal** (declined confirm, non-TTY-no-yes, or Ctrl-C), as does the normal path now (issue #40 closed the prior exit-0-on-cancel finding).
- `--yes` is threaded through both `restore_receive_mode(...)` call sites and BOTH confirm blocks on the normal path (the missing-apps "continue anyway?" and the destructive "are you sure?"). It bypasses every confirmation prompt on both paths; it does NOT remove the sendme-ticket prompt (receive path) or the `--mariadb-root-password` / `--mariadb-root-username` credential prompts, which already meet the standard via `_prompt_mariadb_credentials`.

Two supporting fixes travel with it, and BOTH the receive path and the normal restore path share the same shapes:

- **Secrets off the argv (M5):** the MariaDB root password (and admin password) are no longer interpolated into the command string. They are put in a `restore_env` dict and referenced as `"$CWCLI_MARIADB_ROOT_PASSWORD"` / `"$CWCLI_ADMIN_PASSWORD"`, and the command is executed as `frappe_container.exec_run(["sh", "-c", cmd], workdir=..., environment=restore_env)`.
  Passing the list form `["sh", "-c", cmd]` (rather than a bare string, which docker-py would `shlex.split` and exec directly) is REQUIRED so the shell actually expands the `$VAR`; the secret then never appears in the command the container process list shows.
  cwcli's own verbose masking (`cmd.replace(password, "***")`) only hid it from cwcli output, not from `ps`/`docker top`/exec-inspect - that masking is now gone because the secret is not in `cmd` at all.
- **Streamed tar copy (M4):** the file-copy loop passes the open tar file handle straight to `frappe_container.put_archive(backup_dir, tar_file)` (docker-py streams it) instead of `tar_file.read()` (which slurped a multi-GB `--with-files` backup into host RAM), and it now checks `put_archive`'s bool return and `raise typer.Exit(1)` on failure instead of ignoring it and surfacing a confusing "backup not found" later.

Regression coverage is in `tests/test_restore_safety.py`: it drives `restore_receive_mode` with a `FakeReceiveContainer` that records every `exec_run` (command, workdir, environment) and asserts a declined/non-TTY confirm does NOT run `bench restore --force` and exits non-zero, `--yes` proceeds, an origin mismatch is surfaced, and the DB password rides in `environment=` (never in the recorded argv).
Testing note: `restore_receive_mode` is a plain function (not the Typer command), so tests call it directly with all args explicit; stub `TipSpinner` to a no-op (it starts a Rich spinner even with `enabled=False`) and fake `subprocess.run` to write the "downloaded" backup into its `cwd`.

## `restore` command (normal path): non-interactive selectors + honest exit codes (issue #40)

The normal (non `--send`/`--receive`) `cwcli restore` path is now fully drivable by an agent or script. Every prompt gets a flag, and a non-TTY without the needed flag refuses with a NON-ZERO exit instead of silently exiting 0 (the prior "exit-0-on-cancel" finding is closed). This was the deferred "AXI task" called out above.

- **Backup selection.** `--latest` non-interactively selects the newest backup set for the target site; `--backup-file <filename-or-full-path>` selects one by its database file. The selector bypasses the `questionary.select` menu. `--latest` does NOT fall through to other-site backups (that would silently restore a different site's data); `--backup-file` searches both target and other backups (the caller named a specific file). The two flags are mutually exclusive, and neither applies to `--send`/`--receive` (validated up front). The resolution happens in `select_backup_set(target_backups, other_backups, *, latest, backup_file)` - a pure helper near `display_backup_selection_menu` - so it is unit-testable with no TTY or container.
- **Non-TTTY guard.** A non-TTY with neither selector exits 1 (message names `--latest`/`--backup-file`) rather than reaching `questionary.select().ask() -> None` on a non-TTY and exiting `0` having done nothing.
- **`--yes` widened to the normal path.** Both normal-path `questionary.confirm` prompts (the missing-apps "continue anyway?" and the destructive "are you sure?") now honor `--yes`: `--yes` proceeds without prompting, a non-TTY without `--yes` refuses with exit 1, an interactive TTY asks. A declined confirm or Ctrl-C now exits 1 (was `Exit(0)`) on BOTH the normal path and the receive path.
- **The sentinel** `{"_restore_from_ticket": True}` (remote restore via sendme, reached from the menu) stays INSIDE the interactive branch only; the selector branch can never produce it (correct: remote restore is reached via `--receive`).
- Both confirms still pass `auto_enter=False` (load-bearing - they must consume their own trailing Enter so the keystroke does not leak into the following credential prompt). `commands/utils.py:confirm_or_exit` omits that setting, so it is NOT used here; the local prompt code is kept.
- Credentials fire AFTER the confirms via `_prompt_mariadb_credentials` (unchanged, already meets the standard); do not reorder.

Regression coverage is in `tests/test_restore_safety.py` (`TestSelectBackupSet` for the pure helper, `TestNormalPathSelectorsAndExitCodes` for the Typer command). The Typer-command tests call `restore_mod.restore(...)` with ALL params explicit (omit any `Option` and its truthy default object leaks through - the same trap flagged for `inspect`); the `@handle_docker_errors` decorator is bypassed by patching `docker_utils`'s `shutil`/`docker` (the decorator reads them from its own namespace; `restore_mod` does not import them directly).

## `restore` + `inspect`: currentsite, default site, receive path, credential prompts, missing-apps, migrate+restart

Six bugs the captain hit on a live 0.33.0 session (Frappe `version-14`, site `development.localhost`) were fixed together.
The authoritative repros and the real-instance E2E evidence are in `docs/e2e/restore-inspect-e2e-r6.md`.

### Site detection is shared (`utils/bench_sites.py`)

`bench_sites.list_sites(container, bench_path)` is the single canonical "what are the real sites" implementation, used by BOTH `commands/rm.py:_list_sites` (which now just delegates) and `commands/inspect.py:_get_sites`.
A real Frappe site is a DIRECTORY containing `site_config.json`; detection probes for that per entry rather than denylisting known non-site names.
It is fail-safe: an entry is excluded only on a positive NOTASITE (a non-directory, or a readable dir with no `site_config.json`); anything ambiguous (probe error, unreadable dir) is treated as a site.
This replaced `inspect._get_sites`' old denylist `{"apps.txt", "assets", "common_site_config.json", "example.com", "apps.json"}`, which did NOT list `currentsite.txt` (a plain file written by `bench use`), so inspect reported it as a site and then errored `bench --site currentsite.txt list-apps -> "Site currentsite.txt does not exist!"`.
`bench_sites.read_current_site(container, bench_path)` reads `sites/currentsite.txt` (the default-site pointer), failing safe to `None`.
The probe string in `list_sites` is byte-identical to the one rm's tests already assert, so rm's fakes stay green; `tests/test_inspect_partial_refresh.py`'s fake gained the same SITE/NOTASITE probe handling.

### Default site resolves from EITHER source (`currentsite.txt` OR `common_site_config.json`)

A bench records its default site in `common_site_config.json`'s `default_site` OR `sites/currentsite.txt`; a plain `bench use`d dev bench only has the latter (the captain's `ners` had no `default_site` key at all).
`Bench.current_site` is a new nullable column (idempotent `_migrate_bench_current_site_column`, mirroring the `label` migration) populated by a full inspect from `currentsite.txt`, carried forward by the T2 partial pass, and surfaced by `get_cached_project_data`.
`db_utils.get_default_site` now returns `common_site_config.default_site` if set, else the cached `current_site` (via `get_current_site`) - so `restore`/`backup`/`unlock` all resolve the default even with no `default_site` key.
`inspect`'s tree marks `(default)` from either source too.
`restore.py:_resolve_default_site` adds a LIVE `currentsite.txt` read as a final fallback for the destructive restore path (a cold/stale cache still resolves correctly); it is used by both the normal and receive paths in place of the bare `get_default_site`.

### `--receive` restore: full container path, not the bare filename

Receive-mode built `bench restore <bare-filename>` with `workdir=backup_dir`; on Frappe `version-14` bench rejects that with `Invalid path <db filename>` (data restore broken).
The fix passes the FULL container path `{backup_dir}/{database_file.name}` and reorders the args to exactly match the normal path (db path, credentials, `--force`, then `--with-public-files`/`--with-private-files`).
On Frappe `version-15` bench has a "trying alternative directories" fallback that masks the bare-filename bug, so this only reproduces on v14 - which is why the E2E built a v14 bench (`docs/e2e/`).

### Credential prompting works in BOTH modes (`_prompt_mariadb_credentials`)

Shared by the normal and receive paths.
Interactive (a TTY): prompt for the username (default `root`, blank keeps `root`) AND the password (blank is an error).
Non-interactive (flags / non-TTY): the username defaults to `root`; the password is a required secret, so a non-TTY WITHOUT `--mariadb-root-password` refuses with a non-zero exit rather than hanging or proceeding empty.

The interactive "empty password" bug and its ROOT fix (sharp edge, worth remembering for ANY questionary confirm followed by another prompt):
`questionary.confirm(...)` with the default `auto_enter=True` submits on the `y`/`n` keypress and leaves the user's habitual trailing Enter in prompt_toolkit's INTERNAL input buffer.
That stray Enter is then read by the immediately-following prompt as an empty submit, so the password prompt returned empty (`Error: Password cannot be empty.`).
A `termios.tcflush` stdin drain was tried and proven INEFFECTIVE: `FIONREAD` shows the OS tty queue is empty (the Enter is inside prompt_toolkit, not the OS queue), so a tcflush reaches nothing - it was removed.
The fix is `auto_enter=False` on EVERY restore confirm (both the destructive `Are you sure you want to restore?` and the missing-apps `continue anyway?`, on both paths): with `auto_enter=False` the confirm REQUIRES Enter to submit and thus CONSUMES its own trailing Enter, so nothing leaks into the next prompt - robust regardless of which credential flags are set.
(An earlier partial mechanism - the username prompt absorbing the stray Enter - only worked in the default flow where the username prompt runs; it silently failed for `--mariadb-root-username <flag>` + habitual `y`+Enter, which is why a realistic pty E2E must press `y` THEN Enter.)

### Missing-apps warning reads the BACKUP's apps (`check_missing_apps` + `_read_backup_installed_apps`)

The warning regressed to silence because `check_missing_apps` read `sites/{site}/apps.json`, which Frappe does NOT write per site (it lives at the bench level, `sites/apps.json`), so the `cat` always failed and it returned `[]`.
The correct question is "does the BACKUP need apps this bench lacks" (the captain's words), not "what does the about-to-be-overwritten site have" - and a site whose app code is already missing cannot even be listed by `bench list-apps` (the import crashes).
`_read_backup_installed_apps` `zcat -f`s the backup's DB dump and greps the `installed_apps` global (`...,'["frappe","widgets"]','installed_apps',...` - exactly what `frappe.get_installed_apps()` reads), extracting the app-name tokens.
`check_missing_apps(frappe_container, project_name, bench_path, backup_db_path, ...)` (the `site` arg became `backup_db_path`) compares those against the bench's apps read LIVE from `ls {bench_path}/apps`, and returns `backup_apps - available_apps`.
It fails safe: an unreadable dump / absent marker yields `None` -> no warning (never a false positive).
Both call sites pass the backup's container path (`selected_backup["database"]["full_path"]` on the normal path; `{backup_dir}/{database_file.name}` on the receive path).
Because both sides are now read LIVE (the backup's dump and `ls apps`), the check no longer touches cwcli's cache, so `check_missing_apps` dropped its `cache.recache_project` call and `--no-recache` became a DEPRECATED no-op - the flag (and its `no_recache` param) is kept only for backward compatibility with existing callers.

### Post-restore: migrate then restart (`_post_restore_migrate_and_restart`)

After a successful `bench restore`, both paths run `bench --site <site> migrate` then restart the instance (via `_start_project`, which kills the old `bench start` and relaunches it - the same app restart `cwcli restart` does).
The restart targets the SAME bench that was just restored via `_start_project`'s new `bench_path_override` (used verbatim, bypassing the `resolve_bench_path` guessing), so a multi-bench restore into a non-first bench restarts the right dev server, never bench 0.
A failed migrate does NOT undo the restore; it is surfaced and the restart still runs, but the function returns False so the caller exits non-zero.
A Docker/API exception raised by the migrate `exec_run` is treated as a failed-but-reported migrate (`exit_code=1`) that STILL continues to the restart, so an exec error can never skip bringing the instance back up.
`--no-migrate` skips the whole post-restore step.

Regression coverage: `tests/test_restore_inspect_fixes.py` (all six) and `tests/test_bench_labels`-style DB tests; `tests/test_inspect_partial_refresh.py` and `tests/test_restore_safety.py` fakes were updated for the shared site probe and the `no_migrate` param.

## Interactive AND non-interactive modes: support and test BOTH (captain standard, 2026-07-04)

Every cwcli command that prompts the user (destructive confirmations, credential entry, selection menus) MUST work in two modes and be E2E-verified in BOTH on a real instance, not just unit tests:

- **Interactive** (a human at a TTY): every prompt is shown AND its input is actually collected.
  A prompt that is skipped, or that returns empty without waiting for input, is a bug (e.g. the restore flow's MariaDB username/password prompts skipping after the "are you sure?" confirm).
- **Non-interactive** (an agent/automation, or any non-TTY): every prompt has a corresponding flag so the command runs to completion with NO prompt - `--yes`/`-y` for confirmations, `--mariadb-root-username`/`--mariadb-root-password` for credentials, `--site` and backup selectors, etc.
  Under a non-TTY WITHOUT the needed flag, a destructive or blocking prompt must refuse with a non-zero exit rather than silently proceeding, defaulting, or hanging.

This is an AXI requirement (agents drive cwcli non-interactively) and a UX requirement (humans get working prompts).
When adding or changing any prompting command: add/verify the non-interactive flags, make the interactive prompts genuinely collect input, and exercise BOTH paths in a real-instance E2E (drive the TTY prompts via a pty/expect, waiting for prompt_toolkit's raw-mode readiness marker `ESC[?2004h` before each keystroke).

## Re-run the real-instance E2E AFTER the no-mistakes run and AFTER any CodeRabbit fixes (captain standard, 2026-07-05)

Unit tests + a green no-mistakes pipeline are NOT sufficient proof for a behavior change.
The no-mistakes review/document steps and CodeRabbit's post-pipeline suggestions frequently produce behavior-altering fixes (e.g. `start` skip-and-continue vs abort, `label --clear` DB/marker consistency, `inspect` Tier-2 read-only, restore path/prompt changes).
Those fixes land AFTER the original E2E was run, so they ship re-validated only by unit tests.

Standard: after the no-mistakes run completes AND after applying any CodeRabbit fixes, run the real-instance E2E AGAIN (on an isolated throwaway instance) to confirm the final shipped code still behaves correctly end-to-end - not just that the unit suite is green.
Treat "CodeRabbit fixes applied" as a trigger to re-E2E, the same way you would after any late behavior change.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
