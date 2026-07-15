# Project agent memory

This file is the project's committed, distilled agent-memory: a tight always-loaded orientation core plus the cross-cutting captain standards that apply to ALL cwcli work.
Per-command incident detail (the former `## Sharp edges` appendix) and the full E2E protocol now live in project-level skills under `.claude/skills/`, indexed in "Deep-dive skills" below and loaded on demand when you touch that command - each skill preserves the root-cause "why" that guards a real shipped bug.
Read this core first; drop into a skill only when you are about to work on that area.
For anything the codebase already shows, this file points to the authoritative source or doc rather than restating it.

`CLAUDE.md` is a symlink to this file; keep it that way.

## Project Context

Caffeinated Whale CLI (`cwcli`) is a command-line tool for managing Frappe/ERPNext Docker instances during local development.
It covers instance lifecycle (init, start, stop, restart, rm), bench and site operations (inspect, run, open, unlock, update, label), and backup/restore including peer-to-peer transfer over sendme.
The source of truth for the user-facing feature set is `README.md`.

## Repository Layout

- `src/caffeinated_whale_cli/commands/` - one module per CLI command (`init`, `inspect`, `rm`, `restore`, `start`, `run`, `label`, `config`, `axi`, ...) plus `utils.py` (shared resolver, confirm, container-running helpers).
- `src/caffeinated_whale_cli/core/` - the UI-pure logic core (no `rich`/`questionary`/`typer`): `envelope.py` (`Result`/`Status`/`Message`/`Choice`), `errors.py` (`CwcliError`/`ErrorKind`), `resolvers.py` + `docker.py` (pure container-state/bench resolvers plus the shared bench-op steps every bench verb reuses: default-site resolution, site/path metachar validation, the bench/site `test -d` probes), `backup.py` + `unlock.py` (the reference migrated command and its near-twin, built from exactly the same primitives), `stop.py`, `list.py` + `where.py` (the migrated read-only slices), `start.py` + `status.py` + `restart.py` (sharing the `supervision.py` supervisord tracked-state contract), `version.py` (install-method detection + PyPI lookup shared by `self-update` and the passive update notice). See "UI-pure logic core" below.
- `src/caffeinated_whale_cli/utils/` - cross-command building blocks: `db_utils.py` (SQLite cache + migrations + secret redaction), `bench_labels.py` (label model + marker I/O), `bench_sites.py` (site detection), `docker_utils.py`, `cache.py`, `sendme_utils.py`, `toon.py` (dependency-free TOON encoder for `axi`).
- `tests/` - pytest suites (50 `test_*.py` files, ~780 tests); `bench_fakes.py` and `bench_fakes_mb.py` hold the container fakes. See `tests/README.md` for the coverage map.
- `docs/e2e/` - worked real-instance E2E evidence (the canonical examples for the recipe below); `docs/technical/`, `docs/testing/`, `docs/contributing/` hold design, test, and workflow docs. The authoritative testing/gate policy - fast-tests-only gate, both-modes requirement, per-change test-writing discipline, one-throwaway real-instance discipline, and no broad Docker prune - is `docs/testing/guide.md#gate-policy`.
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

Each entry is the contract; the linked source file is authoritative and the named deep-dive skill holds the root-cause detail.

- **`init` version gating** (`commands/init.py`) - default branch `version-16`; `--version <N|X.Y.Z>` alias resolves to a `version-N` branch or `vX.Y.Z` tag (`--frappe-branch` still takes a raw ref; the two are mutually exclusive). `bench new-site` MariaDB flag, Python/Node versions, and `setuptools` pin gate on the major version parsed from the ref.
  Existing-bench decline continues on a fresh name instead of dead-ending.
  See skill: `cwcli-lifecycle`.
