## ADDED Requirements

### Requirement: core.read_logs is a bounded, pure-read log reader returning serializable data

The system SHALL provide `core.read_logs(project_name, *, bench=None, bench_path=None, process=None, lines=100, auto_start=False) -> Result[LogsRead]` in `core/logs.py`, a bounded `tail -n N` (NO follow) over the same bench per-process log files that `core.logs_plan` resolves, including the identical not-cwcli-supervised fallback.

`core.read_logs` SHALL run the tail INSIDE the core as one buffered `container.exec_run`, NOT via `core.exec_stream`, and SHALL return the captured lines as serializable data.
`core.read_logs` SHALL NOT print, prompt, or call `typer.Exit`, and SHALL NOT import `rich`, `questionary`, `typer`, or `subprocess`.
`core.read_logs` SHALL be a PURE READ that never launches, installs, or restarts supervisord or the bench, on any path including the fallback.

#### Scenario: The bounded read returns lines as data, not a printed side effect

- **WHEN** `core.read_logs` reads a supervised bench's logs
- **THEN** it returns `Result(OK, LogsRead(...))` carrying the tailed lines and it prints nothing

#### Scenario: The source is bench per-process logs, not container logs

- **WHEN** `core.read_logs` resolves which files to tail
- **THEN** it tails the bench's per-process log files (with the honcho/`bench start` fallback), never `docker logs`

#### Scenario: No live Docker object and no argv cross the return boundary

- **WHEN** `core.read_logs` returns a `LogsRead`
- **THEN** every field is serializable, and it contains no live Docker `Container` and no `docker exec` argv

### Requirement: LogsRead carries per-process line groups

`LogsRead` SHALL carry `project`, `container_name`, `bench_path`, `lines_requested`, `not_cwcli_supervised`, and `logs`, where `logs` is a list of per-process groups each carrying the normalized process label, the file path tailed, and that file's lines oldest-first.

#### Scenario: A combined read groups lines by process

- **WHEN** `core.read_logs` runs with no `process` against a multi-program bench
- **THEN** `logs` contains one group per program with output, each naming its process and its lines

#### Scenario: A single-process read returns just that process

- **WHEN** `core.read_logs` runs with `process="web"`
- **THEN** `logs` contains only the `web` group

### Requirement: read_logs shares logs_plan's resolve and preserves the three not-found outcomes

`core.read_logs` and `core.logs_plan` SHALL share one private resolve helper (container lookup, run-state fork, bench resolution, `--process` selection, existence probe, not-cwcli-supervised fallback); `core.logs_plan` SHALL continue to return its declarative `LogsPlan` unchanged in behaviour.

`core.read_logs` SHALL keep the three not-found outcomes distinct: a stopped container is a `confirm_start` `NEEDS_CHOICE`; a running container whose bench has a live manager but no logs yet is a successful EMPTY read (`Result(OK, LogsRead(..., logs=[]))` with a `logs.none_yet` warning); a running container whose bench has no live manager raises `CwcliError(NOT_RUNNING, "logs.no_manager")` with the `cwcli start` hint.

#### Scenario: A stopped container is a confirm_start choice, not an auto-start

- **WHEN** `core.read_logs` runs against a stopped project with `auto_start=False`
- **THEN** it returns a `confirm_start` `NEEDS_CHOICE` result and starts nothing

#### Scenario: A running-but-quiet bench is an empty success, not an error

- **WHEN** `core.read_logs` runs against a running bench whose manager is up but which has written no logs yet
- **THEN** it returns `Result(OK, LogsRead(..., logs=[]))` with a `logs.none_yet` warning and raises nothing

#### Scenario: A bench with no live manager raises the no-manager error

- **WHEN** `core.read_logs` runs where the container is up but no supervisord or honcho is running the bench
- **THEN** it raises `CwcliError(NOT_RUNNING, "logs.no_manager")` with a hint naming `cwcli start`

#### Scenario: An unknown --process is a select_process choice

- **WHEN** `core.read_logs` is given a `process` label absent from the bench's Procfile programs
- **THEN** it returns a `select_process` `NEEDS_CHOICE` result listing the valid labels and tails nothing

### Requirement: cwcli axi logs emits one TOON document and never prompts or auto-starts

The system SHALL provide `cwcli axi logs <project>` with flags `--bench`, `--lines/-n` (default 100), and `--process/-p`, calling `core.read_logs` and emitting ONE TOON document on stdout.

The verb SHALL NOT provide `--follow` and SHALL NOT provide `--yes`.
Progress and diagnostics SHALL go to stderr; stdout SHALL carry only TOON.
Log lines SHALL be emitted as raw block lines (one line per indented row, never re-parsed as key:value), grouped under one block per process.

#### Scenario: A successful read emits metadata plus per-process blocks

- **WHEN** `cwcli axi logs myproj` reads logs successfully
- **THEN** stdout is one TOON document: a metadata head (`project`, `bench_path`, `container`, `not_cwcli_supervised`, `lines_requested`) then one raw-line block per process, and the process exits 0

#### Scenario: A running-but-quiet bench exits 0 with an empty read

- **WHEN** `cwcli axi logs myproj` runs against a running bench that has written no logs yet
- **THEN** it emits the metadata head with no process blocks, a `logs.none_yet` note, and exits 0

#### Scenario: A stopped container is a usage error naming cwcli start

- **WHEN** `cwcli axi logs myproj` runs against a stopped project
- **THEN** it emits a TOON usage error whose help names `cwcli start`, and exits 2

#### Scenario: A multi-bench project with no selector is a usage error naming --bench

- **WHEN** `cwcli axi logs myproj` runs against a multi-bench project with no `--bench`
- **THEN** it emits a TOON usage error listing the benches and naming `--bench`, and exits 2

#### Scenario: An unknown --process is a usage error naming --process

- **WHEN** `cwcli axi logs myproj --process nope` names a process absent from the bench
- **THEN** it emits a TOON usage error listing the valid labels and naming `--process`, and exits 2

### Requirement: the axi logs verb is asserted present and listed in the generated skill

A test SHALL assert `cwcli axi logs` is registered in the `axi` Typer app, replacing the never-written absence assertion tracked as the `logs` half of `cwcli-axi-deferral-guards-g4`.
The generated installable skill SHALL list `axi logs` in its verb table, produced by `build_skill.py` walking the live Typer registry; `tests/test_axi_skill.py`'s `--check` unit test SHALL keep it from drifting.
The still-deferred `axi restore`, `axi apps install`, and `axi apps uninstall` absences SHALL remain asserted unchanged.

#### Scenario: The verb is registered

- **WHEN** the `axi` Typer app's registered commands are enumerated
- **THEN** `logs` is present

#### Scenario: The generated skill lists the verb without hand-editing

- **WHEN** `build_skill.py` regenerates the skill from the Typer registry
- **THEN** the skill's verb table includes `axi logs`, and the `--check` unit test passes
