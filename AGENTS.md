# Project agent memory

This file is the project's committed, distilled agent-memory.
It is two tiers: a tight orientation core at the top, then a `## Sharp edges (verified incident detail)` appendix that preserves the per-command root-cause notes each guarding a real shipped bug.
Read the core first; drop into a sharp edge only when you are about to touch that command.
For anything the codebase already shows, this file points to the authoritative source or doc rather than restating it.

`CLAUDE.md` is a symlink to this file; keep it that way.

## Project Context

Caffeinated Whale CLI (`cwcli`) is a command-line tool for managing Frappe/ERPNext Docker instances during local development.
It covers instance lifecycle (init, start, stop, restart, rm), bench and site operations (inspect, run, open, unlock, update, label), and backup/restore including peer-to-peer transfer over sendme.
The source of truth for the user-facing feature set is `README.md`.

## Repository Layout

- `src/caffeinated_whale_cli/commands/` - one module per CLI command (`init`, `inspect`, `rm`, `restore`, `start`, `run`, `label`, `config`, ...) plus `utils.py` (shared resolver, confirm, container-running helpers).
- `src/caffeinated_whale_cli/utils/` - cross-command building blocks: `db_utils.py` (SQLite cache + migrations + secret redaction), `bench_labels.py` (label model + marker I/O), `bench_sites.py` (site detection), `docker_utils.py`, `cache.py`, `sendme_utils.py`.
- `tests/` - pytest suites (19 `test_*.py` files, ~356 tests); `bench_fakes.py` and `bench_fakes_mb.py` hold the container fakes. See `tests/README.md` for the coverage map.
- `docs/e2e/` - worked real-instance E2E evidence (the canonical examples for the recipe below); `docs/technical/`, `docs/testing/`, `docs/contributing/` hold design, test, and workflow docs.
- Version-bump touches exactly four files: `pyproject.toml` (`version`), `src/caffeinated_whale_cli/__init__.py` (`__version__`), `uv.lock` (regenerate with `uv lock`), and `CHANGELOG.md`.
- `.github/workflows/` - `lint.yml`, `test.yml`, `build.yml`, `release.yml`.

## Terminology

Use **instance** as the top-level term throughout.

- **Instance** - one docker-compose deployment (its frappe container, db, redis, named volumes, network).
- **Project** - cwcli's name/identifier for an instance; the compose project label `com.docker.compose.project={name}` and the local config dir `~/.cwcli/projects/{name}/`.
  Code symbols keep the `project_name` / `PROJECTS_DIR` spelling.
- **Bench** - a bench directory inside an instance's frappe container.
  One instance can hold several benches (a **multi-bench** instance).
- **Numeric label** - a bench's index into the stable sorted-by-path order (0, 1, 2, ...).
  It shifts when a bench is added or removed, so it is NOT durable.
- **User label** - a durable per-bench handle stored in the DB and in a marker file inside the bench.
- **Freshness tiers** - `inspect`'s cache strategy: T1 serve cache, T2 partial read-only refresh, T3 full re-cache.
- **Default site** - a bench's default site, read from `common_site_config.json`'s `default_site` OR `sites/currentsite.txt`.
- **Receive-mode** - `restore --receive`, the sendme download-then-restore path (the most destructive path in the codebase).
- **Sendme** - the iroh-based peer-to-peer blob transfer used by `restore --send`/`--receive`.

## Important Components

Each entry is the contract; the linked source file is authoritative and the Sharp edges appendix holds the root-cause detail.

- **`init` version gating** (`commands/init.py`) - `bench new-site` MariaDB flag, Python/Node versions, and `setuptools` pin depend on the Frappe branch.
  Existing-bench decline continues on a fresh name instead of dead-ending.
  See Sharp edges: `init`.
- **`inspect` 3-tier freshness** (`commands/inspect.py`) - cache-backed with a read-only drift check; escalates to a full re-cache only on real drift.
  See Sharp edges: `inspect`.
- **Multi-bench addressing** (`utils/bench_labels.py`, `commands/utils.py:resolve_bench_path`) - `--bench <index|label>` selects a bench; a multi-bench op with no selector errors instead of guessing.
  Labels persist to BOTH the DB and an in-bench marker file for cache-loss recovery.
  See Sharp edges: multi-bench.
- **`rm` deletion + backup gate** (`commands/rm.py`) - removes named volumes and the project dir that `Container.remove(v=True)` leaves behind, gated by a verified live backup of EVERY bench that fails closed.
  See Sharp edges: `rm`.
- **`restore` safety** (`commands/restore.py`) - destructive confirm on both normal and receive paths, non-interactive selectors/flags, secrets off the argv, streamed copies, post-restore migrate + restart.
  See Sharp edges: `restore`.
- **Cache never stores secrets** (`utils/db_utils.py`) - a whitelist redaction at the single write chokepoint; nothing reads secrets back from the cache.
  See Sharp edges: cache secrets.
- **`apps` command group** (`commands/apps.py`) - first-class app management (list/install/uninstall/update) per bench and multi-site by default; reuses the shared resolver/confirm/site-detection/recache primitives; `cwcli update` is now a deprecated alias for `apps update`.
  See Sharp edges: `apps`.

## End-to-End Testing

There is no runnable E2E harness and you must NOT build one.
Real validation is pexpect driving `cwcli` against throwaway Frappe Docker benches.
The canonical worked examples are `docs/e2e/restore-inspect-e2e-r6.md` and `docs/e2e/bench-ux-m7-multi-bench.md` (and `docs/e2e/restore-noninteractive-h2.md`); follow the procedure below and point back to them.

1. **Isolate everything.**
   Set a temporary `HOME` so `~/.cwcli` is a fresh SQLite DB, use unique docker-compose project names (for example `cwe2e-<something>`), and dedicated volumes/network.
   NEVER touch the captain's real projects or real `~/.cwcli`.
