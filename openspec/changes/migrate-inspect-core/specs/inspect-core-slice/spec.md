## ADDED Requirements

### Requirement: core.inspect owns the 3-tier freshness state machine and returns a typed report

The system SHALL provide `core.inspect(project_name, *, refresh="auto", auto_start=False, offer_choice=True, on_event=None) -> Result[InspectReport]` in a new `core/inspect.py`, owning the T1/T2/T3 tier selection, the bench discovery and gather fan-out, and the cache write.

`refresh="cache_only"` SHALL serve the cache verbatim with zero container calls (today's `--no-refresh`); `refresh="full"` SHALL force a full re-inspect (today's `--update`); `refresh="auto"` SHALL run the tiered read: serve cache when containers are not running, run the read-only partial pass on a cache hit, escalate to a full inspect on drift or cache miss.

`core/inspect.py` SHALL import no `rich`, no `questionary`, and no `typer`, and SHALL never print, prompt, or exit; the `--verbose` diagnostic trace SHALL ride the optional typed-event `on_event` callback, never `Result.warnings`.

#### Scenario: A cache hit with no drift serves the cache unchanged

- **WHEN** `core.inspect(refresh="auto")` runs against a project with a valid cache and no on-disk drift
- **THEN** it returns `Result(OK, InspectReport(served_from="partial", ...))` with the cached data, and writes nothing

#### Scenario: cache_only never touches a container

- **WHEN** `core.inspect(refresh="cache_only")` runs against a cached project
- **THEN** it returns the cached data with `served_from="cache"` having made zero container calls

#### Scenario: The core is silent

- **WHEN** any `core.inspect` path runs without an `on_event` callback
- **THEN** nothing is written to stdout or stderr, and `tests/test_core_envelope.py`'s import ban covers `core/inspect.py` automatically

### Requirement: core.inspect is a plain function, not a generator and not two-phase

`core.inspect` SHALL be a plain function returning `Result[InspectReport]`; it SHALL NOT return an iterator, SHALL NOT be split into a plan/stream or plan/apply pair, and SHALL NOT consume `core.exec_stream`.

Nothing consumes intermediate results (the spinner is content-free), `NEEDS_CHOICE` must be returnable at call time (a generator cannot return it), nothing streams (every probe is a short buffered `exec_run`), and there is no `try/finally` cleanup pressure.
This follows `core.update`'s counter-example to reading locked decision 4 as "every progress-emitting op returns an iterator", and `core/logs.py`'s measured non-consumption precedent.

#### Scenario: NEEDS_CHOICE is available at call time

- **WHEN** `core.inspect` must escalate to a full inspect against a stopped project with `offer_choice=True`
- **THEN** the call itself returns `Result(NEEDS_CHOICE, choice=Choice(kind="confirm_start", ...))` without any iteration step

### Requirement: The core owns the cache write, with the tier write-contract intact

`core.inspect` SHALL call the unchanged `db_utils.cache_project_data` itself: on T3 success it SHALL write; on the T2 partial pass it SHALL NEVER write; on a drift escalation that cannot rediscover any bench it SHALL degrade to the remembered cached data WITHOUT persisting and report `degraded=True`; the hard `CwcliError(NOT_FOUND)` for "no bench instances" SHALL survive ONLY on the `refresh="full"` / cache-miss paths.

Secret redaction SHALL remain inside `db_utils.cache_project_data` (the single chokepoint) and SHALL NOT move into the inspect layer, because redacting there would strip in-memory dicts the same run uses.
The cached `installed_apps` line format SHALL remain the RAW `bench list-apps` output lines; changing that rawness is a separately-tracked decision (`migrate-apps-core/tasks.md` §8), not this batch's.

#### Scenario: T2 never writes

- **WHEN** the partial pass finds no drift, or finds drift, or fails outright
- **THEN** `db_utils.cache_project_data` has not been called by the T2 path in any of the three cases

#### Scenario: A drift escalation that rediscovers nothing degrades without persisting

- **WHEN** the T2 pass detects drift and the escalated full inspect discovers zero benches
- **THEN** the cached benches are served with `degraded=True`, nothing is persisted, and no error is raised

#### Scenario: A full inspect on a cache miss still fails hard

- **WHEN** `core.inspect(refresh="full")` (or a cache-miss auto run) discovers zero benches
- **THEN** it raises `CwcliError(NOT_FOUND)`, matching today's "No Bench Instances found" + exit 1 through the CLI

### Requirement: T2 is passive by construction; auto-start belongs only to T3

