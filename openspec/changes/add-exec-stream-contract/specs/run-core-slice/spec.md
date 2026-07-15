## ADDED Requirements

### Requirement: core.run_plan resolves in a Result envelope, separately from streaming

The system SHALL provide `core.run_plan(project, args, *, bench=None, bench_path=None, auto_start=False) -> Result[RunPlan]` in a new `core/run.py`, owning everything `commands/run.py` performs between its arguments and its exec.
The resolve phase SHALL be a separate call returning `Result[RunPlan]` rather than folded into the streaming iterator, because a generator function's body does not execute until first iteration: a core function returning `Iterator[...]` cannot raise `CwcliError` at call time, and cannot return `NEEDS_CHOICE` at all without yielding a choice as an output event, which the envelope's `Result.choice` exists to prevent.
This split SHALL NOT be described or implemented as a plan/apply split: plan/apply previews a destructive action for confirmation and is deferred to `restore`/`rm`, whereas this split exists solely because generators are lazy.
`core.run_plan` SHALL NOT print, prompt, or call `typer.Exit`.

#### Scenario: An unresolvable project raises at call time, not at first iteration

- **WHEN** `core.run_plan` is called for a project that does not exist
- **THEN** it raises `CwcliError(kind=NOT_FOUND)` from the call itself, so a frontend's `try`/`except` around the call catches it before any output reaches stdout

#### Scenario: A multi-bench project with no selector is a select_bench choice

- **WHEN** `core.run_plan` runs against a multi-bench project with no `bench` and no `bench_path`
- **THEN** it returns a `select_bench` `NEEDS_CHOICE` result and execs nothing

#### Scenario: Both selectors together are a usage error

- **WHEN** `core.run_plan` is given both `bench` and `bench_path`
- **THEN** it raises `CwcliError(kind=USAGE)`

#### Scenario: A project with no cached benches falls back to the default path with a warning

- **WHEN** `core.run_plan` runs against a project with no cached bench data
- **THEN** it resolves `resolvers.DEFAULT_BENCH_PATH` and carries a `bench.default_used` warning in the envelope rather than printing it, matching `core/unlock.py`'s handling of the identical fork

### Requirement: RunPlan carries no live Docker object, and the id is bridged back inside the core

`RunPlan` SHALL carry `container_id` as a Docker ID string, never a live `Container`, and every field SHALL be a builtin.
This is the locked "no live Docker objects leak past the core boundary" contract applied at the two-phase seam: a plan crosses a `core.<verb>` return boundary by construction, which is exactly what the contract forbids for live objects.
Accepting a live container as a parameter remains permitted and unchanged, as `resolvers.resolve_container_state` and `resolvers.require_bench_dir` already do; the rule governs returns, not parameters.
`core.exec_stream` SHALL therefore accept a LIVE container, matching those existing primitives, and SHALL NOT construct a Docker client of its own.
The system SHALL provide `core.docker.get_container(container_id)` as the id-to-handle bridge and `core.run_stream(plan) -> Iterator[ExecEvent]` as phase 2, so the frontend never touches a container and the exec lands on the container that was PLANNED rather than on whatever a later re-resolution by project name would find.

(This requirement was corrected during implementation. The proposal specified `core.exec_stream(container_id, ...)`, which over-applied the return-boundary rule to a parameter; it forced the primitive to build its own client, discarded the client every caller already held, and broke 24 tests in `tests/test_apps.py` whose fakes supply a live container. The existing suite caught it before it shipped.)

#### Scenario: A plan is fully serializable

- **WHEN** a `RunPlan` returned by `core.run_plan` is inspected or serialized
- **THEN** it holds only builtins, and no attribute is or transitively holds a Docker SDK object

#### Scenario: The plan execs the container it planned

- **WHEN** `core.run_stream` is given a `RunPlan`
- **THEN** it resolves `plan.container_id` back to a handle and execs THAT container, rather than re-resolving the project's frappe container afresh

### Requirement: core.run_plan adds no new resolver primitives and resolves no more than run does today

`core.run_plan` SHALL be built from the primitives the foundation already provides (`core.docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench`, `resolvers.DEFAULT_BENCH_PATH`, the `Result`/`Choice`/`CwcliError` envelope) WITHOUT adding new ones.
`core.run_plan` SHALL resolve the container, its run-state, and the bench, and SHALL NOT resolve a default site, validate the site name or bench path, or probe that the bench directory exists.
`run` performs none of those today and passes `bench_path` to `exec_create(workdir=...)` rather than interpolating it into a shell string, so there is no injection for metacharacter validation to prevent; adding any of them would introduce failures `cwcli run` does not have today, which is a behavior change wearing a migration's clothes.
The implementation SHALL report whether any existing primitive needed changing, widening, or special-casing, as batches 1 and 2 did.

