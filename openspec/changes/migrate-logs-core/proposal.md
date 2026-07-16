## Why

**`logs` migrates onto the core. It does NOT migrate onto `core/exec_stream.py`, and the experiment that settles it is the point of this proposal.**

This batch was briefed on the recon in `cwcli-open-handover-design-o9`, whose recommendation 2 is "move `logs` into the streaming batch, next to `run`", implemented as `logs_plan` + `logs_stream` over `exec_stream`, with "zero new primitives" and three benefits falling out "for free" (report lines 356-371).
That recommendation is **wrong**, and `CLAUDE.md:72`'s later line ("`logs` is a deliberate NON-consumer ... re-pointing it would leak a `tail -F` per Ctrl+C and turn a normal stop into `exec.stream_lost`") is **right**.

Two committed documents disagree.
Arbitrating them by prose is exactly the failure mode this rework already has a standing warning about (`CLAUDE.md:75`: "state the un-migrated list from the code, not from a prior proposal's prose").
So it was settled by experiment, against the REAL `core/exec_stream.py`, a REAL container, and a REAL SIGINT.

**Result 1: `logs --follow` on `exec_stream` leaks an orphan `tail -F` per Ctrl+C, and they accumulate.**

```
A. exec_stream + real SIGINT
  consumer: KeyboardInterrupt propagated out of the generator (as o9 predicts)
  consumer: exec_stream's `finally` closed the socket
  >>> `tail -F` still alive in container AFTER the socket closed: 1
  >>> after a WRITE to the log (would SIGPIPE it): 1 alive

B. Repeat 3x: do orphans accumulate?
  after Ctrl+C #1: 2 live `tail -F`
  after Ctrl+C #2: 3 live `tail -F`
  after Ctrl+C #3: 4 live `tail -F`
```

Closing the exec socket does not kill the exec'd process; Docker has no kill-exec API.
The o9 report's line 371 ("Ctrl-C on `logs -f` still works correctly under `exec_stream`") is right about the client and never looked at the container.
The orphan survives even a write to the log, so SIGPIPE does not collect it either.

**Result 2: the exit code cannot be had anyway, and asking costs 10 seconds.**

```
C. _poll_exit_code on an interrupted follow
  RAISED after 10.1s: exec.stream_lost
    message: Lost the connection to the command's output stream; it may still be running.
```

`_poll_exit_code` sees `Running: True` (the orphan **is** still running) and correctly raises.
A routine Ctrl+C would hang ten seconds and then report a scary `DOCKER` error.

**Result 3: the mechanism `exec_stream` would replace is verified CLEAN on the path that matters.**

```
D-real. `docker exec -it` under a REAL pty + Ctrl+C  (the shipped TTY path)
  run #1: during=1 live | docker exit=130 | after Ctrl+C=0 live
  run #2: during=1 live | docker exit=130 | after Ctrl+C=0 live
  run #3: during=1 live | docker exit=130 | after Ctrl+C=0 live
```

Docker's raw mode forwards `^C` into the container, `tail` dies, exit 130, **zero orphans**, 3/3.
That 130 is precisely what `tests/test_logs.py:193`'s `test_ctrl_c_through_dockers_tty_is_a_clean_exit` pins, and this is the first time it has been verified against real Docker from this repo.

`exec_stream` cannot have a TTY: `tty=True` and `demux=True` are mutually exclusive in docker-py, and `demux=True` is locked as contract point 2 (`exec_stream.py:28-36`, the stream tag `init` and `axi` depend on).
So the choice is not "tidy vs untidy".
It is: keep a verified-clean mechanism, or trade it for one that leaks and cannot report an exit code, and bend a locked contract to do it.

**Result 4 (NEW DEFECT, reported not fixed): today's non-TTY `logs -f` already leaks the same orphan.**

Modeling `commands/logs.py:212-232`'s non-TTY branch faithfully, with SIGINT to the process GROUP as a terminal's Ctrl+C actually delivers it:

```
  run #1: cwcli exit=0 ('CWCLI: Stopped viewing logs.') | live `tail -F` in container: 1
  run #2: cwcli exit=0 ('CWCLI: Stopped viewing logs.') | live `tail -F` in container: 2
  run #3: cwcli exit=0 ('CWCLI: Stopped viewing logs.') | live `tail -F` in container: 3
```

Nothing signals `tail` when there is no `-it` raw mode to forward `^C`, so a piped/agent-driven `cwcli logs -f` leaves an orphan every time.
Neither o9, nor PR #83, nor `CLAUDE.md` noticed this.
It is **pre-existing, out of scope, and reported rather than absorbed** (design Decision 7, `tasks.md` §7), for the same reason batch 5 reported `core/update.py:291` instead of fixing it: the fix needs its own evidence, and Docker's missing kill-exec API means it is not a one-liner.

It also **inverts the last argument for `exec_stream`**.
The leak is not a reason to migrate: today it is confined to the non-TTY path, and `exec_stream` would extend it to the interactive path that is currently clean.

| Ctrl+C on `logs -f` | today (shipped) | on `exec_stream` |
| --- | --- | --- |
| TTY (human, `-it`) | **clean**: exit 130, 0 orphans | leaks 1 each, 10s hang, `exec.stream_lost` |
| non-TTY (pipe/agent) | exit 0, leaks 1 each | leaks 1 each, 10s hang, `exec.stream_lost` |

