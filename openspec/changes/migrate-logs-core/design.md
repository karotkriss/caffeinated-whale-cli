## Context

`logs` is the smallest of the eight un-migrated commands (251 lines) and the only one whose migration was pre-argued by a scout report (`cwcli-open-handover-design-o9`, 2026-07-15) that reached the opposite conclusion from the project's own committed memory (`CLAUDE.md:72`, written after it).

This design settles that disagreement with an experiment, then migrates `logs` on the shape the evidence supports.

### Audit: what was verified first-hand, and how

All probes ran against real Docker (server 29.4.3, docker-py 7.1.0) with the repo's REAL `core/exec_stream.py` imported from `src/`, at `468d704`.
Scratch files lived in the session scratchpad and are discarded.

| # | Probe | Answers |
| --- | --- | --- |
| A | `exec_stream(c, ["tail","-F",...])` + real SIGINT to self | Does the client-side cleanup o9 describes leave the container clean? **No.** 1 orphan |
| B | Probe A x3 | Do orphans accumulate? **Yes.** 1, 2, 3, 4 |
| C | `_poll_exit_code` on an interrupted follow | Can the exit code be had? **No.** `exec.stream_lost` after 10.1s |
| D | `docker exec -it` under a real pty, `^C` written into the pty | Is the shipped mechanism clean? **Yes.** exit 130, 0 orphans, 3/3 |
| E | bare `docker exec` + SIGINT | Is the non-TTY path clean? **No.** exit 0, leaks 1 each |
| F | `exec_stream(c, ["tail","-n","100",...])` | Is `exec_stream` fine for a BOUNDED read? **Yes.** `ExecDone(exit_code=0)`, 0 leftovers; missing file -> honest `exit_code=1` |
| G | Faithful model of `logs.py:212-232` non-TTY, SIGINT to the process GROUP | Does TODAY's shipped code leak? **Yes.** 1, 2, 3 |

Probe D used a real pty because `docker exec -t` requires one; an earlier contrast run with `-i` alone leaked, which would have argued the opposite, and is exactly the error the pty run corrects.
This matters: the honest contrast is `-it` (raw mode, signal forwarding) against `exec_stream`, not "any subprocess" against `exec_stream`.

Probe F is the reason `exec_stream` is not dismissed wholesale.
It is a perfect fit for a bounded `tail -n N`, which is what a future `axi logs` needs (Decision 6).
The unfitness is specific to `--follow`.

### Audit: what `logs` actually is, mapped at `468d704`

| Lines | What | Migrates? |
| --- | --- | --- |
| `15-25` | `_existing_files`: one `docker exec` probing which log files exist | **Yes.** 0% covered |
| `28-45` | `_discover_bench_log_files`: one `docker exec` globbing the fallback's real logs | **Yes.** 0% covered |
| `48-65` | `_program_log_matches`: pure stem matching | **Yes.** Already pure, already tested |
| `119-144` | ensure-running, container lookup, bench resolution | **Yes.** Ordinary resolve |
| `148-162` | Procfile program selection; unknown `--process` -> print + `Exit(1)` | **Yes**, as `NEEDS_CHOICE` |
| `166-202` | existence probe + not-cwcli-supervised fallback + the two error shapes | **Yes.** Ordinary resolve |
| `204-251` | the `-it` gating, the `tail` argv, `subprocess.run`, the 130 branch, the returncode | **No.** The mechanism, and it is correct |

~120 lines of logic; 12 lines of action.

## Goals / Non-Goals

**Goals.**
Move `logs`'s resolve onto the core behind `Result[LogsPlan]`.
Settle the `--follow` / decision-4 question on evidence and record it so it is not reopened.
Preserve PR #83's exit-code fix in substance.
Raise coverage of what moves from 0% to real.

**Non-Goals.**
Changing the tail mechanism, any human-visible output, or any exit code.
An `axi logs` verb (Decision 6).
Fixing the pre-existing non-TTY orphan leak (Decision 7).
Touching `commands/axi.py`, `utils/agent_hooks.py`, `tests/test_axi*.py`, or the skills/docs owned by crew `cwcli-axi-pass-x9`.

## Decisions

### 1. `logs --follow` does NOT go on `exec_stream`, and locked decision 4 does not govern it

**This is the batch's central decision and it contradicts the brief that commissioned it.**

Decision 4 reads: *streaming operations return typed event iterators.*
`migrate-update-core`'s spec cites `logs` as its paradigm case ("that decision governs genuine streaming operations such as `logs` and raw exec output").
The brief goes further: `logs --follow` "may be the case decision 4 was actually written for."