#### Scenario: The zero-new-primitives claim is reported either way

- **WHEN** the implementation of `core.run_plan` is complete
- **THEN** the PR states explicitly whether any existing primitive had to be changed, and a primitive that needed bending is reported as a foundation signal rather than absorbed silently

#### Scenario: A bench path that today reaches the container still reaches it

- **WHEN** `core.run_plan` is given a `bench_path` that `cwcli run` accepts today
- **THEN** it resolves to the same path and raises no validation error

### Requirement: cwcli run is a thin frontend and reports an honest exit code

`commands/run.py` SHALL be reseated over `core.run_plan` and `core.exec_stream`, keeping in the frontend only the typer signature, the `--yes`/auto-start prompt via the existing `ensure_containers_running` prologue, the rendering of each chunk, and the exit code.
`cwcli run` SHALL preserve its arguments, flags, and behavior, with ONE deliberate exception: it SHALL NOT exit 0 when the exit code of the bench command cannot be established.
Today `run.py:78-79` reads `result.get("ExitCode", 1)` and raises `typer.Exit(code=None)` on an unknown code, which exits the process 0 and reports a bench command that never finished as a success.

#### Scenario: A failing bench command exits non-zero

- **WHEN** `cwcli run <project> <cmd>` runs a bench command that exits non-zero
- **THEN** `cwcli run` exits with that same code

#### Scenario: An unknown exit code is never reported as success

- **WHEN** the exit code of the bench command cannot be established
- **THEN** `cwcli run` exits non-zero and reports the lost stream, rather than exiting 0

#### Scenario: Output is streamed live, not buffered

- **WHEN** `cwcli run <project> migrate` runs a long bench command
- **THEN** output appears as it is produced, preserving carriage returns and progress-bar redraws, rather than being withheld until completion

### Requirement: run is tested in both modes and at the unit level

`run` SHALL gain unit tests for `core.run_plan`, for `core.exec_stream` as its consumer, and for the reseated `commands/run.py`.
Unit tests are REQUIRED in addition to the E2E, not as an alternative to it: `run` is the only genuinely untested command left in cwcli (`tests/README.md:164`), its single existing test covers the decode path rather than the command, and this batch makes it prove new machinery.
`run` SHALL gain a real-Docker E2E covering BOTH modes, because it is a prompting command: `run.py:43` reaches an auto-start confirm gated by `--yes`.
Interactive mode SHALL be driven through a pty, awaiting the raw-mode marker before each keystroke; non-interactive mode SHALL be driven by `--yes`; and a non-TTY WITHOUT `--yes` SHALL refuse with a non-zero exit rather than hanging or silently proceeding.

#### Scenario: The interactive auto-start prompt is genuinely shown and collects input

- **WHEN** `cwcli run` is driven through a pty against a project whose containers are stopped
- **THEN** the auto-start prompt is displayed, waits for input, and honors the answer

#### Scenario: A non-TTY without --yes refuses

- **WHEN** `cwcli run` runs against stopped containers with stdin not a TTY and no `--yes`
- **THEN** it exits non-zero without prompting, without hanging, and without silently proceeding

#### Scenario: A large unicode stream survives end to end against a real container

- **WHEN** a bench command emitting more than 32KB of output containing multi-byte characters is run through the real `cwcli` binary against a real container
- **THEN** the full output is delivered intact and the command exits with the bench command's real exit code

### Requirement: The documented run examples work

`README.md`'s `cwcli run` examples SHALL be corrected, and `run`'s passthrough limitation SHALL be documented.
`README.md:1330` documents `cwcli run frappe-one --site development.localhost migrate`, which exits 2 with `No such option: --site` because typer claims the argv before `run` sees it.
The `--` separator is the working form and is documented nowhere.

#### Scenario: A documented example runs

- **WHEN** a user copies a `cwcli run` example from `README.md`
- **THEN** it is parsed by `cwcli` rather than rejected as an unknown option

#### Scenario: The flag limitation is discoverable

- **WHEN** a user needs to pass a bench flag such as `--site` or `--branch` through `cwcli run`
- **THEN** the documentation states that flags must follow a `--` separator, and notes that `--branch` without it is misreported as a suggestion to use `--bench`, which is cwcli's bench selector and an unrelated concept