- **`init` secret handling** (`commands/init.py`) - both `bench new-site` secrets (admin + db-root passwords) ride in the exec `environment=` and are referenced as `$CWCLI_*` in the command string (mirrors `restore.py`'s M5), so they stay off cwcli's echo/logs. The admin password is generated when `--admin-password` is omitted (interactive only; required non-interactively), used verbatim when supplied, and printed once - gated on the site actually being created. See skill: `cwcli-lifecycle`.
- **`inspect` 3-tier freshness** (`commands/inspect.py`) - cache-backed with a read-only drift check; escalates to a full re-cache only on real drift.
  See skill: `cwcli-inspect-benches`.
- **Multi-bench addressing** (`utils/bench_labels.py`, `commands/utils.py:resolve_bench_path`) - `--bench <index|label>` selects a bench; a multi-bench op with no selector errors instead of guessing.
  Labels persist to BOTH the DB and an in-bench marker file for cache-loss recovery.
  See skill: `cwcli-inspect-benches`.
- **`rm` deletion + backup gate** (`commands/rm.py`) - removes named volumes and the project dir that `Container.remove(v=True)` leaves behind, gated by a verified live backup of EVERY bench that fails closed. A STOPPED project on the `--volumes` path is transiently STARTED for that backup (start -> back up -> delete, reusing `start`'s port-conflict handling); if it cannot be started or backed up, rm ABORTS, keeps all data, returns the project to stopped, and exits non-zero (`--no-backup` is the escape hatch; orphans are refused likewise). `_remove_project`'s not-running branch is the fail-closed backstop.
  See skill: `cwcli-lifecycle`.
- **`restore` safety** (`commands/restore.py`) - destructive confirm on both normal and receive paths, non-interactive selectors/flags, secrets off the argv, streamed copies, post-restore migrate + restart.
  See skill: `cwcli-lifecycle`.
- **Cache never stores secrets** (`utils/db_utils.py`) - a whitelist redaction at the single write chokepoint; nothing reads secrets back from the cache.
  See skill: `cwcli-inspect-benches`.
- **`CWCLI_HOME` override** (`utils/config_utils.py:cwcli_home()`) - when set to a non-empty value, relocates cwcli's entire on-disk footprint (config, projects, cache, the auto-inspect run dir, and `rm`'s archive dir) from the default `~/.cwcli` to `$CWCLI_HOME`, without repointing the process `HOME` itself.
  See skill: `cwcli-inspect-benches`.
- **`apps` command group** (`commands/apps.py`) - first-class app management (list/install/uninstall/update) per bench and multi-site by default; reuses the shared resolver/confirm/site-detection/recache primitives; `cwcli update` is now a deprecated alias for `apps update`.
  See skill: `cwcli-apps-update`.
- **`self-update` command** (`commands/self_update.py`, `core/version.py`) - install-method-aware upgrade of cwcli itself (`uv tool upgrade` or `sys.executable -m pip install --upgrade`, never a bare `pip`); a dev/editable checkout or an ephemeral `uvx --from ...` run is always a no-op (never self-modify a source tree). All detection + lookup logic lives in the UI-pure `core/version.py` (install-method detection, a fail-open PyPI lookup, PEP 440 compare via `packaging`, a ~1-day TTL cache under `cwcli_home()/cache`) shared verbatim with the passive notice below. `--check` is a read-only dry run (reports current-vs-latest, exits non-zero when an update is available); `--no-cache` forces a fresh PyPI lookup. Named `self-update`, not `update`, because the top-level `update` is the deprecated Frappe-app updater. Human CLI only - no `cwcli axi` verb (deferred).
- **passive update notice** (`update_notice.py`, `core/version.py:passive_notice`) - the passive companion to `self-update`: a one-line "a newer cwcli is available" hint wired once into `main.py`'s root Typer callback, so it covers BOTH the human CLI and every `cwcli axi` verb without per-command repetition. STDERR-only and gated on `sys.stderr.isatty()`, so it never corrupts machine-readable stdout (the `axi` one-TOON-document contract above all) and never appears in pipes/CI/agent runs; suppressible with `CWCLI_NO_UPDATE_CHECK=1`. `core.version.passive_notice` is the cache-ONLY, non-blocking, fail-open gate reusing the shared `core/version.py` helper: it never makes a blocking network call on the hot path - a missing/stale cache returns no notice and fires ONE detached `_spawn_background_refresh` subprocess that populates the ≤1-day cache for the NEXT run; a dev/editable or `uvx` install shows nothing (nothing to upgrade).
- **UI-pure logic core** (`core/`) - business logic + I/O live in `src/caffeinated_whale_cli/core/`, which imports NO `rich`/`questionary`/`typer` (a unit test in `tests/test_core_envelope.py` enforces the ban). A core function returns the typed envelope `Result[T]` (`core/envelope.py`: `Status` OK/WARNING/NEEDS_CHOICE, `data`, `warnings`, `choice`; NO `errors` field) or raises a typed `CwcliError` (`core/errors.py`: closed `ErrorKind`). A decision it can't make from params is returned as `NEEDS_CHOICE` (never a prompt); each frontend resolves it its own way (CLI prompts + re-invokes; `axi` renders a usage error). No live Docker object may cross a `core.<verb>` return boundary - DTOs carry only serializable data. The human CLI, `cwcli axi`, and any future GUI are thin frontends over one core. Migrated so far: `backup` (`core/backup.py`), `unlock` (`core/unlock.py`), `stop` (`core/stop.py`), the read-only `ls`/`list` (`core/list.py`) and `where` (`core/where.py`), and `start`+`status`+`restart` (`core/start.py`, `core/status.py`, `core/restart.py`, sharing `core/supervision.py`); the shared bench-op resolvers were split into `core/resolvers.py` (pure) + thin CLI wrappers in `commands/utils.py`, and `docker_utils.get_frappe_container` is now a wrapper over `core/docker.py`. The authoritative rationale (seven locked decisions) is `openspec/changes/core-logic-foundation/design.md`; migrating the next command follows that pattern. **`unlock` is the foundation's generality proof**: it is `backup`'s near-twin and was migrated with ZERO new primitives, so the pattern is confirmed not overfitted to its first caller - the remaining bench verbs should fall out the same way, and needing to bend a primitive is a signal worth reporting, not absorbing. Still un-migrated: `inspect`, `label`, `restore`, `rm`, `apps`, `config`, `init`, `run`, `open`; `logs` is a partial (reads `core/supervision.py`, has no `core.logs` and no verb) and `self_update`'s logic is in `core/version.py` with its `axi` verb deliberately deferred. **`open` is a deliberate NON-migration**: it ends in `exec_into_container`'s `os.execvp()` (`utils/docker_utils.py`), which replaces the process and never returns, so it cannot return an envelope and has no coherent `axi` verb - it needs its own design decision (likely: core resolves a `LaunchTarget` DTO, the frontend performs the handover), not a slot in a batch. See skill: `cwcli-core-axi`.
- **supervisord per-process supervisor** (`core/supervision.py`, `core/start.py`, `core/status.py`, `core/restart.py`) - the bench runs under **supervisord** in-container (openspec `add-per-process-supervisor`, REVERSING migrate-start-status-core's D1 "keep honcho"; captain-approved). cwcli generates a supervisord config from the live Procfile (one `[program:<key>]` per line, run via a launcher script `{bench}/logs/.cwcli-run.sh` so PATH/redirections/`.env` behave as under honcho), installs `supervisor` into the bench venv on first supervise (idempotent, FAIL-CLOSED - never a silent honcho fallback), and launches `supervisord -c {bench}/logs/.cwcli-supervisor.conf` detached. `core/supervision.py` reuses the s4 substrate (`discover_stack` tree-walk, `label_for`, `expected_labels`, multi-bench keying) re-pointed from honcho to supervisord: detection keyed by the `-c` config path, `SUPERVISOR="supervisord"`, the marker records `config_path`; per-program state (`RUNNING/STARTING/BACKOFF/EXITED/FATAL/STOPPED`) comes from `supervisorctl_states` (over the unix socket - never touches :8000), and logs are per-process files (`{bench}/logs/<program>.supervisor.log`, supervisord rotation; `commands/logs.py`'s multi-file `tail` is the combined view). **Auto-heal is config STATE, not a loop**: `autorestart=unexpected` + `startretries` backoff + `stopasgroup`/`killasgroup`, gated by `core.start(autorestart=...)` (the `--autorestart`/`--no-autorestart` toggle). `core.restart_process(project, label, bench=...) -> Result[ProcessRestartOutcome]` restarts ONE program (`supervisorctl restart`) leaving siblings running; unknown/ambiguous `--process` returns `NEEDS_CHOICE` `select_process` listing valid labels. `core.status` `overall` DROPS honcho's all-or-nothing: a stable partial stack (a program `FATAL` while web serves) is honestly `degraded` (four tokens kept; FATAL detail rides the per-process `state`). **`core.status` not-cwcli-supervised FALLBACK** (regression fix): when NO cwcli supervisord is detected for the bench, status does NOT report all-down - it falls back to `supervision.discover_unsupervised_stack` (the SAME `ps`/`label_for`/`_descendants` machinery walking the honcho / `bench start` tree, keyed by honcho's cwd), reports each process's TRUE `up`/pid/uptime (per-process supervisord `state` is `None` here), sets `StatusReport.not_cwcli_supervised=True` with a `supervisor.not_cwcli` hint warning, and aggregates to real `running`/`degraded`; it is a PURE READ (never launches supervisord - that stays `cwcli start`'s job) and applies to `status`, `status --watch`, and `axi status`. **`cwcli logs` shares the SAME not-cwcli-supervised fallback** (`commands/logs.py`, mirror of the status fix): a honcho / `bench start` bench has no `<program>.supervisor.log` files, so the supervisord path finds nothing and it used to falsely error `No process logs found ... The bench may not be running`; now, when those files are absent, it asks `discover_unsupervised_stack` if a manager is live and, if so, DISCOVERS the bench's real `{bench}/logs/*.log` files (`_discover_bench_log_files` globs the dir - honcho log names differ from supervisord's, so it never assumes `<program>.supervisor.log`) and tails them (`--process` filters by file stem via `_program_log_matches`). It is a PURE READ (never launches/installs supervisord - `cwcli logs` must not mutate a deliberately-honcho-run bench); the supervisord path is byte-for-byte unchanged (the fallback triggers ONLY when the `.supervisor.log` files are absent); and the "may not be running" hint now appears only when NO manager (supervisord OR honcho) is running, so a genuinely-running-but-unsupervised bench no longer reads as down. `discover_stack` stays supervisord-only (its `supervisor_up` gates `start`/`restart`). `label_for` matches BOTH `bench <cmd>` AND the real runtime `frappe <cmd>` (bench_helper) form for serve/schedule/watch/worker, else a genuinely-serving web/watch/schedule reads as down. Command surface: `cwcli restart <p> [--process <label>] [--bench]` (whole-stack when omitted), `cwcli logs <p> [--process]`, `cwcli axi restart <p> --process <label>` (one TOON doc; `--process` required; no `axi --watch`). `cwcli status --watch` still re-polls with `probe_web=False`. **Explicit NON-GOAL**: auto-relaunch of the supervisor on container BOOT (needs the image entrypoint, unsettable via `docker exec`) - a `cwcli start` is still required after a container restart. See skill: `cwcli-lifecycle`.

