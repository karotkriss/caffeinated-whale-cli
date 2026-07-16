## ADDED Requirements

### Requirement: core.logs_plan owns the logs resolve and returns a typed plan

The system SHALL provide `core.logs_plan(project_name, *, bench=None, bench_path=None, process=None, follow=False, lines=100, auto_start=False) -> Result[LogsPlan]` in a new `core/logs.py`, owning everything `commands/logs.py` performs between its arguments and its `tail`: the frappe container lookup, the container run-state fork, the bench resolution, the Procfile program selection, the log-file existence probe, and the not-cwcli-supervised fallback discovery.

`core.logs_plan` SHALL NOT print, prompt, or call `typer.Exit`, and `core/logs.py` SHALL import no `rich`, no `questionary`, and no `typer`.
`core/logs.py` SHALL NOT import `subprocess`: the tail is the frontend's, and a core module that can spawn one can drift back into owning it.
`core.logs_plan` SHALL be a PURE READ that never launches, installs, or restarts supervisord or the bench, on any path including the fallback.

#### Scenario: The plan is a returned value, not a printed side effect

- **WHEN** `core.logs_plan` resolves a supervised bench's logs
- **THEN** it returns `Result(OK, LogsPlan(...))` naming the log files to tail, and it prints nothing

#### Scenario: The core prints nothing and execs no tail on any path

- **WHEN** `core/logs.py` is imported and inspected
- **THEN** it imports no `rich`, `questionary`, `typer`, or `subprocess`, and `tests/test_core_envelope.py`'s existing import ban covers the first three automatically

#### Scenario: A multi-bench project with no selector is a select_bench choice

- **WHEN** `core.logs_plan` runs against a multi-bench project with no `bench` and no `bench_path`
- **THEN** it returns a `select_bench` `NEEDS_CHOICE` result and probes no log files

#### Scenario: A stopped container is a confirm_start choice, not an auto-start

- **WHEN** `core.logs_plan` runs against a stopped project with `auto_start=False`
- **THEN** it returns a `confirm_start` `NEEDS_CHOICE` result and starts nothing

### Requirement: LogsPlan is declarative and carries no argv and no live Docker object

`LogsPlan` SHALL carry `project`, `container_name`, `bench_path`, `log_files`, `follow`, `lines`, and `not_cwcli_supervised`, all serializable.

`LogsPlan` SHALL NOT carry a command line such as `["docker", "exec", "-it", ...]`, and SHALL NOT carry a live Docker `Container`.
The core describes WHAT to tail and WHERE; the mechanism is the frontend's answer.
An argv would make the core emit `docker` CLI command lines while the rest of the core speaks docker-py, and would hand a future GUI a mechanism it cannot use: a GUI will not shell out to `docker exec -it`, it will open its own stream into its own widget.

`container_name` SHALL be a name string rather than an ID, because the tail needs a name and nothing in this slice needs a live handle; unlike `RunPlan` there is nothing to bridge back via `core.docker.get_container`.

#### Scenario: The plan describes the tail without prescribing the mechanism

- **WHEN** `core.logs_plan` returns a `LogsPlan`
- **THEN** it names the container, the bench path, and the ordered log files, and contains no `docker` argv and no Docker object

### Requirement: An unknown --process is a select_process choice, not a printed error

`core.logs_plan` SHALL resolve `process` against the bench's live Procfile programs via `supervision.program_for_label`, and SHALL return `Result(NEEDS_CHOICE, Choice(kind="select_process", param="process", ...))` listing the normalized valid labels when the label is unknown or ambiguous.

This mirrors `core.restart_process` (`core/restart.py:120-135`), which answers the identical question against the identical substrate.
`commands/logs.py` SHALL render that choice as the message and valid-label list it prints today, exiting 1, so the human-visible behaviour is unchanged.

#### Scenario: An unknown label yields the choice, not an exit

- **WHEN** `core.logs_plan` is given a `process` label that no Procfile program matches
- **THEN** it returns a `select_process` `NEEDS_CHOICE` result listing the valid labels, and raises no error and exits nothing

#### Scenario: The CLI's rendering of that choice is unchanged

- **WHEN** `cwcli logs proj --process nope` runs against a bench whose programs are `web` and `worker_default`
- **THEN** the command exits 1 having named the invalid process and listed the valid labels, exactly as before the migration

### Requirement: The two no-logs outcomes stay distinguishable

`core.logs_plan` SHALL distinguish a bench that is running but has not written the requested logs yet from a bench that nothing manages.

It SHALL raise `CwcliError(NOT_FOUND, "logs.none_yet", ...)` when `supervision.discover_unsupervised_stack` reports a live manager but no matching log files exist, and `CwcliError(NOT_RUNNING, "logs.no_manager", ...)` carrying the `cwcli start` hint when no manager is up.

A genuinely-running-but-quiet bench SHALL NOT be reported as possibly not running.

#### Scenario: A running bench with no matching log is not reported as down

- **WHEN** a honcho-managed bench is live but the requested process has written no log file
- **THEN** `core.logs_plan` raises `logs.none_yet`, and the CLI's message does not say the bench may not be running

#### Scenario: A bench nothing manages gets the start hint

- **WHEN** the container is up but neither supervisord nor honcho manages the bench
- **THEN** `core.logs_plan` raises `logs.no_manager` and the CLI prints the `cwcli start` hint and exits 1, having attempted no tail

### Requirement: The not-cwcli-supervised fallback is preserved and reported on the plan

