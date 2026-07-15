## ADDED Requirements

### Requirement: core.exec_stream is a per-exec UI-pure typed event iterator

The system SHALL provide `core.exec_stream(container, cmd, *, workdir=None, environment=None) -> Iterator[ExecEvent]` in a new `core/exec_stream.py`, yielding zero or more `ExecChunk(stream, text)` events followed by exactly one terminal `ExecDone(exit_code)`.
It SHALL take a LIVE container, as `resolvers.resolve_container_state` and `resolvers.require_bench_dir` already do (the no-live-objects rule governs what a `core.<verb>` RETURNS, not what it accepts), and SHALL NOT construct a Docker client of its own.
It SHALL be keyed to a SINGLE exec rather than to a verb, because `run` is the only consumer with one exec per resolve while `update` performs 13 and `init` performs 11 downstream of a single resolve, and a verb-shaped iterator cannot express a fan-out with logic between the execs.
`core.exec_stream` SHALL NOT print, prompt, or call `typer.Exit`, and SHALL NOT import `rich`, `questionary`, or `typer`.
It SHALL accept an `environment` mapping, so the one consumer that carries live secrets in its exec (`init`) can migrate later without the primitive changing.

#### Scenario: The primitive yields tagged chunks then a terminal done event

- **WHEN** `core.exec_stream` runs a command that writes output and exits
- **THEN** it yields one `ExecChunk` per decoded chunk in the order the daemon framed them, followed by exactly one `ExecDone`, and yields nothing after the `ExecDone`

#### Scenario: A command producing no output still reports its exit code

- **WHEN** `core.exec_stream` runs a command that writes nothing
- **THEN** it yields no `ExecChunk` and exactly one `ExecDone` carrying the real exit code

#### Scenario: No live Docker object crosses the boundary

- **WHEN** any `ExecChunk` or `ExecDone` is inspected
- **THEN** every field is a builtin, and the `CancellableStream` returned by `exec_start` is wrapped rather than yielded or returned

### Requirement: The exit code is polled and honest, and an unknown code is never reported as success

`core.exec_stream` SHALL determine the exit code by polling `exec_inspect` while the code is unknown and the exec reports itself running, bounded so that it cannot hang indefinitely.
`ExecDone.exit_code` SHALL be typed `int`, never `int | None`, so that a `None` exit code cannot be expressed or coerced downstream.
When the exit code is genuinely unknowable, `core.exec_stream` SHALL raise `CwcliError(kind=DOCKER)` rather than reporting any number, because reporting `0` claims a success that did not happen and reporting `1` claims a command failure that may not have happened.

This replaces two fail-open sites: `run.py:78` (`result.get("ExitCode", 1)` then `typer.Exit(code=None)`, which exits 0) and `apps.py:78` (`result.get("ExitCode", 1) or 0`, which yields 0).
In both, the `1` default is dead code, because `exec_inspect` returns `{"ExitCode": None, "Running": True}` with the key present while an exec is still going.

#### Scenario: An exec still running when the stream ends does not report success

- **WHEN** the stream ends and `exec_inspect` reports `{"ExitCode": None, "Running": True}`, and a subsequent poll reports the real code
- **THEN** `core.exec_stream` yields `ExecDone` carrying that real code, and never yields `ExecDone(exit_code=0)` on the basis of the `None`

#### Scenario: A lost stream is a typed error, not a success and not a generic failure

- **WHEN** the exit code cannot be established because the exec reports finished with no code, or because the poll bound expires while it still reports running (a dropped connection, which `CancellableStream` renders indistinguishable from a clean end of stream)
- **THEN** `core.exec_stream` raises `CwcliError(kind=DOCKER)` naming the lost stream, and does NOT yield an `ExecDone`

#### Scenario: A failing bench command reports its real non-zero code

- **WHEN** `core.exec_stream` runs a command that exits non-zero
- **THEN** it yields `ExecDone` carrying that exact code

### Requirement: Every chunk carries a stream tag, and the primitive always demuxes

`core.exec_stream` SHALL call `exec_start` with `demux=True` unconditionally and SHALL tag every `ExecChunk` with `stream` of `"stdout"` or `"stderr"`.
There SHALL be no `demux` parameter, because no consumer needs the untagged form: a consumer that wants today's combined output writes both tags to stdout in yield order.
This is byte-for-byte equivalent to today's `demux=False` consumers, because docker-py builds the same `frames_iter(socket, tty)` generator in both modes and differs only in the per-frame wrapper, preserving frame order and content.
The tag is REQUIRED rather than optional because `init.py:171` routes container stderr to `sys.stderr`, and a tagless event would silently lose that routing when `init` migrates; the `cwcli axi` surface likewise cannot keep stdout pure without knowing which stream a byte came from.

#### Scenario: stdout and stderr are distinguishable

- **WHEN** a command writes to both stdout and stderr
- **THEN** each `ExecChunk` carries the tag of the stream it came from, and the events appear in the order the daemon framed them

