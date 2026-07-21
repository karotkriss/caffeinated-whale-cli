## ADDED Requirements

### Requirement: cwcli axi apps checkout emits one TOON document and never prompts

The system SHALL provide `cwcli axi apps checkout <project> <app> <ref>` with flags `--bench <index|label>` and `--reset`, calling the EXISTING `core.checkout_app` with `auto_start=False` and emitting ONE TOON `AppsReport` document on stdout.

The verb SHALL NOT provide `--yes`, `--path`, `--json`, or `--verbose`.
The verb SHALL NOT prompt on any path.
Progress, the git command echo, and diagnostics SHALL go to stderr; stdout SHALL carry only TOON.

#### Scenario: A successful checkout emits the report and exits 0

- **WHEN** `cwcli axi apps checkout myproj myapp feature/x` fetches and checks out successfully
- **THEN** stdout is one TOON document carrying `project`, `bench_path`, `ok: true`, and one `results` row per git step (`fetch`, `checkout`), and the process exits 0

#### Scenario: --reset adds its own reported step

- **WHEN** `cwcli axi apps checkout myproj myapp feature/x --reset` succeeds
- **THEN** the document carries a third `results` row whose `action` is `reset`, recording that the destructive hard reset ran

#### Scenario: The git command echo never reaches the document

- **WHEN** the verb runs any git step
- **THEN** the `$ git ...` echo and all progress appear on stderr only, and stdout parses as one TOON document

### Requirement: The verb adds no core logic and reuses the existing envelope machinery

The verb SHALL be a renderer over `core.checkout_app`.
It SHALL reuse `emit_result`, `emit_axi_error`, and `emit_axi_choice_as_usage_error`, introducing no new `Choice.kind` token and no new `ErrorKind`.

This requirement originally froze `core.checkout_app` as entirely unchanged; the "A dirty working tree is refused" requirement below amended it, adding a guard (`core.apps._refuse_dirty_tree`) shared by both frontends rather than a per-verb branch.
That amendment changes `core.checkout_app`'s behaviour but stays within this requirement's remaining guarantee: no new `Choice.kind` token and no new `ErrorKind` (the guard raises the EXISTING `ErrorKind.CONFLICT`/`ErrorKind.PRECONDITION`).

#### Scenario: axi introduces no new choice or error kind

- **WHEN** the change is implemented
- **THEN** `commands/axi.py`'s checkout wiring introduces no new `Choice.kind` token and no new `ErrorKind`; any behavioural change to `core.checkout_app` itself is governed by its own requirement, not this one

### Requirement: The exit code reads the report, not the envelope status

The verb SHALL exit 0 when `report.ok` is true and 1 when it is false, and SHALL NOT derive its exit code from `Result.status`.

#### Scenario: A failed git step exits non-zero

- **WHEN** a git step fails (an unknown ref or an auth failure)
- **THEN** the document reports `ok: false` with the failing step's row, and the process exits 1

#### Scenario: A WARNING-shaped envelope does not report success

- **WHEN** `core.checkout_app` returns `Status.WARNING` because a step failed
- **THEN** the verb still exits 1, because the exit code reads `report.ok`

### Requirement: A dirty working tree is refused, not silently carried across

The verb SHALL NOT hard-reset or discard the app checkout's working tree unless `--reset` is passed.
`core.checkout_app` SHALL refuse a dirty working tree with `CONFLICT`/`app.dirty_tree` BEFORE fetching anything, so the guard covers every frontend rather than one caller.
Dirty SHALL mean anything plain `git status --porcelain` reports: staged changes, unstaged modifications to tracked files, and untracked files.
`--untracked-files=no` SHALL NOT be used to narrow the read, since a new file not yet added is uncommitted work.
An unreadable `git status` SHALL fail closed as `PRECONDITION`/`app.dirty_state_unknown`, never degrading to "clean".
`--reset` SHALL remain the opt-in through the refusal, and SHALL NOT delete untracked files (no `git clean`).