## Conventions

- **Tooling.** Use `uv` (`uv sync --frozen --all-extras`, `uv run ...`).
  Format with black and lint with ruff, both at line-length 100 and scoped to `src/` (`uv run black --check src/`, `uv run ruff check src/`).
  `black` is a **dev-only** dependency (in the `dev` extra, NOT runtime `dependencies`): it is a formatter, never imported or subprocessed by `src/`, so keeping it out of runtime deps keeps the `uv tool install`/`uvx` environment lean - do not move it back.
- **Distribution.** cwcli is distributed as a uv tool: `uv tool install caffeinated-whale-cli` (unpinned, so `uv tool upgrade` works) or `uvx --from caffeinated-whale-cli cwcli ...`.
  Both `cwcli` and `caffeinated-whale-cli` console scripts point at `caffeinated_whale_cli.main:cli`; the `--from` is needed because the command name differs from the package name.
  Install/run/upgrade docs live in `README.md`; the isolated E2E evidence is `docs/e2e/uv-tool-distribution.md`.
- **Types.** Keep `uv run mypy src/` at zero errors; it is a blocking gate.
  Prefer accurate annotations over `# type: ignore` (there are none in `src/`), and add the matching `types-*` stub (`types-requests`, `types-toml` are dev deps) rather than ignoring an untyped import.
  Do not loosen `[tool.mypy]` to hide errors.