`exec_stream` is strictly worse on one path and no better on the other.

**So why migrate at all?** Because the mechanism is not the command.

`logs` is 251 lines, of which the tail is 12 (`logs.py:212-251`).
The other ~120 are ordinary, returning, envelope-shaped logic: container lookup, bench resolution, Procfile program selection, log-file existence probing, and the not-cwcli-supervised fallback discovery.
That is the same 285-logic/7-action ratio the o9 report used to argue `open` should migrate (Finding 2), and the argument holds here for the same reason.
Two concrete costs are already being paid:

**1. The only uncovered code in `logs.py` is exactly the code that should move.**
Measured at `468d704`:

```
src/caffeinated_whale_cli/commands/logs.py   92 stmts   15 miss   83.70%   17-25, 38-45, 124-125, 133-136, 205, 225
```

Lines `17-25` and `38-45` are `_existing_files` and `_discover_bench_log_files`, the two functions that shell out to `subprocess.run(["docker", "exec", ...])`.
They are at **0%** because a subprocess shell-out cannot be faked, only monkeypatched away wholesale, which every test does (`test_logs.py:62,239,245`).
Their siblings next door in `core/supervision.py` use `container.exec_run` and are covered by `tests/bench_fakes.py`'s existing container fakes.
Moving them is not tidying: it is the difference between 0% and testable.

**2. `logs` hand-rolls a `NEEDS_CHOICE` that `core.restart_process` already models.**
`logs.py:151-159` resolves an unknown `--process` with a `stderr_console.print` plus `typer.Exit(1)`.
`core/restart.py:120-135` answers the identical question with `Result(NEEDS_CHOICE, Choice(kind="select_process", ...))`, listing the valid labels.
Same substrate (`supervision.program_for_label`), same question, two answers, one of which no non-CLI frontend can use.

## What Changes

- **`core/logs.py`, the UI-pure resolve.** `core.logs_plan(project_name, *, bench=None, bench_path=None, process=None, follow=False, lines=100, auto_start=False) -> Result[LogsPlan]` owns everything `commands/logs.py` does between its arguments and its `tail`: the frappe container lookup, the bench resolution, the Procfile program selection, the existence probe, and the not-cwcli-supervised fallback discovery. Returned, never printed.
- **`LogsPlan` is declarative, and the frontend performs the tail.** It carries `container_name`, the resolved `log_files`, `follow`, `lines`, and `not_cwcli_supervised` - never an argv (design Decision 3). This is `RunPlan` minus `run_stream`, and it is the same shape the o9 report settled for `open` (`LaunchTarget`), reached here by `logs`'s own evidence.
- **`logs --follow` is NOT re-pointed onto `exec_stream`, and locked decision 4 does not govern it** (design Decision 1). Decision 4 governs operations **cwcli** streams and must therefore emit as typed events. `logs` delegates streaming to `tail` and relays it through docker's TTY; the bytes never enter the Python process, so there are no events to type. `core/exec_stream.py` remains the ONE way to exec-and-stream for every consumer that actually streams in Python.
- **Unknown `--process` becomes `NEEDS_CHOICE`/`select_process`**, identical in kind to `core.restart_process`. The CLI renders it as today's error plus the valid-label list, so the human-visible behaviour is unchanged and the exit code stays 1.
- **The two reads move to `container.exec_run`**, `core/supervision.py`'s existing idiom, and become coverable by the existing container fakes. The `docker` CLI shell-out survives only for the interactive tail, where it is the correct mechanism. Note `shutil.which("docker")` is already a hard preflight (`docker_utils.py:46`), so the o9 report's "the docker CLI dependency dies" benefit was never available: cwcli requires the binary regardless.
- **PR #83's exit-code fix is preserved in substance, in the frontend**, where the mechanism it guards lives: the propagated `result.returncode`, the 130-is-a-clean-Ctrl+C branch, and the `except KeyboardInterrupt`. Its five regression tests keep asserting the same behaviours through the same public surface (design Decision 5).
- **No `axi logs` verb in this batch** (design Decision 6). It is coherent and should exist eventually, but it needs `core.read_logs -> Result[LogsContent]` returning bounded CONTENT (a different function from a plan), and it would touch `commands/axi.py`, which crew `cwcli-axi-pass-x9` owns on `fm/cwcli-axi-pass-x9`. Deferred on scope and collision, not on principle.

## Impact

- **New:** `src/caffeinated_whale_cli/core/logs.py`, `tests/test_core_logs.py`.
- **Changed:** `commands/logs.py` becomes a renderer over `core.logs_plan` plus the tail it still owns. `tests/test_logs.py` keeps every behaviour it pins, PR #83's included.
- **Unchanged:** the tail mechanism (`docker exec -it`), the human-visible output, every exit code, `core/exec_stream.py`, and `commands/axi.py`.
- **Zero new primitives**, as a falsifiable claim, reported either way (design Decision 8). The near-miss - `exec_stream` needing a `tty` parameter - is REFUSED rather than bent, and refusing it is what this proposal is mostly about.