`core.logs_plan` SHALL preserve `commands/logs.py`'s fallback: when no `<program>.supervisor.log` files exist, it asks `supervision.discover_unsupervised_stack` whether a honcho / `bench start` manager is live and, if so, discovers the bench's real `{bench}/logs/*.log` files rather than assuming supervisord names, filtering by file stem when `process` is given.

The supervised path SHALL be unchanged: the fallback fires ONLY when the `.supervisor.log` files are absent.
`LogsPlan.not_cwcli_supervised` SHALL report that the fallback fired, mirroring `StatusReport.not_cwcli_supervised`, and `commands/logs.py` SHALL render it as the note it prints today.

#### Scenario: The supervised path never reaches the fallback

- **WHEN** cwcli-supervisord per-process logs exist for the bench
- **THEN** `core.logs_plan` returns them with `not_cwcli_supervised=False` and never calls `discover_unsupervised_stack`

#### Scenario: A honcho bench's real logs are discovered, not invented

- **WHEN** no `.supervisor.log` files exist and a honcho manager is live
- **THEN** `core.logs_plan` returns the bench's actual `logs/*.log` files with `not_cwcli_supervised=True`, and names no `<program>.supervisor.log` path

### Requirement: logs --follow is NOT a core.exec_stream consumer, and locked decision 4 does not govern it

`core/logs.py` SHALL NOT re-point the tail onto `core.exec_stream`, and SHALL NOT provide a `logs_stream` iterator.
`commands/logs.py` SHALL retain the `docker exec -it` passthrough as the tail mechanism.
`core.exec_stream` SHALL NOT gain a `tty` parameter to accommodate `logs`.

Locked decision 4 ("streaming operations return typed event iterators") governs operations **cwcli** streams and must therefore emit as typed events.
`logs --follow` is streamed by `tail`, relayed by docker's TTY, and merely awaited by cwcli: the bytes never enter the Python process, so there are no events to type.
This is the same boundary the rework already holds for `open` - the mechanism is correct and belongs to the frontend - reached here by `logs`'s own measurements.

The costs of the alternative are measured, not predicted, against the real `core/exec_stream.py` and real Docker:
closing the exec socket does not kill the exec'd process and Docker exposes no kill-exec API, so a Ctrl+C leaks an orphan `tail -F` per invocation (verified accumulating 1, 2, 3, 4), surviving even a write to the log;
`_poll_exit_code` then sees `Running: True` and honestly raises `exec.stream_lost` after ~10s on every routine stop;
and avoiding both needs `tty=True`, which docker-py makes mutually exclusive with the `demux=True` stream tag locked as `exec_stream` contract point 2 and depended on by `init`'s stderr routing and `axi`'s stdout purity.
The mechanism it would replace is verified clean on the interactive path: `docker exec -it` under a real pty forwards `^C` into the container, exiting 130 with zero orphans.

`core.exec_stream` remains the ONE way to exec-and-stream for every consumer that streams in Python, and remains the right primitive for a future bounded `core.read_logs` (`tail -n N`, no follow), which is verified fit and is a different function for a different frontend.

#### Scenario: The core owns no tail

- **WHEN** `core/logs.py` is imported and inspected
- **THEN** it contains no `exec_stream` call, no `logs_stream`, and no `subprocess` import

#### Scenario: The exec-stream contract is not bent to fit

- **WHEN** this slice is complete
- **THEN** `core/exec_stream.py` is unchanged, retains `demux=True` unconditionally, and has no `tty` parameter

### Requirement: PR #83's exit-code fix is preserved in substance

`commands/logs.py` SHALL continue to propagate the tail's real exit code rather than discard it: `subprocess.run(tail_cmd)` without `check=`, `raise typer.Exit(code=result.returncode)` on a non-zero code that is not 130, the `returncode == 130` clean-stop branch, and the `except KeyboardInterrupt` clean stop.

The migration SHALL NOT re-open the fail-open PR #83 closed, and the five regression tests at `tests/test_logs.py:178-217` SHALL remain green, unchanged in substance, asserting through the same public command surface.

The fix stays in the frontend because it is a property of `subprocess.run`, whose mechanism the core does not own.
It is not superseded by `core.exec_stream`: `exec_inspect`'s `ExitCode` is genuinely `int | None`, which is why `run`/`apps` needed the fail-open typed out of existence, whereas `subprocess.run(...).returncode` is already an honest `int` and was never `None`-able. Nothing in `logs` was mistyped; the returncode was simply not read.

#### Scenario: A failing tail still exits non-zero

- **WHEN** the tail exits 1
- **THEN** `cwcli logs` exits 1

#### Scenario: The real code is propagated, not flattened

- **WHEN** the tail exits 137 (SIGKILL)
- **THEN** `cwcli logs` exits 137

#### Scenario: An interactive Ctrl+C is still a clean exit

- **WHEN** the user's Ctrl+C reaches `tail` through docker's raw mode and it exits 130
- **THEN** `cwcli logs` reports a clean stop and exits 0

### Requirement: logs_plan resolves no more than the command does today

`core.logs_plan` SHALL NOT resolve a default site, SHALL NOT validate the bench path or process label for shell metacharacters, and SHALL NOT probe that the bench directory exists, though each primitive exists in `core/resolvers.py`.

`cwcli logs` performs none of these today, and adding them would add failures the command does not have - the trap recorded at `core/run.py:24-32`.
The existing `shlex.quote` on the two in-container reads SHALL be preserved, because those do interpolate into `sh -c` strings and the quoting is existing behaviour rather than new hardening.

#### Scenario: No resolver is added for a failure the command cannot have

- **WHEN** `core.logs_plan` runs against a bench whose directory does not exist
- **THEN** it behaves as `cwcli logs` does today rather than raising a new bench-dir error