- **Tests.** Two tiers, split by marker (registered in `pyproject.toml`; `tests/conftest.py` auto-applies `unit` to anything not marked `e2e`/`e2e_p2p`).
  A bare `uv run pytest` runs only the fast `unit` tier (no Docker; the default `-m "not e2e and not e2e_p2p"`); the real-Docker tier is `uv run pytest tests/e2e -m e2e` (set `CWE2E_FRAPPE_MAJOR` for the version leg).
  Unit-tier Typer commands keep their `typer.Option(...)` default objects when a param is omitted, so tests must pass every param explicitly (the `inspect`/`restore` skills flag this trap); the E2E tier instead drives the real `cwcli` binary, exercising Typer parsing. The full E2E protocol + harness contract is in skill `cwcli-e2e-testing`.
- **CI gates.** `lint.yml` runs black + ruff (scoped to `src/`); `test.yml` runs the `Pytest` (now `-m unit`, the fast tier) and `Mypy` jobs on pushes and PRs to all branches; `e2e.yml` runs the real-Docker `e2e` matrix (v14/v15/v16) on PRs into `develop`/`master` and on-demand via the `e2e` PR label.
  `develop` has no branch protection, so a repo admin must still tick `Pytest`/`Mypy` (and the `E2E (frappe vNN)` checks, to make E2E a required gate) as required status checks for them to block merges.