The refinement the evidence forces:

> **Decision 4 governs operations *cwcli* streams. `logs --follow` is streamed by `tail`, relayed by docker's TTY, and merely awaited by cwcli.**

The bytes of `cwcli logs -f` never enter the Python process.
`tail` writes into a pty that docker allocated inside the container; docker's raw mode relays it to the user's terminal and relays `^C` back the other way.
cwcli's role is to build an argv and wait.
There are no events to type because there is no data in the process to type them from.

That is not a loophole.
It is the same line the o9 report drew for `open` and called "where the true boundary is" (report line 330):

> `execvp` is correct, and it must stay in the frontend. [...] Re-implementing an interactive TTY through docker-py (`socket=True` plus raw-mode handling) to satisfy a purity rule would be strictly worse code in service of tidiness.

Every clause of that is true of `logs`'s `-it` passthrough, and the report did not apply it to `logs` because it had already classified the two as opposites.
They **are** opposites in the way the report proved (`execvp` replaces the process, `subprocess.run` returns).
They are **siblings** in the way that matters here: both hand a terminal to `docker`, and both are correct to.

The costs of ignoring this are measured, not predicted (Probes A/B/C):

- **An orphan `tail -F` per Ctrl+C, accumulating.** Closing the exec socket does not kill the exec'd process, and Docker exposes no kill-exec API. Verified: 1, 2, 3, 4. It survives a write to the log, so SIGPIPE does not collect it.
- **A 10.1-second hang, then `exec.stream_lost`, on a routine stop.** `_poll_exit_code` sees `Running: True`, because the orphan genuinely is running, and honestly refuses to guess. The primitive behaves perfectly; the operation is wrong for it.
- **A locked contract bent to get there.** Avoiding the above needs `tty=True` on the exec, and `exec_stream` is `demux=True` always (contract point 2, `exec_stream.py:28-36`), which docker-py makes mutually exclusive with `tty=True`. `init` routes stderr to `sys.stderr` and `axi` needs stdout purity; both depend on the tag.

And the mechanism it would replace is verified clean where it counts (Probe D): exit 130, zero orphans, 3/3, under a real pty.

**The brief's framing, answered directly.** It argues: `logs` fails open through a discarded returncode, `run`/`apps` failed open through `ExitCode: None`, same bug class, so the same cure applies.
The bug class is the same.
The cure does not transfer, for a reason specific to the mechanism.
`exec_inspect`'s `ExitCode` is genuinely `int | None`, so `run`/`apps` needed the fail-open **typed out of existence**; that is what `ExecDone.exit_code: int` did.
`subprocess.run(...).returncode` is already an honest `int` and was never `None`-able.
Nothing was mistyped in `logs`; the returncode was simply not read.
PR #83 read it.
There is no fail-open left for `exec_stream` to type away, which is why o9's "the reproduced bug dies" benefit (report line 367) is real but **moot**: it died in PR #83, cured at the mechanism it belongs to.

**What would falsify this decision.** A `tty` parameter on `exec_stream` that preserves the stream tag, or a Docker API that kills an exec. Neither exists today. If either arrives, reopen it: the shape here (`logs_plan` returning a declarative plan) is precisely the shape that would let `logs_stream` be added later without disturbing the resolve.

### 2. `logs` migrates anyway, because the mechanism is not the command

Rejecting `exec_stream` is not rejecting the migration, and conflating those is the error that left this command out of five batches.
~120 lines of ordinary returning logic sit in front of 12 lines of action.
The two costs are concrete and already being paid: 0% coverage on the two shell-out reads (`logs.py:17-25,38-45`, the ONLY substantive misses in the file), and a hand-rolled unknown-`--process` error that duplicates `core.restart_process`'s `select_process` `NEEDS_CHOICE` with an answer only a CLI can consume.

### 3. `LogsPlan` is declarative, never an argv

`LogsPlan` carries `container_name: str`, `log_files: list[str]`, `follow: bool`, `lines: int`, `bench_path: str`, `not_cwcli_supervised: bool`.
It does NOT carry `["docker", "exec", "-it", ...]`.

The o9 report named this risk for `LaunchTarget` (report line 301) and it applies unchanged: an argv would make the core emit `docker` CLI command lines while the rest of the core speaks docker-py, and would hand a future GUI a mechanism it cannot use.
A GUI tailing logs will not shell out to `docker exec -it`; it will open its own stream into its own widget.
`container_name` plus `log_files` is what every frontend needs; the argv is one frontend's answer.