#### Scenario: A combining consumer reproduces today's output exactly

- **WHEN** a consumer that previously used `demux=False` writes every chunk's text to stdout in yield order
- **THEN** the resulting byte stream is identical to today's, including carriage returns and progress-bar redraws

### Requirement: The stream is decoded incrementally, one decoder per stream, and is always closed

`core.exec_stream` SHALL decode through `utils.docker_utils.utf8_stream_decoder`, holding one incremental decoder PER stream so that a demuxed exec never feeds stderr's bytes into stdout's pending character.
It SHALL NOT decode a chunk in isolation, because Docker frames the socket at 32KB while `bench` (CPython on a pipe) flushes in 8KB blocks, so a chunk boundary lands at an arbitrary byte offset and routinely mid-character on Frappe's routine unicode.
`core.exec_stream` SHALL close the `CancellableStream` when iteration ends for any reason, including an early `break` by the consumer or an exception raised mid-render.
Closing is the caller's documented responsibility (`docker/api/client.py:_read_from_socket`: "If stream=True, then a generator is returned instead and the caller is responsible for closing the response"), and not one of the four current consumers does it.

#### Scenario: A character split across a chunk boundary survives

- **WHEN** a multi-byte character is split across two chunks of the exec stream
- **THEN** the concatenated text of the yielded `ExecChunk` events round-trips exactly, with no `UnicodeDecodeError` and no U+FFFD substitution

#### Scenario: A demuxed exec does not cross-contaminate its decoders

- **WHEN** stdout and stderr each carry a multi-byte character split across chunk boundaries in the same exec
- **THEN** both streams decode correctly and independently

#### Scenario: The stream is released when a consumer stops early

- **WHEN** a consumer breaks out of the iteration before the terminal event, or an exception propagates through it
- **THEN** the underlying stream is closed rather than leaked

### Requirement: run, apps, and update route their exec streams through the primitive

`commands/run.py`, `commands/apps.py`, and `commands/update.py` SHALL obtain exec output and exit codes through `core.exec_stream`, and SHALL NOT call `exec_create`/`exec_start`/`exec_inspect` directly.
Re-pointing `apps` and `update` is REQUIRED, not optional, because that is where the fail-open exit code lives; leaving them on their own loops would mean writing the fix and leaving it uncalled in the commands that carry the bug.
`apps` and `update` SHALL NOT be migrated as commands: they keep their typer signatures, `rich` rendering, multi-site fan-out, maintenance-mode safety, and exit-code aggregation, and gain no `core.apps`, no `core.update`, and no `axi` verb.

#### Scenario: The three consumption modes are frontend concerns

- **WHEN** a consumer needs live output (`run`, verbose `update`, human `apps`), needs only the exit code (non-verbose `update`, which runs under a spinner), or needs the output captured (`apps --json`, whose stdout must hold only the JSON document)
- **THEN** each consumes the same iterator differently - rendering each event, draining and discarding, or draining and joining - and the primitive is unchanged between them

#### Scenario: apps stops reporting a successful uninstall on an unknown exit code

- **WHEN** `apps uninstall` runs a bench command whose exit code cannot be established
- **THEN** the result is not reported as `"ok": True`

#### Scenario: update's behavior is preserved across the re-point

- **WHEN** the existing `tests/test_apps.py` suite runs after `apps` and `update` are re-pointed
- **THEN** it passes unchanged, including both output modes, the multi-site fan-out, the frappe reset path, and the maintenance-mode `finally`

#### Scenario: A dropped connection during a long update raises instead of hanging

- **WHEN** `update`'s stream is lost while its bench command is still running
- **THEN** it raises a typed error rather than polling forever, replacing today's unbounded `while exit_code is None` loop at `update.py:51-55`

### Requirement: The superseded decode_exec_stream helper is removed

`utils.docker_utils.decode_exec_stream` SHALL be deleted once `run`, `apps`, and `update` route through `core.exec_stream`, because those three call sites (`run.py:73`, `apps.py:74`, `update.py:42`) are its only callers and it therefore becomes dead code.
`utils.docker_utils.utf8_stream_decoder` SHALL be retained unchanged and reused by `core.exec_stream`; it keeps two consumers (`core/exec_stream.py` and `init.py`) and SHALL NOT be moved into `core/`.
The guard tests in `tests/test_exec_stream_decode.py` SHALL be re-pointed at the primitive rather than removed with the helper, because they pin a crash that shipped.

#### Scenario: The helper has no remaining callers

- **WHEN** the re-point is complete
- **THEN** no module imports `decode_exec_stream`, and the symbol is gone

#### Scenario: The split-character guard survives the deletion

- **WHEN** the re-pointed `tests/test_exec_stream_decode.py` runs
- **THEN** the mid-character-split property still holds for `run`, `apps`, and `update` through the primitive, and `init`'s cases are unchanged
