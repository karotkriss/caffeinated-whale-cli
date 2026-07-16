## ADDED Requirements

### Requirement: core.open_plan resolves everything and returns a declarative LaunchTarget

The system SHALL provide `core.open_plan(project_name, *, bench=None, bench_path=None, app=None, editor=None, auto_start=False, on_event=None) -> Result[LaunchTarget]` in a new `core/open.py`, owning the resolve chain in today's order: frappe container, run-state, bench, the no-cache fallback populate, the `--app` pass, and the editor.

`LaunchTarget` SHALL carry `project`, `container_name` (a NAME string, because both handover mechanisms consume the name and there is no phase-2 core call to bridge an ID), `working_dir`, and `editor` (`"docker" | "code" | "code-insiders" | "cursor"`), all serializable via `dataclasses.asdict` to plain data.
It SHALL NOT carry an argv, a command line, or any live Docker object: the mechanism belongs to the frontend, so a GUI can perform its own handover.

`core/open.py` SHALL import no `rich`, no `questionary`, and no `typer`, and SHALL never print, prompt, exit, or exec; diagnostics ride the optional typed-event `on_event` callback (`OpenNotice` for unconditional notes, `OpenTrace` for `-v` lines), never `Result.warnings`.

#### Scenario: A running single-bench project resolves to an OK plan

- **WHEN** `open_plan("proj", editor="docker")` runs against a running project with one cached bench
- **THEN** it returns `Result(OK, LaunchTarget(project="proj", container_name=<frappe name>, working_dir=<bench path>, editor="docker"))`

#### Scenario: The plan is plain data

- **WHEN** `asdict()` is applied to a returned `LaunchTarget`
- **THEN** the result contains only plain serializable strings, with no Docker object and no argv at any depth

#### Scenario: The core is silent

- **WHEN** any `open_plan` path runs without an `on_event` callback
- **THEN** nothing is written to stdout or stderr, and `tests/test_core_envelope.py`'s import ban covers `core/open.py` automatically

### Requirement: open_plan is a plain function and the frontend performs the handover