This requirement supersedes the verb's original reliance on git's own refusal, which covered only a checkout that would OVERWRITE a modified file and let a non-conflicting dirty file through at exit 0.

#### Scenario: A non-conflicting uncommitted edit still blocks the checkout without --reset

- **WHEN** `cwcli axi apps checkout myproj myapp feature/x` runs against an app checkout with an uncommitted edit that the target ref does NOT touch, and no `--reset`
- **THEN** the verb emits `error:`/`help:` naming the dirty path and `--reset`, exits 1, performs no fetch, and leaves the working tree as it was

#### Scenario: An untracked file also blocks the checkout without --reset

- **WHEN** the app checkout contains an untracked file only, and no `--reset`
- **THEN** the verb emits `error:`/`help:` naming the dirty path and `--reset`, exits 1, performs no fetch, and leaves the untracked file in place

#### Scenario: --reset is the only path that discards local work

- **WHEN** the same command is run with `--reset`
- **THEN** the hard reset runs, is recorded as a `reset` row in `results`, and the checkout completes

### Requirement: The verb never starts containers and refuses a stopped project as a usage error

`core.checkout_app` SHALL be called with `auto_start=False`, and a stopped project SHALL be rendered as a TOON usage error (exit 2) naming `cwcli start`.

#### Scenario: A stopped project is a usage error naming cwcli start

- **WHEN** `cwcli axi apps checkout myproj myapp feature/x` runs against a stopped project
- **THEN** it emits a TOON usage error whose help names `cwcli start`, exits 2, and starts nothing

#### Scenario: A multi-bench project with no selector is a usage error naming --bench

- **WHEN** the verb runs against a multi-bench project with no `--bench`
- **THEN** it emits a TOON usage error listing the benches and naming `--bench`, and exits 2

### Requirement: An app that is not a git checkout is a typed error, not a silent no-op

When the target `apps/<app>` directory is absent or is not a git checkout, the verb SHALL emit the core's `NOT_FOUND` / `app.no_checkout` error as a TOON `error:` / `help:` pair with the mapped exit code, and SHALL NOT clone, install, or create the app.

#### Scenario: An unknown app name errors rather than installing

- **WHEN** `cwcli axi apps checkout myproj nosuchapp feature/x` names an app absent from the bench
- **THEN** it emits the `app.no_checkout` error with a help line pointing at `cwcli apps list`, exits non-zero, and installs nothing

### Requirement: A successful checkout refreshes the project cache

When any git step succeeded, the verb SHALL refresh the project cache via `cache.recache_project` so a subsequent `axi apps list` or `axi inspect` does not report the app's pre-checkout state.
A failed recache SHALL be a stderr warning only and SHALL NOT change the exit code.

#### Scenario: The cache is refreshed after a successful checkout

- **WHEN** a checkout succeeds
- **THEN** the project cache is refreshed before the process exits

#### Scenario: A failed recache does not fail the checkout

- **WHEN** the checkout succeeds but the cache refresh fails
- **THEN** a warning is written to stderr, stdout still carries the successful report, and the process exits 0

### Requirement: The verb is asserted present and the sibling deferrals stay asserted absent

A test SHALL assert `checkout` IS registered in `axi_mod.apps_app.registered_commands`, replacing the absence assertion at `tests/test_axi_apps_list.py:148` and recording this decision in its docstring.
The absences of `axi apps install` and `axi apps uninstall` SHALL remain asserted unchanged.
The generated installable skill SHALL list `axi apps checkout` in its verb table, produced by `build_skill.py` walking the live Typer registry, with no hand-edit.

#### Scenario: The verb is registered and its siblings are not

- **WHEN** the `axi apps` Typer app's registered commands are enumerated
- **THEN** `list`, `update`, and `checkout` are present, and `install` and `uninstall` are absent

#### Scenario: The generated skill lists the verb without hand-editing

- **WHEN** `build_skill.py` regenerates the skill from the Typer registry
- **THEN** the skill's verb table includes `axi apps checkout`, its `axi apps install` / `axi apps uninstall` / `axi restore` absent-verb notes are unchanged, and the `--check` unit test passes