The T2 partial pass SHALL check container state without prompting and without starting anything, EVEN when `auto_start=True`; when containers are not running, T2 SHALL serve the cache as-is.
Only the T3 full-inspect path may auto-start (with `auto_start=True`) or surface a `confirm_start` choice (with `offer_choice=True`); with `offer_choice=False` a stopped project on the T3 path SHALL raise `CwcliError(NOT_RUNNING)` instead, preserving the spinner-borne caller contract.

#### Scenario: A stopped project under auto_start=True is not started by a cache-hit read

- **WHEN** `core.inspect(refresh="auto", auto_start=True)` runs against a stopped project with a valid cache
- **THEN** the cached data is served, and no container is started

#### Scenario: A spinner-borne caller gets a typed error, never a choice

- **WHEN** `core.inspect(refresh="full", offer_choice=False)` runs against a stopped project
- **THEN** it raises `CwcliError(NOT_RUNNING)` and returns no `NEEDS_CHOICE`

### Requirement: InspectReport is serializable and carries no live Docker object

`InspectReport` SHALL carry the project name, `served_from` (`"cache"`/`"partial"`/`"full"`), `degraded`, and per-bench data (index, path, optional label, optional current/default site, available apps, and per-site name/installed-apps), all serializable via `dataclasses.asdict` to plain data.

No live Docker `Container` SHALL cross the `core.inspect` return boundary; the moved helpers take containers as parameters, which remains allowed.

#### Scenario: The report is plain data

- **WHEN** `asdict()` is applied to a returned `InspectReport`
- **THEN** the result contains only plain serializable values, with no Docker object at any depth

### Requirement: partial_refresh is exposed on the core with its pinned semantics

The system SHALL expose the T2 pass as `core.inspect.partial_refresh(container, cached_benches) -> tuple[list[dict], bool]`, returning cache-shaped dicts (NOT DTOs, because the dicts ARE the cache shape and feed straight back into cache and consumer comparisons).

Its pinned behaviors SHALL move unchanged: cheap `test`/`ls` probes only (no `find`, no per-site `bench list-apps`, no config re-read), carry-forward of cached `installed_apps`/configs/`label`/`current_site` (it never re-reads the marker or currentsite.txt), drop-vanished-bench-as-drift, and it never writes the cache.

#### Scenario: A just-installed app is visible without a deep probe

- **WHEN** an app directory appears in `apps/` after the last full inspect
- **THEN** `partial_refresh` reports it in `available_apps` and flags drift, without running `bench list-apps`

#### Scenario: The carried-forward label survives the pass

- **WHEN** a cached bench carries a user label and the marker file is unreadable
- **THEN** the refreshed dict still carries the label, because T2 never consults the marker

### Requirement: discover_benches is exposed on the core with the numeric-label order intact

The system SHALL expose bench discovery as `core.inspect.discover_benches(container) -> list[str]`, replacing `rm.py`'s import of the underscore-private `_find_bench_instances` across a module boundary.

The returned order SHALL remain `sorted(set(...))` over the discovered paths, because that order IS the numeric-label / index order every `--bench` selector resolves against.

#### Scenario: Discovery order is stable and sorted

- **WHEN** `discover_benches` finds the same set of benches on two runs
- **THEN** it returns them in the identical sorted-by-path order both times

### Requirement: All seven logic-consumer edges call the core, and the reach-back dies

The `inspect` Typer command SHALL no longer be invoked as a function by any module: `utils/cache.py:recache_project` (body only; signature preserved), `utils/auto_inspect.py:_inspect_project`, `commands/open.py`'s no-cache fallback and in-memory `--app` freshness pass, `commands/update.py`'s bench-path fallback, `commands/restore.py`'s three fallback blocks, and `commands/rm.py`'s live-discovery fallback SHALL each call `core.inspect` / `core.partial_refresh` / `core.discover_benches` instead.

`recache_project` SHALL keep its never-prompt, False-when-stopped contract by calling `core.inspect(refresh="full", offer_choice=False)` and mapping `CwcliError(NOT_RUNNING)` to `False`, so its three callers (`rm.py`, `apps.py`, `core/update.py`) are untouched and `core/update.py` no longer imports the CLI layer at runtime - the last such site.

#### Scenario: The core no longer imports the CLI layer

- **WHEN** `core/update.py`'s recache path runs
- **THEN** no module under `core/` imports anything from `commands/` at runtime

#### Scenario: recache under a spinner still degrades cleanly on a stopped project