`open_plan` SHALL be a plain function returning `Result[LaunchTarget]`; it SHALL NOT return an iterator, SHALL NOT be split into a plan/stream or plan/apply pair, and SHALL NOT perform or wrap the launch.
The handover SHALL stay in the frontend: `commands/open.py` switches on `LaunchTarget.editor` - `"docker"` calls `exec_into_container(container_name, working_dir=...)` (the codebase's only `execvp`, unchanged in `utils/docker_utils.py`), and the editor values call `vscode_utils.open_in_vscode(...)`, which returns normally.

#### Scenario: The docker branch hands over with the planned arguments

- **WHEN** `cwcli open proj --docker` resolves a plan with `working_dir="/workspace/frappe-bench"`
- **THEN** the frontend calls `exec_into_container` with that container name and working dir, and no core function ever execs

#### Scenario: An editor branch returns normally

- **WHEN** `cwcli open proj --code` resolves a plan on a host where VS Code is installed
- **THEN** the frontend calls `open_in_vscode("code", <container name>, <working dir>, ...)` and the process survives the launch

### Requirement: Three choice surfaces, resolved by the frontend as today

`open_plan` SHALL return `NEEDS_CHOICE` for exactly three forks: `confirm_start` (a stopped project reached past the frontend prologue, `resolve_container_state`'s race backstop), `select_bench` (a multi-bench project with no `--bench`/`--path`), and the new `select_editor` kind (`param="editor"`, no editor requested while at least one editor is installed; options list the installed editors plus Docker with today's labels).

The CLI SHALL resolve them preserving today's behavior: `confirm_start` via the capped once-retry loop after `ensure_containers_running` (the `run.py` pattern); `select_bench` rendered as the error listing the benches with the "Pass --bench" hint, exit 1; `select_editor` as the questionary select on a TTY (cancel exits 1 with "Operation cancelled.") and as the refusal naming `--code`/`--code-insiders`/`--cursor`/`--docker` on a non-TTY, exit 1, then ONE re-invoke with `editor` filled.

#### Scenario: Multi-bench without a selector still refuses to guess

- **WHEN** `cwcli open proj` runs against a cached multi-bench project with no `--bench`/`--path`
- **THEN** the command errors listing the benches and exits 1, opening nothing

#### Scenario: No editor flag on a non-TTY refuses instead of hanging

- **WHEN** `cwcli open proj` runs on a non-TTY with at least one editor installed and no editor flag
- **THEN** it refuses naming the four flags and exits 1, exactly as today

#### Scenario: No editors installed auto-picks docker

- **WHEN** `open_plan("proj", editor=None)` runs on a host where `shutil.which` finds no editor
- **THEN** it returns an OK plan with `editor="docker"` and no choice is surfaced

### Requirement: The editor decision is validated in the core

A requested editor that is not installed SHALL raise `CwcliError(NOT_FOUND, "editor.not_installed")` carrying today's install-URL hint (`docker` is always valid); an unrecognized editor value SHALL raise `CwcliError(USAGE)`.
Detection SHALL use stdlib `shutil.which` inside the core, run once at editor-resolution time; the four boolean CLI flags SHALL fuse to the single `editor` param in the frontend, where the mutual-exclusion error (more than one flag, exit 1) also stays.

#### Scenario: A requested editor that is missing is a typed error

- **WHEN** `open_plan("proj", editor="cursor")` runs on a host without Cursor
- **THEN** it raises `CwcliError(NOT_FOUND)` whose hint names the Cursor install URL, and the CLI exits 1 with today's message

#### Scenario: Conflicting editor flags stay a frontend usage error

- **WHEN** `cwcli open proj --code --docker` runs
- **THEN** the command errors on the flag conflict and exits 1 without calling `open_plan`

### Requirement: The no-cache fallback populate is core-to-core with the merged abort contract intact

When bench resolution finds nothing cached, `open_plan` SHALL emit an `OpenNotice`, call `core.inspect(project_name, refresh="auto", auto_start=<the caller's auto_start>, offer_choice=False)` purely for its cache side effect (the returned report is not consumed), re-resolve the bench, and fall back to `DEFAULT_BENCH_PATH` with a `bench.default_used` warning ONLY when still unresolvable.

The abort/degrade contract SHALL match current HEAD (the merged fallback-abort decision, PR #93): a hard `CwcliError` from the fallback inspect PROPAGATES out of `open_plan` and the CLI exits 1 without ever opening the guessed default path; a non-`CwcliError` exception degrades to `DEFAULT_BENCH_PATH` with a warning; a populate that succeeds but still resolves nothing degrades likewise; a freshly-populated multi-bench project surfaces `select_bench` rather than picking a bench.

One hardening is disclosed, not smuggled: `offer_choice=False` turns the race-window stopped container into a typed `CwcliError(NOT_RUNNING)` abort, where today a returned `confirm_start` is silently discarded and the guessed default path is opened against a stopped container.

#### Scenario: A hard inspect failure aborts instead of opening the default path

- **WHEN** the fallback populate raises `CwcliError(NOT_FOUND, "bench.none_found")`
- **THEN** `open_plan` raises, the CLI exits 1, and no handover is attempted (as `tests/test_open_inspect_fallback.py` pins today)

#### Scenario: An unexpected failure still degrades to the default bench path

- **WHEN** the fallback populate raises a non-`CwcliError` exception
- **THEN** the plan resolves with `working_dir=DEFAULT_BENCH_PATH` and a warning, as today

#### Scenario: A successful populate that resolves nothing degrades with a warning

- **WHEN** the fallback populate completes but the re-resolve still finds no cached bench
- **THEN** the plan resolves with `working_dir=DEFAULT_BENCH_PATH` and a `bench.default_used` warning

### Requirement: The --app pass moves verbatim, matched by path and never persisted

Under `app=<name>`, `open_plan` SHALL: raise `CwcliError(NOT_FOUND)` naming `cwcli inspect` when no bench data is cached; select the cached bench BY PATH against the resolved bench (never `bench_instances[0]`); run `core.partial_refresh` in-memory over the cached benches to see a just-installed app, degrading to the cached list on any exception (with a trace event) and NEVER writing the cache; re-match the refreshed list by path (vanished benches shift indexes); raise `CwcliError(NOT_FOUND)` listing the available apps when the app is absent; and assemble `working_dir = f"{bench_path}/apps/{app}"` on success.

#### Scenario: A just-installed app opens without a manual inspect

- **WHEN** an app directory appears in `apps/` after the last full inspect and `cwcli open proj --docker --app newapp` runs
- **THEN** the plan's `working_dir` ends in `/apps/newapp` and the cache is not written

#### Scenario: An app from another bench is rejected

- **WHEN** `--app` names an app that exists only in a different cached bench than the one being opened
- **THEN** `open_plan` raises `CwcliError(NOT_FOUND)` listing that bench's available apps, and nothing is opened

### Requirement: There is no axi open verb, and its absence is asserted

The `axi` surface SHALL NOT gain an `open` verb: `execvp` destroys the process that owes `axi` its one-TOON-document contract, and the editor branches are meaningless to an agent with no desktop (the plan's serializability is NOT the reason - it serializes fine).
A test SHALL assert the `axi` Typer registry has no `open` command, and `core/open.py`'s docstring SHALL record the structural reason.

#### Scenario: The verb cannot slip in unnoticed

- **WHEN** the axi verb registry is inspected by the test suite
- **THEN** no `open` command is registered, and the assertion names the execvp reason

### Requirement: The human CLI preserves today's observable behavior

`commands/open.py` SHALL become a renderer plus the handover: every exit code preserved (mutual-exclusion 1, not-running refusal 1, selector errors 1, app/editor not-found 1, cancel 1); messages pinned by existing tests byte-identical; `OpenNotice` and warnings rendered unconditionally, `OpenTrace` under `-v`; prompts outside the spinner; `handle_docker_errors` retained.
The one named wording drift is `select_bench`'s rendering aligning to the `run` renderer.

#### Scenario: The characterization suite survives the migration unchanged

- **WHEN** the pre-migration characterization tests (fallback matrix, editor validation, non-TTY refusals, handover arguments) run against the migrated code
- **THEN** they pass without assertion changes