`container_name` is a NAME rather than an ID because the tail needs a name and nothing here needs a live handle, so unlike `RunPlan` there is nothing to bridge back (`core/docker.py:get_container` is not needed).
No live Docker object crosses the boundary, per the locked rule.

### 4. `logs_plan` resolves no MORE than `logs` does today

Batch 3's Decision 8 trap, stated in `core/run.py:24-32`: reaching for available primitives ADDS failures the command does not have.

`logs_plan` therefore does NOT resolve a default site, validate the bench path for metacharacters, or probe that the bench directory exists, though each primitive is one import away.
`logs` does none of them today.
It does keep `shlex.quote` on the two reads, because those DO interpolate into `sh -c` strings (`logs.py:19,40`) and the quoting is existing behaviour, not new hardening.

One deliberate exception, argued rather than slipped in: the unknown-`--process` path returns `NEEDS_CHOICE` instead of raising.
That is a change of **kind**, not of behaviour: the CLI renders it as the identical message plus the identical valid-label list and exits 1, pinned by `test_logs.py:130`'s existing `test_unknown_process_errors`.
It is in scope because a printed error is precisely what a core function may not do, and `core.restart_process` already settled the shape for the same question on the same substrate.

### 5. PR #83's fix is preserved in the frontend, where its mechanism lives

`logs.py:227-251` stays: `result = subprocess.run(tail_cmd)`, the `except KeyboardInterrupt`, the `returncode == 130` clean-stop branch, and `raise typer.Exit(code=result.returncode)`.

It stays in `commands/logs.py` and not in the core because it is a property of `subprocess.run`, and `subprocess` is the mechanism the core does not own.
Moving it would mean moving the tail, which is Decision 1 in reverse.

Its five regression tests (`test_logs.py:178-217`) keep asserting the same behaviours through the same public surface.
The migration must not quietly re-open the hole, so `tasks.md` §1 requires those tests green **before** anything moves and **again** after, unchanged in substance.
The comment block at `test_logs.py:166-175` is updated in one respect only: it currently explains the non-consumer decision in a sentence, and should point at this design instead of re-arguing it.

### 6. No `axi logs` verb in this batch

`cwcli axi logs <project> --lines N` is coherent and should exist eventually; o9 is right about that (report line 408), and `--follow` has no place on it for the same reason `axi status` has no `--watch`.

It is deferred on two grounds, neither of them principle:

1. **It needs a different function.** `LogsPlan` is a plan (which files to tail); an agent wants CONTENT (the lines). That is `core.read_logs(...) -> Result[LogsContent]`, a bounded `tail -n N` with no follow - and **that** one is a textbook `exec_stream` consumer in `apps --json`'s drain-and-join mode, verified fit by Probe F. Building it as a side effect of this batch would ship an unrequested DTO; batch 2's `self-update` discipline says ship a verb when a real DTO justifies it, and this batch's DTO does not.
2. **Collision.** It would touch `commands/axi.py`, owned by crew `cwcli-axi-pass-x9` on `fm/cwcli-axi-pass-x9`.

Recording (1) matters more than deferring: it names where `exec_stream` genuinely belongs in `logs`'s future, so Decision 1 is not misread as "`exec_stream` has no place in `logs`". It has one. It is the bounded read, not the follow.

### 7. The non-TTY orphan leak is REPORTED, not fixed

Probe G found that today's shipped `cwcli logs -f` under a pipe/agent leaks an orphan `tail -F` on every Ctrl+C (1, 2, 3), because without `-it` there is no raw mode to forward `^C` and nothing else signals `tail`.
cwcli's `except KeyboardInterrupt` correctly reports a clean stop; the container is left dirty.

Not fixed here, for the reason batch 5 gave for `core/update.py:291`:

- It is **pre-existing** and orthogonal to the migration. This batch moves resolve logic; the leak is in the mechanism, which is explicitly unchanged.
- The fix is **not a one-liner**. Docker has no kill-exec API (Probes A and E both confirm the process outlives its client). The known workaround is to mark the exec uniquely and reap it with a targeted `pkill -f <marker>` in a cleanup exec, which is a real design decision with a real hazard: `CLAUDE.md`'s own lessons warn about broad `pkill` patterns, and getting the marker wrong kills the wrong process.
- It **changes behaviour on interrupt**, so it needs its own E2E evidence in both modes, which is a batch, not a footnote.

Tracked in `tasks.md` §7.
It strengthens Decision 1 rather than weakening it: the leak today is confined to the non-TTY path, and `exec_stream` would extend it to the interactive path that Probe D proves is clean.