- **WHEN** `recache_project` is called for a stopped project
- **THEN** it returns `False` without prompting, starting, or raising, exactly as `test_rm_stopped.py` pins today

#### Scenario: rm's live-discovery backup gate is intact

- **WHEN** `rm`'s cache read raises and live discovery finds multiple benches
- **THEN** every discovered bench is backed up, exactly as `test_rm_safety.py` pins today

### Requirement: The human CLI renders the same bytes it does today

`commands/inspect.py` SHALL become a renderer over `core.inspect`: the `confirm_start` choice is resolved interactively OUTSIDE the spinner (the backup pattern, capped retry), `--yes` maps to `auto_start=True`, the hidden `--no-prompt-start` maps to `offer_choice=False`, and the core call runs under the `TipSpinner`.

The `--json` output SHALL remain byte-identical (the index-augmented cache-dict shape); the tree renderer SHALL be unchanged, including the "(default)" marker's resolution order (`common_site_config.default_site` first, `current_site` fallback); every exit code SHALL be preserved; `--show-apps` SHALL remain declared and remain dead, byte-identical, because its disposition is a separately-held captain decision.

The `-i` interactive labeling loop SHALL stay in the frontend, operating on the returned bench data with today's write ordering: per-bench marker writes (warn-and-continue when the container is unavailable) and ONE bulk `cache_project_data` at the end.

#### Scenario: --json is byte-identical across the migration

- **WHEN** `cwcli inspect <project> --json` runs before and after the migration on identical cache state
- **THEN** the stdout bytes are identical

#### Scenario: The dead flag stays dead

- **WHEN** `cwcli inspect <project> --show-apps` and `cwcli inspect <project>` run on the same state
- **THEN** their outputs are identical, as today

### Requirement: axi inspect is one tiered read verb with no --yes

The system SHALL provide `cwcli axi inspect <project> [--update] [--no-refresh]` as a single verb over `core.inspect` (`--update` -> `refresh="full"`, `--no-refresh` -> `refresh="cache_only"`, default tiered), emitting the `InspectReport` as ONE TOON document on stdout.

There SHALL be no read/refresh verb split (the tiers exist to hide that orchestration) and NO `--yes` flag: a stopped project on the refresh path SHALL surface as a usage-error rendering of the `confirm_start` choice, exit 2, naming `cwcli start <project>`, because an axi verb must never open a start-from-axi path.
A degrade-to-cache result SHALL serve the data with a warning and exit 0 (WARNING is a completed operation).
The `axi benches` not-inspected hint SHALL name `cwcli axi inspect` instead of the human `cwcli inspect`, and the generated skill SHALL be regenerated to pick up the verb.

#### Scenario: One TOON document, exit 0

- **WHEN** `cwcli axi inspect <project>` succeeds
- **THEN** stdout is exactly one TOON document carrying the report, and the exit code is 0

#### Scenario: A stopped project is a usage error naming cwcli start

- **WHEN** `cwcli axi inspect <project> --update` runs against a stopped project
- **THEN** the verb exits 2 with a structured usage error naming `cwcli start <project>`, and no container is started

#### Scenario: The agent dead end is closed

- **WHEN** `cwcli axi benches <project>` runs against a never-inspected project
- **THEN** its NOT_FOUND hint names `cwcli axi inspect`, not the human `cwcli inspect`

### Requirement: Two hardenings are adopted and disclosed, changing rendering but not outcomes

`core/inspect.py`'s probe decode SHALL use `errors="replace"` (today's strict `.decode("utf-8")` at `inspect.py:34` crashes the whole inspect on one non-UTF-8 byte), and a raw docker exception escaping the T3 fan-out SHALL be wrapped into `CwcliError(DOCKER)` (today it is an unhandled traceback, because `@handle_docker_errors` only wraps the ping preflight).

Both SHALL be disclosed as deliberate hardening in the PR.
The outcome contract SHALL NOT change: a T3 mid-fan-out connection loss still aborts without writing the cache (crash-without-corruption, now typed); the T2 pass still degrades to cache on any exception; per-probe failures still absorb to `[]`/`None` and never abort the fan-out.

#### Scenario: A non-UTF-8 probe byte no longer kills the inspect

- **WHEN** a probe's output contains an invalid UTF-8 sequence
- **THEN** the byte is replaced and the inspect completes, where today it crashes

#### Scenario: A dropped daemon connection is a typed error and the cache is untouched

- **WHEN** the docker connection drops mid-T3-fan-out
- **THEN** `core.inspect` raises `CwcliError(DOCKER)` and `cache_project_data` has not been called