2. **Build genuine benches.**
   Use `cwcli init` + `bench init` to create real benches, not fixtures.
   Build BOTH Frappe `version-14` and `version-15` when the behavior is version-sensitive; some bugs only reproduce on v14 (for example the receive-mode bare-filename `Invalid path` bug, which v15's alternative-directory fallback masks).
3. **Drive interactive prompts through a real pty (pexpect).**
   Await prompt_toolkit's raw-mode readiness marker `ESC[?2004h` before each keystroke so nothing races the prompt.
   For a confirm-then-prompt flow, press `y` THEN Enter, the way a human types it, so the confirm consumes its own trailing Enter (see the `auto_enter=False` credential note in Sharp edges: `restore`).
4. **Drive non-interactive paths with flags.**
   Use `--yes`/`-y`, `--mariadb-root-username`/`--mariadb-root-password`, `--site`, and the backup selectors (`--latest`/`--backup-file`).
   A non-TTY without the needed flag must refuse with a non-zero exit, not hang or silently proceed.
5. **Tear down all throwaway instances afterward** (containers, volumes, network, temp `HOME`), and confirm the captain's real instances are untouched.

## Conventions

- **Tooling.** Use `uv` (`uv sync --frozen --all-extras`, `uv run ...`).
  Format with black and lint with ruff, both at line-length 100 and scoped to `src/` (`uv run black --check src/`, `uv run ruff check src/`).
- **Types.** Keep `uv run mypy src/` at zero errors; it is a blocking gate.
  Prefer accurate annotations over `# type: ignore` (there are none in `src/`), and add the matching `types-*` stub (`types-requests`, `types-toml` are dev deps) rather than ignoring an untyped import.
  Do not loosen `[tool.mypy]` to hide errors.
- **Tests.** `uv run pytest` (with `pytest-cov`).
  Typer commands keep their `typer.Option(...)` default objects when a param is omitted, so tests must pass every param explicitly (see the `inspect`/`restore` notes in Sharp edges).
- **CI gates.** `lint.yml` runs black + ruff; `test.yml` runs the `Pytest` and `Mypy` jobs on pushes and PRs to all branches (default branch is `develop`).
  Both jobs are the intended required gates, but `develop` has no branch protection, so a repo admin must still tick `Pytest` and `Mypy` as required status checks for them to block merges.
- **Release.** See "Cutting a release" in Sharp edges: release.
- **Commits.** Author commits as `karotkriss <mckay.christopher73@outlook.com>` only.
  Never add an agent name as a co-author.
  Never hand-edit `CHANGELOG.md` outside a deliberate version bump, and never edit auto-generated files.
- **Captain standards (both are load-bearing):**
  - Support AND test BOTH interactive and non-interactive modes for every prompting command, and E2E-verify BOTH on a real instance (see Sharp edges: interactive + non-interactive).
  - Re-run the real-instance E2E AFTER the no-mistakes run AND after any CodeRabbit fixes, because those steps often produce behavior-altering fixes validated only by unit tests (see Sharp edges: re-run E2E).

---

## Sharp edges (verified incident detail)

Each note guards a real shipped bug.
Keep the root-cause "why" so a later change does not silently re-break the fix.

### `init` command: Frappe/bench version gating

`bench new-site` MariaDB flag depends on the Frappe branch (see `_select_mariadb_flag` in `commands/init.py`):

- `version-13` and `version-14` -> `--no-mariadb-socket`
- `version-15`+ -> `--mariadb-user-host-login-scope=%` (this flag only exists in bench/Frappe 15+; passing it to bench 14 makes `bench new-site` fail)

Other per-branch settings nearby in `init.py`: Python version (`branch_python`: 15->3.12, 14->3.10, 13->3.9), Node major (`branch_node`: 14->16, 13->14), and `setuptools<82` is pinned only for `version-13`.

#### `init` existing-bench flow: decline must continue, not dead-end

The devcontainer image ships a `/workspace/frappe-bench`, so a fresh `cwcli init` usually finds an already-existing bench at the default `bench_parent/bench_name`.
`_resolve_bench_target(frappe_container, bench_parent_path, bench_name, reuse_bench=None)` in `init.py` owns this branch and returns `(bench_name, bench_full_path, bench_exists)`.

The tri-state `reuse_bench: bool | None` flag (`init`'s `--reuse-bench/--no-reuse-bench`, default `None`) pre-answers the existing-bench question so the command is fully agent-drivable (issue #41).
When the bench exists, the resolver short-circuits BEFORE the interactive `while bench_exists` loop:
- `reuse_bench is True` (`--reuse-bench`): reuse with no prompt, return `bench_exists=True` (`bench init` skipped), exactly like an interactive Yes.
- `reuse_bench is False` (`--no-reuse-bench`): refuse to reuse; print an error naming the path and advising a different `--bench`, then `raise typer.Exit(1)`. It must NEVER enter the rename loop (a non-interactive loop would spin forever).
- `reuse_bench is None` and `sys.stdin.isatty()`: the unchanged interactive issue #20 loop below.
- `reuse_bench is None` and NOT a TTY: refuse with an honest `Exit(1)` naming both flags, BEFORE touching questionary. This replaces the old non-TTY hang (idle pipe) / `EOFError` crash (closed stdin); `.ask()` only catches `KeyboardInterrupt`, so the guard must be `sys.stdin.isatty()` checked up front.
When the bench does NOT exist the flag is irrelevant (returns `bench_exists=False`); `--no-reuse-bench` with a fresh name is a useful no-op that just asserts freshness ("create a NEW bench or fail").

Interactive loop (only reached when `reuse_bench is None` AND a TTY): "Reuse the existing bench ...?": Yes reuses it (`bench_exists=True`, `bench init` skipped); No prompts for a different bench name and loops, so site setup continues on a fresh bench (`bench_exists=False`).
A blank replacement name or a cancelled prompt (`.ask()` returns `None`) exits cleanly with code 0 and "No changes made." - this exit-0 interactive-cancel is the DELIBERATE issue #20 behavior; only the NON-TTY path (where "no changes" is a failure to do the requested job) exits 1.
This replaced the old behavior where declining reuse just `raise typer.Exit(0)` and aborted the whole command (issue #20).
The resolver runs after the setup spinner has exited, so its prompts own the terminal; keep it out of any `console.status`/`TipSpinner` block.
`inputs.bench_name` is reassigned from the resolver's return (so `bench init` and the site paths use the chosen name), which is valid because `InitInputs` is a plain mutable `@dataclass`.

Spinner-race fix (issue #41): `ensure_containers_running` used to be called INSIDE init's `TipSpinner` with `prompt=True`, so a slow container start with no `--auto-start` could paint a questionary confirm under the live spinner (the repo's known deadlock pattern).
Now `_wait_for_containers_running(project, ...)` runs a bounded SILENT poll (`ensure_containers_running(..., prompt=False, auto_start=False)`, ~10 x 0.5s) INSIDE the spinner - safe because it never prompts and absorbs normal startup latency (a container is often ~200ms from ready right after `compose up -d`, so a single check mis-reads it as down).
Only if that returns `False` does init call `ensure_containers_running(..., auto_start=auto_start)` (which prompts on a TTY or refuses on a non-TTY) OUTSIDE the spinner, then fetches the frappe container. `--reuse-bench` and `--auto-start` are separate axes (bench reuse vs container start); do not merge them.
Regression coverage is in `tests/test_init_reuse_bench.py` (flag/non-TTY resolver cases in `TestReuseBenchFlag`, poll behavior in `TestWaitForContainersRunning`).

### `inspect` command: 3-tier freshness model (cache / partial / full)

`inspect` is cache-backed and used to be cache-first with no staleness check, so an app installed after the cache was written (it lands in the bench `apps/` dir immediately, but the cache still held the old list) stayed invisible until `inspect --update` rebuilt the cache.
`open --app <name>` read the same stale cache and errored "App not found".
This was issue #27 ("need to inspect -uv on new projects to see installed apps"; `-uv` = `--update --verbose`, only `--update` matters).

The fix (`commands/inspect.py`) is a 3-tier model, chosen so `inspect` stays fast while serving fresh data:

- **T1 - cache return (unchanged, instant):** no container calls. Used with the `--no-refresh` flag, or automatically when the containers are not running (the running check is `ensure_containers_running(..., prompt=False)`, which never prompts or starts - non-disruptive).
- **T2 - partial inspect (`partial_inspect_known_benches`):** the default for a cached-and-running instance, and a pure READ-ONLY drift detector that NEVER writes the cache. For each KNOWN bench path already in the cache it cheaply re-reads only `ls apps` (available_apps) and `ls sites`, plus a `test -d` bench check. It deliberately SKIPS `_find_bench_instances` (the `find`-based instance discovery), the deep per-site `bench list-apps` (which boots Frappe), AND any config re-read: the cached per-site `installed_apps`, the cached per-site `site_config`, and the cached `common_site_config` are all carried forward from the cached structures (so a transient unreadable/half-written config can never silently drop `common_site_config`/`default_site`). It returns `(refreshed, drift)`. A freshly installed app shows up in `apps/`, so the cheap `ls` catches it.
- **T3 - full inspect (unchanged):** the existing `--update` / cache-miss path: `find` discovery + per-site `bench list-apps` + re-cache.

Escalate-on-drift: when T2 sees the on-disk available-apps/site set diverge from the cache (or a known bench vanished) it returns `drift=True` and `inspect` sets `bench_instances_data = None` to fall through to a full T3 inspect, so the deep per-site lists and any brand-new bench are refreshed AND persisted. No drift -> `inspect` serves the cached data UNCHANGED and does NOT write the cache (the former write-on-read that bumped `last_updated` on every cache hit is gone). T2 is wrapped so any failure degrades to serving the cached data (never worse than the old behavior).
Guarded escalation: the drift-triggered full inspect is NOT allowed to hard-fail a previously-working read. Before falling through, `inspect` remembers the cached benches in `drift_fallback_benches`; if the full inspect's `_find_bench_instances` then discovers NO bench (e.g. a custom search path was removed via `config remove-path`, or the bench was never under the default search roots), `inspect` degrades to that cached data and serves it WITHOUT persisting, instead of `raise typer.Exit(1)`. The hard "No Bench Instances found" error is preserved ONLY for the `--update` / cache-miss paths, which have no valid cache to fall back on (`drift_fallback_benches is None`). To keep the raise out of the spinner, the no-benches case is captured as a flag inside the `TipSpinner` and branched on after the `with` block, and `cache_project_data` is called only when benches were actually gathered.
`open --app` reuses this via `partial_inspect_known_benches(frappe_container, cached_data["bench_instances"], verbose=verbose)` IN-MEMORY only - it does NOT persist (degrading to the cached available-apps on any error). It matches the refreshed entry to the chosen bench BY PATH (`b["path"] == bench_instance["path"]`), not by indexing `refreshed[0]`, because the partial pass drops any vanished bench and so can be index-shifted relative to the cached list; on no match or an empty result it keeps the cached available-apps. Leaving the cache untouched is deliberate: the next plain `cwcli inspect` still self-heals via escalate-on-drift (which also refreshes the deep per-site installed lists), instead of `open` consuming the drift signal by writing fresh `available_apps` while carrying stale per-site `installed_apps` forward.

A sharp edge: T2 cannot cheaply refresh per-site `installed_apps` (that needs the deep `bench list-apps`); it relies on escalate-on-drift to bring those up to date when `apps/` or the site set changes. A pure `install-app` of an app already present in `apps/` (no new dir) would not trip drift, but in practice a newly installed app appears in `apps/` first, which is exactly the reported case.

Calling `inspect` directly in tests is a trap: it is a Typer command, so any omitted parameter keeps its `typer.Option(...)` default OBJECT (truthy) - e.g. an unspecified `update` silently forces a full inspect, and an unspecified `no_refresh` silently takes the T1 serve-cache-verbatim branch. Real callers (`recache_project`, `auto_inspect`, `start`, `update`, `restore`, `open`) pass ALL params explicitly, including `no_refresh=False` and `yes=False` (the `--yes` auto-start flag); tests must too. Regression coverage is in `tests/test_inspect_partial_refresh.py` (uses a `FakeFrappeContainer` that records every `exec_run` to assert which tier ran, and asserts T2 no-drift performs no cache write).

### Multi-bench UX: labels, `--bench` selector, marker-file recovery

An instance is one docker-compose deployment; its frappe container can hold more than one bench directory (a multi-bench instance).
The addressing model lives in `utils/bench_labels.py` and the shared resolver `commands/utils.py:resolve_bench_path`.

#### Label model
- **Numeric label = position.** `_find_bench_instances` returns `sorted(set(...))` (deterministic). A bench's numeric label is its index into that stable sorted-by-path order (0, 1, 2, ...). Positions can shift when a bench is added/removed, so numeric labels are NOT durable - a user label is.
- **User label = durable handle.** Optional per-bench string. `--bench <selector>` resolves a user label FIRST, then a numeric index (`bench_labels.resolve_bench`). The two namespaces can't overlap because a user label may NOT be purely numeric (`validate_user_label` rejects `^\d+$`, enforces charset `[A-Za-z0-9._-]`, max 64). Duplicate labels within an instance are rejected by the caller (`label` command / `inspect -i`), which has the sibling benches to check against.
- `get_cached_project_data` returns benches ordered by `Bench.id` (== insertion == sorted discovery order), so `bench_instances[i]` is always numeric label `i`. Do not reorder it.

#### Storage + marker-file recovery (the load-bearing contract)
- The user label is persisted in BOTH the SQLite cache AND a per-bench marker file `<bench-root>/.cwcli/.bench-label` (JSON `{"schema": 1, "label": "..."}`, room for future fields). The marker lives INSIDE the container/bench, so it survives a cache wipe. This is the recovery mechanism: if the SQLite DB is lost, a **full inspect** (`--update` / cache-miss) rebuilds each label from its marker (`_gather_bench_data` calls `bench_labels.read_label_marker`) and re-derives the rest of the bench config live, then re-persists. The marker is the source of truth for labels; T1/T2 serve the DB label (kept in sync by every writer), and `partial_inspect_known_benches` carries the cached label forward (it does NOT re-read the marker).
- **DB schema:** `Bench.label` is a nullable column added AFTER the initial schema. `create_tables(safe=True)` never adds a column to an existing table, so `initialize_database()` runs `_migrate_bench_label_column()` (a `PRAGMA table_info(bench)` check + `ALTER TABLE bench ADD COLUMN label VARCHAR`). This is idempotent and keeps old (`label`-less) caches working - existing rows get NULL. `cache_project_data` stores `bench_data.get("label") or None`; `set_bench_label(project, path, label)` updates one bench by path without rewriting the whole instance.
- **Marker I/O is container-side.** `write_label_marker`/`read_label_marker`/`clear_label_marker` go through `container.exec_run` with LIST-form commands (`["cat", ...]`, `["rm","-f", ...]`, `["sh","-c", script]`). The write base64-encodes the JSON on the host and `base64 -d`s it in the container, so no label content is ever interpolated into a shell command. Reads fail SAFE (missing/garbage marker -> `None`, never an error). Tests use `tests/bench_fakes.py:MarkerFakeContainer`, which really base64-decodes the write script into an in-memory fs.

#### `--bench` selector and the shared resolver
- `resolve_bench_path(project, bench_selector, path_override, *, on_ambiguous="error")` is the single replacement for the old copy-pasted "use `--path` if given, else `bench_instances[0]`, else `/workspace/frappe-bench`" logic (which silently guessed in multi-bench instances). Precedence: `--path` wins (escape hatch; `--bench`+`--path` together is an error); else `--bench` resolves against the cache; else the DEFAULT bench: single-bench -> that bench; multi-bench -> **error listing the benches** (`on_ambiguous="error"`, the data-op default) OR first-bench-plus-note (`on_ambiguous="first"`); no cache at all -> returns `None` so the caller keeps its inspect/default fallback.
- Commands with `--bench`: `run`, `backup`, `update`, `open`, `unlock`, `restore` (all use `on_ambiguous="error"` - a multi-bench op with no selector errors instead of guessing), and `start` (uses `on_ambiguous="first"` per captain decision: `cwcli start` KEEPS WORKING on multi-bench by starting `bench start` in the first sorted bench and printing a note; `--bench` picks another). `run`'s `--path` default is `None` so the resolver runs. `start` recovers `-y/--yes`, `-v`, and `--bench <val>` from its variadic project-name argument (the arg greedily eats trailing options), same trick as `rm`'s `_recover_trailing_flags`.
- **Setting labels:** `cwcli label <project> [<selector> [<new-label>]] [--clear]` (`commands/label.py`). Bare `cwcli label <project>` lists benches (read-only, no container). Setting/clearing writes BOTH stores and REQUIRES a running frappe container (the marker lives inside the bench; it does NOT auto-start - a label change should not spin up a stopped instance). BOTH the set and the clear paths write the MARKER FIRST and only touch the DB on marker success: because the marker is the source of truth for recovery, clearing the DB while the marker survives would let a later full inspect resurrect the just-cleared label, and writing the DB while the marker write failed would drop the durable handle on the next cache wipe. A marker failure prints an error and exits non-zero, leaving the DB unchanged. `inspect -i` was rebuilt on the same model: it prompts a validated user label per bench, writes the marker + persists the DB (was the old ephemeral `bench["alias"]`, which was set in memory and then dropped by `cache_project_data` - it never actually persisted).

#### `--yes`/`-y` contract
- Shared `commands/utils.py:confirm_or_exit(prompt, *, assume_yes, refuse_message)` mirrors the restore/rm pattern: `--yes` proceeds; a non-TTY WITHOUT `--yes` on a destructive op refuses and exits non-zero; an interactive decline exits non-zero.
- `commands/utils.py:ensure_containers_running` mirrors that same contract on its interactive not-running branch: on a **non-TTY without `auto_start`** it refuses (prints a message naming `--yes`, exits 1) instead of hanging on questionary or crashing on EOF, and an **interactive decline or Ctrl-C** exits 1 (was Exit(0)). This one root fix cures the inherited non-TTY defect across every caller (`run`/`backup`/`update`/`open`/`unlock`/`logs`/`inspect`). The `prompt=False` path is unchanged and load-bearing: it still returns `False` silently (no TTY check, no prompt, no Exit) for inspect Tier-2 and the `rm` recache path.
- **Destructive confirm gaining `--yes`:** `config cache clear --all` (uses `confirm_or_exit`).
- **Auto-start commands gaining `--yes`:** `run`, `backup`, `update`, `open`, `unlock`, `logs`, `inspect` thread `--yes` into `ensure_containers_running(auto_start=yes)`, so a stopped instance auto-starts without the "start the containers?" prompt (non-destructive; enables non-interactive driving). For `inspect` this auto-start is scoped to the **Tier 3 full-inspect path** (`--update` / cache-miss / drift-escalation), which persists fresh data; the **Tier 2** cache-hit freshness pass stays passive and calls `ensure_containers_running(prompt=False, auto_start=False)` even under `--yes`, so a plain cache-hit `inspect` never starts a stopped instance (preserving the T2 READ-ONLY contract above).
- **`start --yes`:** auto-confirms stopping conflicting Frappe instances to free their ports (`_check_port_conflicts(assume_yes=...)`). That port-conflict confirm also guards `sys.stdin.isatty()` before prompting: a non-TTY without `--yes` refuses (exit 1) rather than hanging, and Ctrl-C exits 1.
- **Honest exit codes (issue #39):** `start`/`stop`/`restart` exit 1 on a nonexistent instance (never printing "started"/"stopped" for it); multi-instance loops process every name then exit 1 if any failed (the `rm` failures-collector pattern - `_start_project` raises Exit(1) on not-found, `_stop_project`/`_restart_project` signal not-found with a `None` return distinct from a legitimate zero count). `config`'s error paths (`Error:` prints, invalid interval < 60, "not enabled" for `start`/`restart`, no-target `cache clear`, and every `except Exception`) `raise typer.Exit(1)`; the deliberate carve-out is **idempotent success** (exit 0) for already-in-the-requested-state no-ops: `stop`/`restart`/`start`/`install-startup`/`uninstall-startup` "already stopped/running/installed". `update` folds a maintenance-mode-disable failure into `has_errors` and names the stuck site (`bench --site X set-maintenance-mode off`). `open`'s editor-select refuses (exit 1) on a non-TTY with multiple editors and no `--code/--code-insiders/--cursor/--docker` flag, and an interactive cancel exits 1.
- `status` issues no confirmation and needs no auto-start, so it has no `--yes`. `restore`/`rm` already had `--yes` (unchanged).

Regression coverage: `tests/test_bench_labels.py` (validation/resolution/marker I/O), `tests/test_bench_selector.py` (resolver cases incl. ambiguous/not-found), `tests/test_bench_label_db_and_command.py` (migration, `set_bench_label`, `label` command), `tests/test_inspect_label_recovery.py` (DB-loss recovery from markers via full inspect), `tests/test_yes_flag.py` (`confirm_or_exit`, `config cache clear --all`, `start`, auto-start).

### Cache never stores secrets

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

### `rm` command: what removal actually deletes

`docker-py`'s `Container.remove(v=True)` only removes a container's *anonymous* volumes. The named compose volumes that frappe-docker creates (e.g. `sites`, `db-data`) are NOT touched by it, so they must be removed explicitly or the databases/sites survive a "delete" (this was the issue #19 / "rm lies" bug).

In `src/caffeinated_whale_cli/commands/rm.py`:

- `_remove_named_volumes` enumerates volumes via `get_project_volumes` (label `com.docker.compose.project={name}` in `docker_utils.py`) and calls `volume.remove(force=True)`. It runs only when `remove_volumes` is true (`--volumes`, the default); `--no-volumes` leaves named volumes untouched. When `get_project_volumes` returns `None` (a Docker error, not an empty list) it warns instead of silently reporting 0 removed.
- `_delete_project_directory` deletes the local cwcli instance dir `~/.cwcli/projects/{name}/` (`PROJECTS_DIR / name`). This runs regardless of `--no-volumes` because the directory is config, not data; leaving it behind was the lingering-instance half of issue #19.
- Both run after the existing backup/archive safety step inside `_remove_project`, gated on `_archive_project_directory` succeeding. Keep the confirmation text in `rm()` in sync with what is actually deleted.

#### Archive only `conf/`, NOT the whole project dir (sharp edge)

`~/.cwcli/projects/{name}/` is the frappe-docker devcontainer's bind mount (mounted as `/workspace`), so it is NOT just config - it is the whole multi-hundred-MB `frappe-bench` (venv, `node_modules`, `sites`). That tree contains dangling symlinks (e.g. `frappe-bench/env/bin/python*` -> the container's python, `sites/assets/frappe`) that do not resolve on the host. `_archive_project_directory` therefore archives ONLY the small reliable `conf/` subdir (the generated `docker-compose.yml`) into `archive_dir/project_files/conf/`, with `copytree(symlinks=True, ignore_dangling_symlinks=True)`. An earlier version copytree'd the whole dir; `copytree` (default `symlinks=False`) follows and raises on the dangling symlinks, the archive-before-delete gate then skipped the volume + dir deletion, and #19 came back in real conditions while the banner still promised deletion. The data safety net is the live `bench backup --with-files` output, which `_backup_sites` writes into the SAME timestamped `archive_dir/backups/`; `conf/` is only the config safety net. `rmtree` (which does not follow symlinks) deletes the full bench fine.

- `_recover_trailing_flags` makes flag order forgiving: a variadic `typer.Argument` greedily eats options that trail it, so `cwcli rm myproj --yes` would otherwise treat `--yes` as a second project name. It pulls `-v/--verbose`, `-y/--yes`, `--no-backup`, `--volumes/--no-volumes` back out of the name list.
- Orphan path: when `get_project_containers` returns an empty list (containers already gone, distinct from a `None` Docker error) but a named volume or the project dir still exists, `rm` still archives `conf/`, removes the volume + dir, and warns that no live DB backup could be taken. This resolves already-orphaned instances (the literal #19 report).

#### Recache must never prompt under the spinner (stopped-instance deadlock)

The pre-removal recache runs inside a Rich `console.status("Re-caching project '{project}'...")` spinner.
The recache chain is `rm()` -> `cache.recache_project()` -> `commands/inspect.py:inspect()` -> `commands/utils.py:ensure_containers_running(require_running=True)`.
When the frappe container is NOT running, `ensure_containers_running` used to call `questionary.confirm("Would you like to start the containers...?")`.
Issuing an interactive prompt while a `console.status` spinner owns the terminal paints the prompt over and starves it of input -> `cwcli rm <stopped-instance>` hangs forever on the spinner.
A stopped instance is a normal rm case (a live `bench backup` is impossible, so rm warns and still archives `conf/`, removes named volumes, and deletes the dir), so this must degrade, not block.

The fix has two layers; keep both:

- `ensure_containers_running(..., prompt: bool = True)` (`commands/utils.py`): with `prompt=False` it NEVER prompts - when the containers are not running (and `auto_start` is False) it returns `False` instead of asking. `inspect` forwards this via a hidden `--prompt-start/--no-prompt-start` option (`prompt_to_start`, default True) and, when `ensure_containers_running` returns False, prints a clear error and `raise typer.Exit(1)` (a stopped bench can't be inspected: every probe is an `exec_run`). `cache.recache_project` calls `inspect(..., prompt_to_start=False)`, so the recache is ALWAYS non-interactive and degrades to a `False` return. Standalone `cwcli inspect` keeps `prompt_to_start=True`, so it still prompts interactively as before (this is independent of `--interactive`, which only governs per-bench label naming).
- `_frappe_container_running(project)` (`commands/rm.py`): `rm()` checks this BEFORE entering the recache spinner and SKIPS the recache for a stopped instance (printing "Containers for '{project}' are not running; skipping recache"). The recache only refreshes site info for a live backup, which is moot when nothing runs - and rm must NOT auto-start containers it is about to delete. This keeps the spinner off the stopped path entirely; the `ensure_containers_running` layer is defense-in-depth for any other non-interactive caller.

Regression coverage is in `tests/test_rm_stopped.py` (asserts no `questionary.confirm` is reachable from the recache path and that `rm` skips recache for a stopped instance).

#### Data-safety gates: backup gate, name validation, honest exit codes

Three fail-open data-safety gaps were closed in `commands/rm.py` (audit findings C1/H5/M11). Regression coverage is in `tests/test_rm_safety.py`, which drives the previously-untested backup path with a `FakeFrappeContainer` (records every `exec_run` and serves each backup artifact through a `get_archive` that streams a single-member tar in small chunks - the real copy primitive); every pre-existing rm test passed `no_backup=True`, so the backup gate had zero coverage. The streamed copy itself is proved by `TestStreamedCopy` (chunked consumption, exact reassembly, and truncated-stream/missing-file fail-closed) plus a `_backup_sites`-level assertion that the copy goes through `get_archive`, never a whole-file `cat`.

- **C1 - a failed/unverified backup must NOT allow volume/DB deletion.** `_backup_sites`' return value is captured into `result["backup_ok"]` and gates deletion. Critically, `_backup_sites`' success signal was made trustworthy: a zero `bench backup` exit only means the dump was written INSIDE the container (the very volume about to be deleted), so each artifact is copied out of the container and VERIFIED on the host - the database dump (`_DB_DUMP_MARKER = "database.sql"`, matched case-insensitively) must be present and non-empty, and ANY copy-out failure fails the site closed (a backup missing a part is not trusted). The copy/verify set is scoped to the CURRENT backup run only - the artifacts sharing the newest `ls -1t` file's leading `<YYYYMMDD_HHMMSS>` timestamp token (`f.split("-", 1)[0]`), NOT a fixed top-N window. A fixed window (the former `backup_files[:5]`) bleeds into prior runs once a site has been backed up more than once, so a stale, unrelated artifact whose copy-out fails would falsely fail an otherwise complete fresh backup (and a stale non-empty dump could mask a fresh empty one); scoping by timestamp prefix keeps the strict fail-closed guarantee while verifying exactly this run's files. Non-DB artifacts (e.g. a files tar) may legitimately be small, so only the DB dump is size-checked. `_backup_sites` returns True only if EVERY site fully backed up (no more `backed_up_count > 0` partial-success). The copy mechanism STREAMS each artifact out of the container with `get_archive` via the `_stream_container_file` helper - a `_ChunkStreamReader` (an `io.RawIOBase`) feeds `get_archive`'s tar chunks to `tarfile` in streaming mode (`mode="r|"`) and the single member is written to the host file in fixed-size chunks - NOT the former whole-file `cat` + `write_bytes`, which materialised the entire artifact in host RAM as one bytes blob and spiked memory on exactly the multi-GB `bench backup --with-files` backups that matter most. It stays fully fail-closed: `get_archive` raising (missing/unreadable path), a tar/read error, or a truncated stream (bytes written != the tar member's declared size) all return False and fail the site closed, and the load-bearing host-artifact verification (DB dump present and non-empty) is unchanged. `--no-backup` opts out of the whole gate (no backup attempted, `backup_ok` stays True) - it is the documented escape hatch to force removal without a backup.
  - **The backup gate is an EARLY abort, before any container is removed.** `backup_failed = remove_volumes and not no_backup and not result["backup_ok"]` is evaluated right after the backup/archive-config block and BEFORE the stop-and-remove-each-container loop; when true, `_remove_project` prints the refusal + retry hint, appends a failure, and `return`s immediately - no container is stopped or removed, no volume/dir is touched, and (because `failures` is non-empty) the cache is preserved. This closes the retry-orphan data-loss path: if the containers were torn down first and the gate blocked only afterward, a naive retry (`cwcli rm proj` again) would find no running container, attempt no backup (so `backup_ok` defaults True), pass the gate, and delete the DB volumes with NO backup - defeating C1. Aborting before container removal leaves the whole instance (containers + volumes + dir + cache) intact and retryable, so a retry can still take a live backup from the still-running frappe container. The stopped/orphan path is unchanged: no container is running, so no backup is attempted, `backup_ok` stays True, and the early abort never fires there (a stopped DB cannot be dumped live - a previously-accepted tradeoff).
  - **The backup gate is scoped to volume deletion.** `backup_failed = remove_volumes and not no_backup and not result["backup_ok"]`. Under `--no-volumes` no volume data is destroyed, so a failed live backup does NOT abort (backup_failed is False) and does NOT force a non-zero exit - blocking it there would be a false failure. The default `--volumes` path stays fully fail-closed. The LATE gate's `container_removal_failed` and `archive_failed` conditions still apply regardless of `--no-volumes`.
  - **Site detection is fail-safe (`_list_sites` -> `bench_sites.list_sites`), not a denylist.** A real Frappe site is a directory containing `site_config.json`; each entry from `ls sites` is classified by a single `sh -c` probe into SITE / NOTASITE / AMBIGUOUS. An entry is EXCLUDED only on a positive NOTASITE (a non-directory, or a readable dir with no config); SITE, AMBIGUOUS (unreadable dir / probe error), unexpected output, or a non-zero probe exit all fail closed -> treated as a real site that must be backed up. This fixes the earlier fragile denylist that misclassified stray files like `currentsite.txt` (written by `bench use`) as sites and made the default `rm` wrongly refuse to delete a normal bench, while keeping C1 fail-closed under ambiguity (worst case blocks a delete, recoverable via `--no-backup`; never deletes a site's volume with no backup). `_backup_sites` and `_archive_project_config` both use `_list_sites`.
  - **Cache-clear is keyed on `not result["failures"]`, never on gate-not-blocked alone.** Containers are removed BEFORE the LATE (container/archive) gate, so `removed_count > 0` is true even when that gate blocks or a *post-gate* volume/dir/enumeration removal fails; the EARLY backup abort instead returns before removing any container (so `removed_count` stays 0), but it too records a failure. Clearing the cache in any of these cases would make a half-removed (or untouched-but-blocked) instance vanish from `cwcli ls`/`inspect` while its data still occupies disk. Gating the `clear_cache_for_project` call on an empty `failures` list covers the early-abort, late-gate-blocked, AND post-gate failures uniformly, while still clearing on a clean full/orphan/`--no-volumes` removal.
- **H5 - project-name validation before any `rmtree`/volume op.** `_is_valid_project_name` (rejects empty/`.`/`..`/absolute/separator/NUL names) and `_is_safe_project_dir` (asserts `(PROJECTS_DIR / name).resolve()` is strictly inside `PROJECTS_DIR.resolve()`, which also catches a symlink-escape) guard three layers: the CLI filters names up front (mirroring the existing empty-name filter), `_remove_project` re-checks at entry (defense-in-depth for any internal caller), and `_delete_project_directory`/`_archive_project_directory` hard-guard right before the `rmtree`/copytree. Without this, `cwcli rm ..` resolved `PROJECTS_DIR / ".."` to `~/.cwcli` and `rmtree`'d every project + cache + config.
- **M11 - honest exit codes on partial failure.** `_remove_project`'s result dict has `failures: list[str]`; volume-removal, dir-removal, container-removal, backup, invalid-name, and Docker-connection failures all append to it. `_remove_named_volumes` and `_delete_project_directory` take an optional `failures` collector (their int/bool returns are unchanged, so the existing `test_rm_truth.py` unit tests stay green). `rm()` raises `typer.Exit(1)` whenever any instance reported failures (or a name was rejected) and no longer prints the green `✓ Successfully removed` on a partial failure. Two sharp edges: `container_name` is bound to `"<unknown>"` BEFORE reading `container.name` (a docker-py property that can itself raise, which previously left `container_name` unbound → NameError in the except handler); and a caught container-removal error sets `container_removal_failed`, which is part of the same LATE gate as archive, so it does NOT fall through to destroy the volumes/dir.

`_remove_project`'s data-destruction protection is split into two gates. The backup gate is an EARLY abort (`backup_failed = remove_volumes and not no_backup and not result["backup_ok"]`) evaluated BEFORE the container-removal loop; it `return`s without touching anything so a failed backup never tears down the containers a retry needs. The LATE gate is a combined check (`container_removal_failed or archive_failed`) placed AFTER container removal, exactly where the `conf/` archive gate already sat - it protects the irreversible data (named volumes + local dir); removing containers first is intentional and non-destructive (containers are recreatable, the named volumes hold the data). When calling `_remove_project`/`rm()` directly in tests, note it is `@handle_docker_errors`-decorated (patch `docker_utils.shutil.which` + `docker.from_env`) and, like other Typer commands here, real callers pass every param explicitly.

#### Multi-bench `rm`: back up EVERY bench, not just the first (2026-07-10 fix)

`_remove_project` used to back up a single `bench_path = cached_data["bench_instances"][0]["path"]` (the first sorted bench) while `_remove_named_volumes` deletes ALL of the project's named volumes regardless of bench count - so on a multi-bench instance, every non-first bench's sites were destroyed with no backup, and `result["backup_ok"]` reflected only bench 0 (defeating the C1 gate for the other benches).

The fix loops over `bench_paths = [b["path"] for b in cached_data["bench_instances"]]` (falling back to the single default `/workspace/frappe-bench` when there is no cache), calling `_backup_sites` and `_archive_project_config` once per bench. `result["backup_ok"]` is now `all(...)` across every bench - a single bench's failed backup blocks volume deletion for the whole instance, same as the single-bench case. This closes the gap without changing the gate's shape: the EARLY-abort-before-container-removal and scoped-to-`--volumes` behavior above are unchanged, they just now cover every bench's sites instead of only the first.

- **Per-bench archive namespacing.** Two benches in one instance can share a site name (e.g. both use the frappe-docker default `development.localhost`), so their backup/config artifacts must not land in the same archive subdirectory. `_bench_archive_slug(bench_path)` hashes the full bench path (`sha256` first 8 hex chars) appended to a sanitized basename, giving a short, deterministic, collision-safe slug. This nesting is applied ONLY when `len(bench_paths) > 1` (`multi_bench`); a single-bench instance keeps the original flat `archive_dir` layout so a human can still find `archive/{project}_{timestamp}/backups/` directly without knowing the slug.
- **Cache-read failure escalates to live discovery, not a blind default.** If `db_utils.get_cached_project_data` raises, `_remove_project` does not just fall back to the single default `/workspace/frappe-bench` - it first tries `inspect._find_bench_instances(frappe_container, verbose)` (the same `find`-based discovery the full T3 inspect uses) so a genuinely multi-bench instance still gets every bench backed up even with a corrupt/missing cache. Only when live discovery also fails or returns nothing does it fall back to the single default path. Exactly one consolidated warning is printed either way (naming which of "discovered N bench(es) live" or "falling back to default bench path" happened) - not one warning per failed step.
- **Per-bench config-archive failure is a warning, not a gate.** `_archive_project_config` takes an `archive_dir_override` so its output lands in the same per-bench namespace as the DB backup, and it no longer prints its own warning on failure (that would double up with the caller's). The per-bench loop in `_remove_project` prints the warning itself, exactly once per failed bench, and does NOT append to `result["failures"]` - unlike backup/volume/container failures, a failed config archive does not block volume/directory deletion or cache clearing, because the verified live database backup (the C1 gate) already protects the data that matters; the config snapshot is a convenience extra.

Regression coverage: `tests/test_rm_safety.py::TestMultiBench` (two-benches-both-ok, one-fails-blocks-everything, single-bench-stays-a-no-op-regression, cache-failure-fallback-to-default-bench, and cache-failure-with-live-discovery-backs-up-every-bench) and `TestConfigArchiveWarning` (a failed config archive does not block deletion/cache-clear and warns exactly once), using the multi-bench fake container in `tests/bench_fakes_mb.py`.

### `restore` command: receive-mode data-safety semantics

`restore --receive` (`restore_receive_mode` in `commands/restore.py`) downloads a peer's backup over sendme and then runs `bench restore --force`, which drops and recreates the live default site's database.
That is the most destructive path in the codebase, and it used to run the instant the download finished with no "are you sure" gate (the only interactive prompt was the *conditional* missing-apps confirm, which fires only when apps are missing).

The receive path carries the same destructive-restore gate the normal path has, placed right before the `bench ... restore ... --force` command is built:

- It prints the `⚠ This will replace all data in site '{site}'` warning + the backup filename, then compares the backup's origin site (parsed from the database filename via `parse_backup_filename(...)["site_name"]`, which is already dots-to-underscores) against `transform_site_name_to_backup_format(site)` and prints an `⚠ Origin mismatch` line when they differ.
  A failed parse yields `origin_parsed is None`, so the mismatch check is skipped (fail-safe, no false alarm); this is the same parser that must already have matched to set `database_file`, so a hyphen-bearing site that the parser can't represent would have errored earlier.
- The confirmation honors the `--yes/-y` flag on `restore`: `--yes` proceeds without prompting, an interactive TTY asks `questionary.confirm("Are you sure you want to restore?")`, and a **non-TTY without `--yes` refuses and exits non-zero** rather than silently proceeding or exiting 0.
  This confirm exits **non-zero on any refusal** (declined confirm, non-TTY-no-yes, or Ctrl-C), as does the normal path (issue #40 closed the prior exit-0-on-cancel finding).
- `--yes` is threaded through both `restore_receive_mode(...)` call sites and BOTH confirm blocks on the normal path (the missing-apps "continue anyway?" and the destructive "are you sure?"). It bypasses every confirmation prompt on both paths; it does NOT remove the sendme-ticket prompt (receive path) or the `--mariadb-root-password` / `--mariadb-root-username` credential prompts, which already meet the standard via `_prompt_mariadb_credentials`.

Two supporting fixes travel with it, and BOTH the receive path and the normal restore path share the same shapes:

- **Secrets off the argv (M5):** the MariaDB root password (and admin password) are no longer interpolated into the command string. They are put in a `restore_env` dict and referenced as `"$CWCLI_MARIADB_ROOT_PASSWORD"` / `"$CWCLI_ADMIN_PASSWORD"`, and the command is executed as `frappe_container.exec_run(["sh", "-c", cmd], workdir=..., environment=restore_env)`.
  Passing the list form `["sh", "-c", cmd]` (rather than a bare string, which docker-py would `shlex.split` and exec directly) is REQUIRED so the shell actually expands the `$VAR`; the secret then never appears in the command the container process list shows.
  cwcli's own verbose masking (`cmd.replace(password, "***")`) only hid it from cwcli output, not from `ps`/`docker top`/exec-inspect - that masking is gone because the secret is not in `cmd` at all.
- **Streamed tar copy (M4):** the file-copy loop passes the open tar file handle straight to `frappe_container.put_archive(backup_dir, tar_file)` (docker-py streams it) instead of `tar_file.read()` (which slurped a multi-GB `--with-files` backup into host RAM), and it checks `put_archive`'s bool return and `raise typer.Exit(1)` on failure instead of ignoring it and surfacing a confusing "backup not found" later.

Regression coverage is in `tests/test_restore_safety.py`: it drives `restore_receive_mode` with a `FakeReceiveContainer` that records every `exec_run` (command, workdir, environment) and asserts a declined/non-TTY confirm does NOT run `bench restore --force` and exits non-zero, `--yes` proceeds, an origin mismatch is surfaced, and the DB password rides in `environment=` (never in the recorded argv).
Testing note: `restore_receive_mode` is a plain function (not the Typer command), so tests call it directly with all args explicit; stub `TipSpinner` to a no-op (it starts a Rich spinner even with `enabled=False`) and fake `subprocess.run` to write the "downloaded" backup into its `cwd`.

### `restore` command (normal path): non-interactive selectors + honest exit codes (issue #40)

The normal (non `--send`/`--receive`) `cwcli restore` path is fully drivable by an agent or script. Every prompt has a flag, and a non-TTY without the needed flag refuses with a NON-ZERO exit instead of silently exiting 0 (the prior "exit-0-on-cancel" finding is closed).

- **Backup selection.** `--latest` non-interactively selects the newest backup set for the target site; `--backup-file <filename-or-full-path>` selects one by its database file. The selector bypasses the `questionary.select` menu. `--latest` does NOT fall through to other-site backups (that would silently restore a different site's data); `--backup-file` searches both target and other backups (the caller named a specific file). The two flags are mutually exclusive, and neither applies to `--send`/`--receive` (validated up front). The resolution happens in `select_backup_set(target_backups, other_backups, *, latest, backup_file)` - a pure helper near `display_backup_selection_menu` - so it is unit-testable with no TTY or container.
- **Non-TTY guard.** A non-TTY with neither selector exits 1 (message names `--latest`/`--backup-file`) rather than reaching `questionary.select().ask() -> None` on a non-TTY and exiting `0` having done nothing.
- **`--yes` widened to the normal path.** Both normal-path `questionary.confirm` prompts honor `--yes`: `--yes` proceeds without prompting, a non-TTY without `--yes` refuses with exit 1, an interactive TTY asks. A declined confirm or Ctrl-C exits 1 (was `Exit(0)`) on BOTH paths.
- **The sentinel** `{"_restore_from_ticket": True}` (remote restore via sendme, reached from the menu) stays INSIDE the interactive branch only; the selector branch can never produce it (correct: remote restore is reached via `--receive`).
- Both confirms pass `auto_enter=False` (load-bearing - they must consume their own trailing Enter so the keystroke does not leak into the following credential prompt). `commands/utils.py:confirm_or_exit` omits that setting, so it is NOT used here; the local prompt code is kept.
- Credentials fire AFTER the confirms via `_prompt_mariadb_credentials` (unchanged, already meets the standard); do not reorder.

Regression coverage is in `tests/test_restore_safety.py` (`TestSelectBackupSet` for the pure helper, `TestNormalPathSelectorsAndExitCodes` for the Typer command). The Typer-command tests call `restore_mod.restore(...)` with ALL params explicit (omit any `Option` and its truthy default object leaks through - the same trap flagged for `inspect`); the `@handle_docker_errors` decorator is bypassed by patching `docker_utils`'s `shutil`/`docker` (the decorator reads them from its own namespace; `restore_mod` does not import them directly).
The authoritative repros and the real-instance E2E evidence (both selector happy paths, the non-interactive refusals, the interactive menu decline, and the mutual-exclusion guards) are in `docs/e2e/restore-noninteractive-h2.md`.

### `restore` + `inspect`: currentsite, default site, receive path, credential prompts, missing-apps, migrate+restart

Six bugs the captain hit on a live 0.33.0 session (Frappe `version-14`, site `development.localhost`) were fixed together.
The authoritative repros and the real-instance E2E evidence are in `docs/e2e/restore-inspect-e2e-r6.md`.

#### Site detection is shared (`utils/bench_sites.py`)

`bench_sites.list_sites(container, bench_path)` is the single canonical "what are the real sites" implementation, used by BOTH `commands/rm.py:_list_sites` (which delegates) and `commands/inspect.py:_get_sites`.
A real Frappe site is a DIRECTORY containing `site_config.json`; detection probes for that per entry rather than denylisting known non-site names.
It is fail-safe: an entry is excluded only on a positive NOTASITE (a non-directory, or a readable dir with no `site_config.json`); anything ambiguous (probe error, unreadable dir) is treated as a site.
This replaced `inspect._get_sites`' old denylist, which did NOT list `currentsite.txt` (a plain file written by `bench use`), so inspect reported it as a site and then errored `bench --site currentsite.txt list-apps -> "Site currentsite.txt does not exist!"`.
`bench_sites.read_current_site(container, bench_path)` reads `sites/currentsite.txt` (the default-site pointer), failing safe to `None`.
The probe string in `list_sites` is byte-identical to the one rm's tests already assert, so rm's fakes stay green; `tests/test_inspect_partial_refresh.py`'s fake gained the same SITE/NOTASITE probe handling.

#### Default site resolves from EITHER source (`currentsite.txt` OR `common_site_config.json`)

A bench records its default site in `common_site_config.json`'s `default_site` OR `sites/currentsite.txt`; a plain `bench use`d dev bench only has the latter (the captain's `ners` had no `default_site` key at all).
`Bench.current_site` is a nullable column (idempotent `_migrate_bench_current_site_column`, mirroring the `label` migration) populated by a full inspect from `currentsite.txt`, carried forward by the T2 partial pass, and surfaced by `get_cached_project_data`.
`db_utils.get_default_site` returns `common_site_config.default_site` if set, else the cached `current_site` (via `get_current_site`) - so `restore`/`backup`/`unlock` all resolve the default even with no `default_site` key.
`inspect`'s tree marks `(default)` from either source too.
`restore.py:_resolve_default_site` adds a LIVE `currentsite.txt` read as a final fallback for the destructive restore path (a cold/stale cache still resolves correctly); it is used by both the normal and receive paths in place of the bare `get_default_site`.

#### `--receive` restore: full container path, not the bare filename

Receive-mode built `bench restore <bare-filename>` with `workdir=backup_dir`; on Frappe `version-14` bench rejects that with `Invalid path <db filename>` (data restore broken).
The fix passes the FULL container path `{backup_dir}/{database_file.name}` and reorders the args to exactly match the normal path (db path, credentials, `--force`, then `--with-public-files`/`--with-private-files`).
On Frappe `version-15` bench has a "trying alternative directories" fallback that masks the bare-filename bug, so this only reproduces on v14 - which is why the E2E built a v14 bench (`docs/e2e/`).

#### Credential prompting works in BOTH modes (`_prompt_mariadb_credentials`)

Shared by the normal and receive paths.
Interactive (a TTY): prompt for the username (default `root`, blank keeps `root`) AND the password (blank is an error).
Non-interactive (flags / non-TTY): the username defaults to `root`; the password is a required secret, so a non-TTY WITHOUT `--mariadb-root-password` refuses with a non-zero exit rather than hanging or proceeding empty.

The interactive "empty password" bug and its ROOT fix (sharp edge, worth remembering for ANY questionary confirm followed by another prompt):
`questionary.confirm(...)` with the default `auto_enter=True` submits on the `y`/`n` keypress and leaves the user's habitual trailing Enter in prompt_toolkit's INTERNAL input buffer.
That stray Enter is then read by the immediately-following prompt as an empty submit, so the password prompt returned empty (`Error: Password cannot be empty.`).
A `termios.tcflush` stdin drain was tried and proven INEFFECTIVE: `FIONREAD` shows the OS tty queue is empty (the Enter is inside prompt_toolkit, not the OS queue), so a tcflush reaches nothing - it was removed.
The fix is `auto_enter=False` on EVERY restore confirm (both the destructive `Are you sure you want to restore?` and the missing-apps `continue anyway?`, on both paths): with `auto_enter=False` the confirm REQUIRES Enter to submit and thus CONSUMES its own trailing Enter, so nothing leaks into the next prompt - robust regardless of which credential flags are set.
(An earlier partial mechanism - the username prompt absorbing the stray Enter - only worked in the default flow where the username prompt runs; it silently failed for `--mariadb-root-username <flag>` + habitual `y`+Enter, which is why a realistic pty E2E must press `y` THEN Enter.)

#### Missing-apps warning reads the BACKUP's apps (`check_missing_apps` + `_read_backup_installed_apps`)

The warning regressed to silence because `check_missing_apps` read `sites/{site}/apps.json`, which Frappe does NOT write per site (it lives at the bench level, `sites/apps.json`), so the `cat` always failed and it returned `[]`.
The correct question is "does the BACKUP need apps this bench lacks" (the captain's words), not "what does the about-to-be-overwritten site have" - and a site whose app code is already missing cannot even be listed by `bench list-apps` (the import crashes).
`_read_backup_installed_apps` `zcat -f`s the backup's DB dump and greps the `installed_apps` global (`...,'["frappe","widgets"]','installed_apps',...` - exactly what `frappe.get_installed_apps()` reads), extracting the app-name tokens.
`check_missing_apps(frappe_container, project_name, bench_path, backup_db_path, ...)` (the `site` arg became `backup_db_path`) compares those against the bench's apps read LIVE from `ls {bench_path}/apps`, and returns `backup_apps - available_apps`.
It fails safe: an unreadable dump / absent marker yields `None` -> no warning (never a false positive).
Both call sites pass the backup's container path (`selected_backup["database"]["full_path"]` on the normal path; `{backup_dir}/{database_file.name}` on the receive path).
Because both sides are read LIVE (the backup's dump and `ls apps`), the check no longer touches cwcli's cache, so `check_missing_apps` dropped its `cache.recache_project` call and `--no-recache` became a DEPRECATED no-op - the flag (and its `no_recache` param) is kept only for backward compatibility with existing callers.

#### Post-restore: migrate then restart (`_post_restore_migrate_and_restart`)

After a successful `bench restore`, both paths run `bench --site <site> migrate` then restart the instance (via `_start_project`, which kills the old `bench start` and relaunches it - the same app restart `cwcli restart` does).
The restart targets the SAME bench that was just restored via `_start_project`'s `bench_path_override` (used verbatim, bypassing the `resolve_bench_path` guessing), so a multi-bench restore into a non-first bench restarts the right dev server, never bench 0.
A failed migrate does NOT undo the restore; it is surfaced and the restart still runs, but the function returns False so the caller exits non-zero.
A Docker/API exception raised by the migrate `exec_run` is treated as a failed-but-reported migrate (`exit_code=1`) that STILL continues to the restart, so an exec error can never skip bringing the instance back up.
`--no-migrate` skips the whole post-restore step.

Regression coverage: `tests/test_restore_inspect_fixes.py` (all six) and `tests/test_bench_labels`-style DB tests; `tests/test_inspect_partial_refresh.py` and `tests/test_restore_safety.py` fakes were updated for the shared site probe and the `no_migrate` param.

### `apps` command group: multi-site fan-out, JSON purity, update deprecation (2026-07-11)

`commands/apps.py` is the first-class app manager (`list`/`install`/`uninstall`/`update`), registered as a Typer sub-app in `main.py` (`add_typer(apps_cmd.app, name="apps")`). It is built entirely from existing primitives - `resolve_bench_path` (`on_ambiguous="error"`, so a multi-bench op with no `--bench` refuses), `confirm_or_exit`, `ensure_containers_running(auto_start=yes)`, `bench_sites.list_sites`, and `cache.recache_project` - and adds no new dependency or cache field. It was spec-driven via OpenSpec: the artifacts live in `openspec/changes/add-app-management/` (proposal/design/specs/tasks), validated with `OPENSPEC_TELEMETRY=0 openspec validate add-app-management --strict`.

Load-bearing decisions, each guarding a real constraint:

- **Multi-site by DEFAULT for install/uninstall/update.** No `--site` = fan out to ALL sites from the canonical `bench_sites.list_sites` (NOT `get_default_site` - that single-site model was rejected by the captain); `--site` is repeatable and narrows. There is deliberately NO active/disabled-site distinction - one site set, the same `list_sites` everything else uses. The fan-out is **continue-and-report-all**: run every `(app, site)` step, collect a per-step `{app, site, action, ok}` result, and exit non-zero if ANY failed - never a success banner over a partial failure (`_report_and_exit`). Stop-on-first-failure was explicitly rejected.
- **`--json` stdout purity.** `console` is stdout (see `utils/console.py`), so in JSON mode NOTHING but the final `json.dumps` may touch stdout. `_run_bench` therefore CAPTURES bench output (not streams) when `json_output`, all progress goes to `stderr_console`, and a destructive `apps uninstall --json` without `--yes` REFUSES (json implies non-interactive; a `confirm_or_exit` prompt would fight the JSON contract, and its `--yes` ack prints to stdout). Human (non-json) mode streams bench output to stdout via `_stream_bench` (mirrors `run.py`).
- **Post-mutation refresh uses `cache.recache_project`, NOT `partial_inspect_known_benches`.** The partial pass is READ-ONLY (never persists) and cannot refresh per-site `installed_apps` - exactly what install/uninstall change. Only a full recache (which routes through `cache_project_data` -> `_redact_config_for_cache`, so no secret is ever written) makes `where`/`open`/`inspect` honest. Degrades to a warning; the mutation already succeeded.
- **`<app>` is a name OR a git URL** passed straight to `bench get-app`; the per-site `install-app` name is derived from the actual `apps/` before/after diff (the real dir `bench get-app` created), NOT parsed from the target string - `_derive_app_name`'s URL-basename-minus-`.git` heuristic is only a FALLBACK for when that diff is ambiguous (zero or more than one new `apps/` entry, e.g. the app was already present). `uninstall-app` is always invoked with bench's own `--yes` so a non-TTY exec never hangs on bench's internal confirm (cwcli's `confirm_or_exit` gate is the human confirmation). `uninstall` only removes the app from site(s); deleting its code from the bench is out of scope (that is `bench remove-app`, run manually).

`cwcli update` deprecation + frappe special-case (folded into `commands/update.py`):
- `cwcli update` is now a DEPRECATED alias that prints a notice and delegates to the shared `run_app_update`, which both `apps update` and `update` call - one implementation, no drift. `run_app_update` validates >=1 app then calls `_update_project`.
- `_update_project` gained `sites_filter` (the repeatable `--site` narrowing, applied to `all_affected_sites` via `_apply_site_filter` in BOTH the verbose and non-verbose branches) and an early **frappe special-case**: if any named app is `frappe` (case-insensitive), it runs `_run_frappe_update_reset` (`bench update --reset`, whole-bench) and returns - the per-app `git pull` loop is skipped, and `--site` does not apply (bench update is bench-wide).
- **`--site` matching zero affected sites refuses, non-zero.** `_fail_if_site_filter_matched_nothing` distinguishes "genuinely nothing to migrate" (no site has the app installed at all - exits 0, unchanged) from "`--site` named a site the app isn't actually on" (a typo/mismatch): when `sites_filter` is non-empty AND the unfiltered affected-site set is non-empty AND filtering narrows it to empty, the command errors naming both the requested and the actually-affected sites and exits 1, rather than silently completing having migrated nothing. Applies to both `apps update` and the deprecated `update` alias (shared `_update_project`).

Regression coverage: `tests/test_apps.py` (a `FakeFrappeContainer` recording every exec + streaming via a fake `client.api`; covers both modes, multi-site fan-out aggregation, git-URL derivation, the destructive/non-TTY refusals, cache-refresh, the frappe reset branch, and the deprecated-`update` warn+delegate). The `apps` commands are `@handle_docker_errors`-decorated, so tests patch `docker_utils.shutil.which` + `docker_utils.docker.from_env` (in the `wired` fixture) and pass every Typer param explicitly.

### Interactive AND non-interactive modes: support and test BOTH (captain standard, 2026-07-04)

Every cwcli command that prompts the user (destructive confirmations, credential entry, selection menus) MUST work in two modes and be E2E-verified in BOTH on a real instance, not just unit tests:

- **Interactive** (a human at a TTY): every prompt is shown AND its input is actually collected.
  A prompt that is skipped, or that returns empty without waiting for input, is a bug (e.g. the restore flow's MariaDB username/password prompts skipping after the "are you sure?" confirm).
- **Non-interactive** (an agent/automation, or any non-TTY): every prompt has a corresponding flag so the command runs to completion with NO prompt - `--yes`/`-y` for confirmations, `--mariadb-root-username`/`--mariadb-root-password` for credentials, `--site` and backup selectors, etc.
  Under a non-TTY WITHOUT the needed flag, a destructive or blocking prompt must refuse with a non-zero exit rather than silently proceeding, defaulting, or hanging.

This is an AXI requirement (agents drive cwcli non-interactively) and a UX requirement (humans get working prompts).
When adding or changing any prompting command: add/verify the non-interactive flags, make the interactive prompts genuinely collect input, and exercise BOTH paths in a real-instance E2E (drive the TTY prompts via a pty/expect, waiting for prompt_toolkit's raw-mode readiness marker `ESC[?2004h` before each keystroke).

### Re-run the real-instance E2E AFTER the no-mistakes run and AFTER any CodeRabbit fixes (captain standard, 2026-07-05)

Unit tests + a green no-mistakes pipeline are NOT sufficient proof for a behavior change.
The no-mistakes review/document steps and CodeRabbit's post-pipeline suggestions frequently produce behavior-altering fixes (e.g. `start` skip-and-continue vs abort, `label --clear` DB/marker consistency, `inspect` Tier-2 read-only, restore path/prompt changes).
Those fixes land AFTER the original E2E was run, so they ship re-validated only by unit tests.

Standard: after the no-mistakes run completes AND after applying any CodeRabbit fixes, run the real-instance E2E AGAIN (on an isolated throwaway instance) to confirm the final shipped code still behaves correctly end-to-end - not just that the unit suite is green.
Treat "CodeRabbit fixes applied" as a trigger to re-E2E, the same way you would after any late behavior change.

### Cutting a release

Current practice at 0.34.0 (the `uv --trusted-publishing` flow), verified against `.github/workflows/release.yml`:

- A version bump touches exactly four files: `version` in `pyproject.toml`, `__version__` in `src/caffeinated_whale_cli/__init__.py`, the project's own entry in `uv.lock` (regenerate via `uv lock`, do not hand-edit), and a new `CHANGELOG.md` section.
- Both `build.yml` and `release.yml` hard-fail when `pyproject.toml` and `__init__.py` disagree, so the two version strings must always be bumped together.
- `CHANGELOG.md` is manually maintained in Keep a Changelog format: a `## [x.y.z] - YYYY-MM-DD` section with `### Added`/`### Changed`/`### Fixed` subsections, entries shaped like ``- **`cmd` command** - description`` with indented sub-bullets, user-facing changes only (no CI/typing internals), no issue numbers.
- Version choice follows semver as this repo practices it: minor for new flags or behavior changes (0.32.0, 0.33.0, 0.34.0), patch for a narrow compatibility fix (0.31.1).
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

## Known hazards

Known-but-UNFIXED data-loss/safety gaps in code terms, so an agent working nearby is warned.
Prune each entry as it is fixed.

None currently tracked.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