- **Release.** See skill: `cwcli-release`.
- **Commits.** Author commits as `karotkriss <mckay.christopher73@outlook.com>` only.
  Never add an agent name as a co-author.
  Never hand-edit `CHANGELOG.md` outside a deliberate version bump, and never edit auto-generated files.

## Captain standards (cross-cutting, apply to ALL cwcli work)

These are load-bearing and apply regardless of which command you touch; they stay in this always-loaded core, never buried in a per-command skill.

- **Support AND test BOTH interactive and non-interactive modes for every prompting command.**
  Interactive (a human at a TTY): every prompt is shown AND genuinely collects input - a prompt that is skipped, or returns empty without waiting, is a bug (e.g. the restore MariaDB credential prompts skipping after the "are you sure?" confirm).
  Non-interactive (an agent/automation, or any non-TTY): every prompt has a flag (`--yes`/`-y` for confirmations, `--mariadb-root-username`/`--mariadb-root-password` for credentials, `--site` and the backup selectors, ...) so the command runs to completion with NO prompt; a non-TTY WITHOUT the needed flag must refuse with a non-zero exit, never silently proceed, default, or hang.
  This is both an AXI requirement (agents drive cwcli non-interactively) and a UX requirement, and it must be E2E-verified in BOTH modes on a real instance (drive the TTY via a pty, awaiting prompt_toolkit's `ESC[?2004h` raw-mode marker before each keystroke). Full protocol: skill `cwcli-e2e-testing`.

- **Re-run the real-instance E2E AFTER the no-mistakes run AND after any review fixes.**
  Unit tests + a green no-mistakes pipeline are NOT sufficient proof for a behavior change: the no-mistakes review/document steps and post-pipeline review suggestions frequently produce behavior-altering fixes that land AFTER the original E2E ran and ship re-validated only by unit tests (e.g. `start` skip-and-continue vs abort, `label --clear` DB/marker consistency, `inspect` Tier-2 read-only, restore path/prompt changes).
  Treat "review fixes applied" as a trigger to re-run the real-instance E2E on an isolated throwaway instance and confirm the final shipped code still behaves correctly end-to-end - not just that the unit suite is green.

- **Validate dangerous-delete work with a real full lifecycle, against the editable install, never mocks/PyPI.**
  Any change to a destructive-delete path (`rm` and its backup gates above all) must be proven by a real full lifecycle on a throwaway, isolated instance: real `cwcli init` -> seed real data -> `rm` with backup, then verify the backup is genuinely present/restorable and the deletion is honest (volumes + dir actually gone).
  Run it against the worktree's OWN editable install (`uv run cwcli` / a `uv tool install` from the worktree), never mocked fakes and never a PyPI-published build - the point is to exercise the exact code under review, not a stand-in.

## Deep-dive skills

Per-command incident detail and the E2E protocol live in project-level skills under `.claude/skills/`, loaded on demand when you work on that area. Read the relevant skill BEFORE touching that command - each carries the root-cause "why" that guards a real shipped bug.

- **`cwcli-e2e-testing`** - the E2E protocol + `tests/e2e/harness.py` contract (isolation recipe, genuine v14/v15/v16 benches, pty-driven prompts, non-interactive flag driving, teardown). Load when writing/running/extending E2E tests or hand-validating on a real instance.
- **`cwcli-lifecycle`** - init / rm / restore internals (version gating, secret env-transport, existing-bench reuse, rm deletion + the C1/H5/M11 backup gates + multi-bench per-bench backup, restore receive/normal-path safety, shared site detection, default-site resolution, post-restore migrate/restart). Load when editing `commands/init.py`, `rm.py`, `restore.py`, or `utils/bench_sites.py`.
- **`cwcli-inspect-benches`** - inspect 3-tier freshness, multi-bench labels / `--bench` selector / marker recovery, the `--yes` + honest-exit-code contract, cache secret-redaction, `CWCLI_HOME` footprint. Load when editing `commands/inspect.py`, `label.py`, `utils/bench_labels.py`, `db_utils.py`, `config_utils.py`, or the cache/multi-bench addressing.
- **`cwcli-apps-update`** - the `apps` group + `update.py` (multi-site fan-out, `--json` stdout purity, post-mutation recache, `cwcli update` deprecation + frappe special-case, maintenance-mode safety). Load when editing `commands/apps.py` or `update.py`.
- **`cwcli-core-axi`** - the UI-pure logic core + `cwcli axi` boundary + the migrated read-only ls/where slices. Load when editing `core/`, `commands/axi.py`, `utils/toon.py`, `commands/list.py`/`where.py`, or migrating a command onto the core.
- **`cwcli-release`** - cutting a release (the four version-bump files, the uv trusted-publishing PyPI flow). Load when bumping the version or publishing.

## Known hazards

Known-but-UNFIXED data-loss/safety gaps in code terms, so an agent working nearby is warned; this board is always-loaded so warnings are seen. Prune each entry as it is fixed.

None currently tracked.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
Per-command incident detail belongs in the relevant `.claude/skills/` deep-dive skill (indexed under "Deep-dive skills"), not here; keep this core to always-relevant orientation plus the cross-cutting captain standards.
When updating this file, preserve this bar for all agents and keep entries concise.