### 8. Zero new primitives, as a falsifiable claim

**Claim: this batch adds no new core primitive and bends none.**

What it reuses, unchanged:

| Need | Existing primitive |
| --- | --- |
| frappe container | `core/docker.py:get_frappe_container` |
| running / auto-start fork | `resolvers.resolve_container_state(offer_choice=True)` |
| bench selection | `resolvers.resolve_bench`, `resolvers.DEFAULT_BENCH_PATH` |
| Procfile programs, label mapping | `supervision.procfile_programs`, `program_for_label`, `_normalize_procfile_key` |
| log paths | `supervision.process_log_path`, `_PROC_LOG_SUFFIX` |
| unsupervised fallback | `supervision.discover_unsupervised_stack` |
| the two reads | `container.exec_run` + `supervision._decode`'s idiom |
| unknown `--process` | `Choice(kind="select_process")`, as `core/restart.py:125` |

**The near-miss, reported: `exec_stream` would need a `tty` parameter.**
It is refused (Decision 1) rather than bent.
Batch 4 reported a near-miss and refused it; batch 2 parameterized one string and said so; batch 3's own primitive needed correcting.
The signal this batch sends is the strongest kind available: a primitive was offered, was measured against the job, and did not fit - and the measurement, not the tidiness, decided it.

**One judgement call that is NOT a new primitive but is worth naming.** `_program_log_matches` (`logs.py:48-65`) moves to `core/logs.py` unchanged. It is pure, already tested by `test_logs.py:307`, and its test moves with it.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class LogsPlan:
    """What to tail, and where. Serializable: no live Docker object, and no argv."""

    project: str
    container_name: str        # a NAME string; the tail needs no Docker object
    bench_path: str
    log_files: list[str]       # resolved, existence-checked, in tail order
    follow: bool
    lines: int
    not_cwcli_supervised: bool  # the fallback fired: these are raw bench logs


def logs_plan(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    process: str | None = None,
    follow: bool = False,
    lines: int = 100,
    auto_start: bool = False,
) -> Result[LogsPlan]: ...
```

`not_cwcli_supervised` mirrors `StatusReport.not_cwcli_supervised` deliberately: the same fact, the same name, and the CLI renders it as today's `(bench not under cwcli supervision - tailing its raw log files)` note.

The two "no logs" outcomes stay distinguishable, because `logs.py:183-202` already distinguishes them and the distinction is load-bearing (a running-but-quiet bench must not be told it may be down):
`CwcliError(NOT_FOUND, "logs.none_yet", ...)` when a manager is up, and `CwcliError(NOT_RUNNING, "logs.no_manager", ...)` with the `cwcli start` hint when nothing manages the bench.

## Boundary discipline

`core/logs.py` imports no `rich`, no `questionary`, no `typer`; `tests/test_core_envelope.py`'s existing import ban covers it automatically.
It never prints, prompts, or exits.
It never launches, installs, or restarts anything: `logs` is a PURE READ, and the fallback path must stay one (`CLAUDE.md:76`).
`subprocess` does not appear in `core/logs.py` at all; the tail stays in the frontend.

## Risks / Trade-offs

- **`logs` ends up as the one migrated command whose action stays a `docker` CLI shell-out.** Accepted, and `open` will be the second when it lands. The alternative is measurably worse (Decision 1). The module docstring must say so, the way `core/run.py:1-33` does, or a later agent will "finish the job" and reintroduce Probes A/B/C.
- **Two "no logs" error kinds where there was one code path.** Accepted: the distinction already exists in the branch shape and is pinned by `test_logs.py:286,295`.
- **`NEEDS_CHOICE` for unknown `--process` is a change of kind.** Mitigated by rendering it identically and by `test_logs.py:130` continuing to pin exit 1.
- **The proposal contradicts its own brief.** Deliberate, evidenced, and escalated for review before any implementation code exists.

## Migration Plan

`tasks.md` order, and it is not arbitrary: PR #83's regression tests go green first and stay green, the pure helper and the two reads move with their tests, then the resolve, then the frontend is reseated over it.
`commands/logs.py` keeps its `tail` block byte-for-byte through the whole batch.

## Open Questions

None blocking. Two recorded:

1. **The non-TTY orphan leak** (Decision 7) needs its own batch and its own decision on the reaping mechanism.
2. **`axi logs`** (Decision 6) is coherent, needs `core.read_logs`, and is blocked on crew ownership of `commands/axi.py` rather than on design.
