## Context

This is the rework's Step 3, batch 3: the **exec-stream contract**, with `core.run` as its reference slice.
It follows `core-logic-foundation` (the `backup` reference slice), `migrate-start-status-core`, `migrate-unlock-stop-core` (batch 1, PR #77), and `migrate-label-core` (batch 2, PR #78).

Unlike batches 1 and 2, this batch **adds architecture**: it is the first to build the streaming machinery locked decision 4 describes ("Streaming operations return typed event iterators. No live Docker objects leak past the core boundary").
Batches 1 and 2 could claim "no new architecture". This one cannot, and that raises the bar on its evidence rather than lowering it.

OpenSpec reads no code, so every `file:line` below comes from a first-hand audit of this worktree at `d6d2349`, not from the backlog and not from the recon alone.
**That mattered more than usual here, because the ground moved twice on the day this was written** (`cwcli-exec-decode-crash-d5` / PR #79 merged, consolidating the decoders), and both the backlog item and the dispatch brief carry claims the code no longer supports.
Where the audit and any prior document disagree, the audit wins and the disagreement is called out (proposal's "Corrections to the record", and Decisions 5 and 9 below).

### Audit: what was verified first-hand, and how

| Claim | Verification | Result |
|---|---|---|
| `run` fails open on an unknown exit code | ran `run.py:78-79`'s idiom verbatim under typer | **process exit 0** |
| `apps` fails open on an unknown exit code | `{'ExitCode': None}.get('ExitCode', 1) or 0` | **`0`** |
| the `1` default is dead code | `.get('ExitCode', 1)` with the key present | `None`; the default never fires |
| `update` polls and is correct | read `update.py:47-56` | polls `while exit_code is None`. **The brief says otherwise; the brief is wrong** |
| a dropped connection looks like a clean EOF | read `docker/types/daemon.py:28-33` | `ProtocolError`/`OSError` -> `StopIteration` |
| the caller must close the stream | read `docker/api/client.py:_read_from_socket` docstring | "the caller is responsible for closing the response" |
| `demux=True` preserves order and content | read `_read_from_socket` + `demux_adaptor` | same `frames_iter`, same order; only the per-frame wrapper differs |
| `decode_exec_stream`'s callers | `grep` | `run.py:73`, `apps.py:74`, `update.py:42`. **Exactly the three re-pointed here** |
| `init` raises `UnboundLocalError` | ran the conditional | **NO.** It short-circuits. The brief is wrong (Decision 9) |
| `README.md:1330` cannot work | ran it against the real binary | `No such option: --site`, exit 2 |
| `run` has no command tests | `grep -rn "commands.run" tests/` | one incidental decode test, from PR #79 |
| baseline | `uv run pytest -q` | 821 passed, 51 deselected |

### Audit: the four consumers, and what the contract must carry

| Consumer | `file:line` | demux | wants the bytes? | exit code | under a spinner? |
|---|---|---|---|---|---|
| `run` | `run.py:73-79` | no | yes, raw -> stdout | **FAILS OPEN** | no |
| `update` verbose | `update.py:37-57` | `demux=False` | yes, raw -> stdout | **polled, correct** | no |
| `update` non-verbose | `update.py:59-62` | n/a (`exec_run`) | **NO, discards them** | correct (blocking) | **yes** |
| `apps` human | `apps.py:67-78` | no | yes, raw -> stdout | **FAILS OPEN** | no |
| `apps` `--json` | `apps.py:56-64` | n/a (`exec_run`) | captures; stderr on failure only | correct (blocking) | no |
| `init` | `init.py:171-183` | **`demux=True`** | **yes, SPLIT** stdout/stderr | fail-closed | no |

Three requirements fall out, none of which `run` alone would have taught us:

1. **The stream must carry a tag.** `init.py:171` is the only `demux=True` in the codebase and routes container stderr to `sys.stderr`. A `text`-only event silently loses that when `init` migrates.
2. **The frontend decides whether to render; the core always streams.** `update.py:59-62` discards its output and uses only the exit code, because it runs inside a `console.status` spinner that streaming would shred. `apps.py:56-64` captures so bench output can never corrupt the `--json` document. These look like a conflict and are not one: **one iterator, three consumption modes** - render each event, drain and discard, or drain and join.
3. **Buffering is not an option.** `bench migrate` and `bench build` are minutes-long and unbounded. Batch 1 buffered `unlock` because its `rm -rfv` of a locks directory is milliseconds (`migrate-unlock-stop-core/design.md:62`), and that reasoning explicitly does not transfer.

### Audit: `run` maps onto the existing primitives

`run.py:43-50` against `core/unlock.py:71-92` (unlock's container/state/bench **prefix**, before its site resolution):

| # | `run` step | `run.py` | Primitive | Exists? |
|---|---|---|---|---|
| 1 | ensure containers running | `:43` | `core_docker.get_frappe_container` + `resolvers.resolve_container_state(offer_choice=True)` | yes |
| 2 | resolve the bench | `:47-50` | `resolvers.resolve_bench` | yes |
| 3 | no cached benches -> default path | `:47-50` (`or "/workspace/frappe-bench"`) | `resolvers.DEFAULT_BENCH_PATH` + a `bench.default_used` warning | yes (`core/unlock.py:81-88`) |
| 4 | find the frappe container | `:52-65` | subsumed by step 1 | yes |
| 5 | assemble `bench <args>` | `:68` | `shlex.quote` join; run-specific logic belonging IN `core.run_plan` | n/a |
| 6 | exec + stream + exit code | `:70-79` | **`core.exec_stream`** | **NO - this batch builds it** |

**New resolver primitives required: zero**, the same result batches 1 and 2 got by the same test.
`run` needs no site resolution, no `validate_site_name`, no `require_bench_dir` probe: it maps onto unlock's **prefix** (`:71-92`), not onto all of unlock.
That is a precision the recon's "step for step" phrasing invites a reader to overshoot, and overshooting it would mean **adding** validation `run` does not do today, which is a behavior change wearing a migration's clothes (Decision 8).

## Goals / Non-Goals

**Goals:** fix a live fail-open exit code in two commands, one of them destructive, at the layer that owns it; build the exec-stream contract shaped by all four consumers rather than by `run` alone; keep the two-phase seam honest about live Docker objects; give `run` - the least-tested command in cwcli - its first unit tests and a both-modes E2E; delete the helper the contract supersedes; and unblock `axi apps update` for batch 4.

**Non-Goals** (the proposal holds the full list): `apps`/`update` as command migrations, `init`, `logs`, `axi run`, plan/apply, `run`'s argv passthrough, the AXI shell.

## Decisions

### 1. The primitive is per-EXEC, not per-VERB

`run` executes **one** command per resolve. Nothing else does:

```
update.py  ->  7 _stream_command sites + 6 exec_run sites, ALL downstream of ONE bench resolve
               (:227 bench update --reset, :273/:284 git pull, :388/:410 migrate,
                :429/:437 bench build, :456 clear-cache, :475 rm locks)
init.py    ->  11 _exec_in_container calls per init run; 4 of them stream
apps.py    ->  3 _run_bench sites
run.py     ->  1
```

- **Decision.** The contract is `core.exec_stream(container_id, cmd, *, workdir, environment)`, plus per-verb resolve functions that produce the target. `run` is the degenerate one-exec case.
- **Why not `core.run(project, args) -> Iterator[RunEvent]`.** `update` fans out to a dozen-plus execs off a single resolve with real logic between them (enable maintenance mode, discover affected sites, disable maintenance in a `finally`). A verb-shaped iterator cannot express that: the resolve would repeat per exec, and the maintenance-mode `try/finally` (`update.py:621-720`) would have nowhere to live.
- **This is why `run` proves the contract but must not shape it.** On both questions that matter, `run` is the worst of the four consumers and `update` is the best: `run` fails open on the exit code where `update` polls, and `run` has one exec where `update` has thirteen. Designing from `run` alone copies `run`'s semantics into the primitive and ships them to all four.

### 2. Two-phase, because generators are lazy. This is NOT plan/apply

A generator function's body does not execute until first iteration.
So if `core.run` were `-> Iterator[RunEvent]`, the established frontend pattern would catch nothing:

```python
try:
    result = core.backup(...)          # commands/axi.py's shipped pattern
except CwcliError as e:
    emit_axi_error(e); raise typer.Exit(exit_for(e.kind))
if result.status is Status.NEEDS_CHOICE:
    emit_axi_choice_as_usage_error(result.choice); raise typer.Exit(2)
```

The `try` would wrap a call that merely constructs a generator, and the `CwcliError` would erupt later from inside the rendering loop, after output may already be on stdout.
Worse, **`NEEDS_CHOICE` cannot be returned by a generator at all.** It would have to be *yielded* as an event, which is a category error: a choice is not an output event, and `Result.choice` (`core/envelope.py:59`) is where the architecture puts it.

- **Decision.** `core.run_plan(...) -> Result[RunPlan]` (resolve; may raise, may return `NEEDS_CHOICE`), then `core.exec_stream(...) -> Iterator[ExecEvent]` (pure I/O; no choices possible, because everything was resolved in phase 1).
- **The proposal must say, and does, that this is not the deferred plan/apply split.** Plan/apply exists to preview a **destructive** action for confirmation and is deferred to `restore`/`rm`. This split exists because generators are lazy. Same two-call shape, unrelated motivation. Conflating them would let a later agent believe this batch settled the plan/apply question.
- **It matches what all four consumers already do**: every one resolves container and bench once, then execs (`run.py:43` then `:72`; `update.py:498-580` then the fan-out; `apps.py:49` then `_run_bench`; `init` then its 11 execs).
- **Rejected: raise eagerly by making `run_plan` return a non-generator wrapper object holding a `.stream()` method.** That is the same two calls wearing one name, and it puts a live stream inside a returned object, which Decision 3 forbids.

### 3. `RunPlan` carries `container_id: str`, never a live container (the seam that keeps decision 4 honest)

This is the decision the recon left open, and it is the one that decides whether the two-phase split respects the locked contract or quietly launders around it.
The recon sketched `core.exec_stream(plan_or_target, cmd, ...)` without settling what a plan holds.

- **The rule, precisely.** Locked decision 4 bans live Docker objects **past the core boundary**, i.e. from crossing a `core.<verb>` **return**. It does not ban a core function from **accepting** one as a parameter, and the foundation already ships exactly that: `resolvers.resolve_container_state(project_name, frappe_container, ...)` and `resolvers.require_bench_dir(frappe_container, bench_path)` both take live containers. Within one core call the container is resolved and used and never leaves.
- **A plan is a return.** `RunPlan` crosses the boundary by construction, so putting a live `Container` in it would be a plain violation - and the tempting one, because the container is right there in `run_plan`'s hand when it returns.
- **Decision.** `RunPlan` carries `container_id: str`, and `exec_stream` takes a `container_id` and re-resolves internally. Every field of `RunPlan` is a builtin. `apps`/`update` pass `frappe_container.id`, which they already hold, so one signature serves all three consumers with no union parameter and no second entry point.
- **The cost is a re-resolution per exec, and it is already the codebase's price.** `docker.from_env()` per call is the shipped idiom: `utils/docker_utils.get_project_containers` builds a client and pings on **every** invocation of **every** command. An extra `containers.get(id)` inside `exec_stream` costs one local-socket round trip against a `bench` command that takes minutes. Precedent settles this; it is not a new concern, and optimizing it now would be speculative.
- **Rejected: `exec_stream(container, ...)` taking the live object, with `commands/run.py` resolving it.** The frontend would resolve the container a second time (`run_plan` just did), which is the double-resolve the plan exists to prevent, and it pushes container resolution back into a frontend the batch is trying to thin.
- **Rejected: a module-level cached client.** Over-engineering against a cost the codebase already pays everywhere; if client churn ever matters it is one change in `utils/docker_utils`, benefiting every command rather than this one.

### 4. `exec_stream` always demuxes. There is no `demux` parameter

- **Decision.** `exec_stream` calls `exec_start(stream=True, demux=True)` unconditionally and tags every chunk `stdout` or `stderr`. Consumers wanting today's combined output write both tags to stdout in yield order.
- **Verified equivalent, not assumed.** `docker/api/client.py:_read_from_socket` builds `gen = frames_iter(socket, tty)` and then either wraps each frame with `demux_adaptor` (`demux=True`) or strips the stream index (`demux=False`). **Same generator, same frames, same order**; the only difference is the per-frame wrapper. So writing both tags to stdout in yield order reproduces today's combined byte stream exactly, including carriage returns and progress-bar redraws.
- **Why no flag.** A `demux` parameter would be a config option for a value that never varies: every consumer either wants the tag (`init`, and the axi surface for stdout purity) or is indifferent to it (`run`, `apps`, `update`, which just write both to stdout). Carrying the tag costs the indifferent consumers one extra `sys.stdout.write` call site and buys `init`'s migration for free.
- **Consequence for `init`.** `init` keeps its stderr routing when it migrates, because the tag is already there. That is the concrete payoff of designing against four consumers rather than one, and it is why the tag is in this batch even though `init` is out of it.
- **One decoder PER stream, not one per exec.** `utf8_stream_decoder`'s own docstring already states the rule ("a demuxed exec (`init`) must not feed stderr's bytes into stdout's pending character"), and `init.py:173-174` already holds two. `exec_stream` holds two for the same reason. Sharing one would mangle both streams at any boundary.

### 5. `decode_exec_stream` is superseded and deleted; `utf8_stream_decoder` is reused and stays put

The brief asks for "the right relationship between the two". The audit answers it, and the answer was not visible to either the brief or the recon (which predates PR #79):

```
decode_exec_stream  ->  callers: run.py:73, apps.py:74, update.py:42     <- EXACTLY the three re-pointed here
utf8_stream_decoder ->  callers: decode_exec_stream, init.py:173, init.py:174
```

- **The finding.** `decode_exec_stream` drops to **zero** production callers the moment this batch lands. It is a non-demuxed byte-iterator-to-text adapter, which is precisely the shape `exec_stream` supersedes (Decision 4: always demux), so it can never be useful again - not even to `init`, which is demuxed and uses `utf8_stream_decoder` directly.
- **Decision.** Delete `decode_exec_stream`. `core/exec_stream.py` reuses `utf8_stream_decoder`, holding one per stream, exactly as `init.py:173-174` does.
- **`utf8_stream_decoder` is the real primitive and stays in `utils/docker_utils.py`.** It keeps two consumers (`core/exec_stream.py` and `init.py`) and needs no change. Moving it into `core/` was considered and rejected as churn: `core/docker.py` already imports from `utils/docker_utils`, so the direction is established precedent, and a move would touch `init` and the guard tests for tidiness alone.
- **The guard test is not deleted with it.** `tests/test_exec_stream_decode.py` pins a crash that **shipped**. Its `run`/`apps`/`update` cases re-point at the primitive and must stay green in substance; its `init` cases are untouched. Deleting the helper must not delete the property.

### 6. No `axi run` verb, and this batch therefore ships none at all

- **There is no DTO.** `emit_result` is `toon.encode(asdict(data), ...)` (`axi.py:62`); arbitrary bench stdout is an opaque text blob. TOON-encoding it would be a serializer in name only.
- **It re-opens the hatch `apps` closed.** `README.md:839` states it directly: the `apps` group "replaces dropping to the raw `cwcli run <project> bench get-app ...` escape hatch".
- **The passthrough is broken for exactly the invocations an agent would need**, verified against the real binary:
  ```
  cwcli run frappe-one --site development.localhost migrate   # README.md:1330 verbatim
    -> Error: No such option: --site                          exit 2
  cwcli run <proj> get-app --branch develop <url>
    -> Error: No such option: --branch  Did you mean --bench? exit 2
  cwcli run <proj> -- --site development.localhost migrate
    -> reaches the real code path                             the `--` separator works
  ```
  Handing an agent `cwcli axi run` would hand it a passthrough that fails on most real bench invocations unless it knows to insert `--`, which is documented nowhere. The `--branch` case is actively misleading: click suggests `--bench`, cwcli's **bench selector**, for what the user meant as a **git branch**.
- **Decision.** No `axi run`. **This batch ships no new axi verb at all**, which breaks the rhythm of every prior batch and is stated in Non-Goals rather than discovered in review.
- **A verb-less core function is established, twice.** `core/supervision.py` has no verb (it is substrate); `core/version.py` shipped verb-less for two batches. Batch 2 added `axi self-update --check` only when a real DTO justified it, and refused the mutating verb. Same discipline.
- **The agent surface loses nothing now and gains later.** `apps` is the sanctioned path and already covers get-app/install/uninstall/update. The payoff is deferred but real: this contract is the precondition for `axi apps update`, which cannot exist today precisely because streaming blocks `--json` (`README.md:839`).

### 7. The honest exit code: poll, bounded, and never coerce an unknown to success

This is the batch's Why, so the contract is stated exactly.

After the stream ends, `exec_inspect` can legitimately report `{"ExitCode": None, "Running": True}`: the daemon closes the stream and records the code non-atomically, so a read immediately after can land in that window. `update.py:47-56` handles it by polling. `run` and `apps` do not, and both turn `None` into 0.

- **Decision.** `exec_stream` polls `exec_inspect` while the code is unknown and the exec reports `Running`, **bounded**, then yields `ExecDone(exit_code: int)` where `exit_code` is genuinely known. `ExecDone.exit_code` is typed `int`, not `int | None`, so the fail-open cannot be re-expressed downstream.
- **When the code is genuinely unknowable, raise `CwcliError(DOCKER)`, do not report a number.** If polling settles with `Running: False` and no code, or the bound expires with `Running: True` (the dropped-connection case, since `CancellableStream` swallows `ProtocolError`/`OSError` into `StopIteration`), the honest answer is "the stream was lost and the exec may still be running", not "your command failed". Reporting `ExecDone(1)` would be the same disease in a milder form: a confident wrong answer. Raising mid-iteration is sound here precisely because phase 1 already resolved everything, so nothing about it is a deferred `NEEDS_CHOICE`.
- **The bound is a real improvement to `update`, and it is disclosed.** `update.py:51-55` polls **unbounded**, so a dropped connection during a still-running `bench migrate` hangs it forever. Re-pointed onto the primitive, it raises instead. That is a behavior change to a command this batch does not migrate; it is in scope because it is inside the exec-and-decode loop that moves, and it replaces a hang with a typed error.
- **`update.py:57`'s `return exit_code if exit_code is not None else 1` is dead code** (the `while` above it cannot exit with `None`) and disappears with the re-point. Noted so its removal is not mistaken for a behavior change.
- **Open for implementation, deliberately.** The exact bound is not specified here. The race window is milliseconds; the value must be small enough not to look like a hang and large enough not to trip on a slow daemon. Pick it in implementation and pin it with a test on the boundary, not on the number.

### 8. Zero new resolver primitives, stated as a falsifiable claim - and `run_plan` must resolve NO MORE than `run` does today

Batches 1 and 2 both made this claim and both reported what came back (batch 1: zero bent; batch 2: one hardcoded string parameterized). The claim's value is the signal, not the score.

- **The claim.** `core.run_plan` needs no new resolver primitive. `run.py:43-50` maps onto `core/unlock.py:71-92` (the container/state/bench prefix), using `core_docker.get_frappe_container`, `resolvers.resolve_container_state`, `resolvers.resolve_bench`, `resolvers.DEFAULT_BENCH_PATH`, and the envelope. Implementation MUST report any primitive that needed changing, widening, or special-casing.
- **The trap this decision exists to close.** `run` resolves **less** than `unlock` does: no default site, no `validate_site_name`, no `validate_bench_path`, no `require_bench_dir` probe. The primitives for all of those exist and are one import away, and a migration that reaches for them because they are there would **add** failures `cwcli run` does not have today - turning a not-breaking migration into a behavior change. `run` passes `bench_path` to `exec_create(workdir=...)`, never interpolating it into a shell string, so the metacharacter validation that protects `backup`/`unlock` has no injection to prevent here.
- **Decision.** `run_plan` resolves exactly steps 1 to 3 of the audit table and nothing more. Adding a probe or a validation is a separate, argued change with its own test, not a migration item.
- **The one place `run` may legitimately gain a warning.** Today `run.py:47-50`'s `or "/workspace/frappe-bench"` fallback is **silent**; `core/unlock.py:81-88` carries a `bench.default_used` warning for the identical fork. `run_plan` returns the warning in the envelope (that is what the envelope is for); whether the reseated frontend **prints** it is a rendering choice, and printing it is additive and opt-in-shaped. Note it in the PR either way.

### 9. Two claims in the dispatch brief are wrong, and the code says so

Recorded here rather than absorbed, because the standing instruction is that the code wins and saying so plainly has been rewarded every time.

- **"`run.py:78` and `update.py:48` carry the same family ... no polling."** `update.py:47-56` polls. It is the only consumer that gets the exit code right, and the recon's own §1d table says exactly that. The fail-open pair is **`run` and `apps`**. The brief's own next paragraph agrees ("getting it right means polling as `update.py:47-56` does"), so this is an internal contradiction in the brief, not a disagreement with the recon. **Impact on the batch: none.** `update` is still re-pointed - its correct-but-duplicated polling is what the primitive absorbs - but the Why names `run` and `apps`.
- **"`init.py:193`: `output` is unbound when `stream_output=True` - `UnboundLocalError`."** It is not:
  ```python
  raw_output = output if not stream_output else b""   # streaming -> takes b"", never evaluates `output`
  ```
  A conditional expression evaluates its condition first and only the taken branch. Verified by running it. The real defect is the recon's milder version: the ENOSPC hint is **dead** whenever streaming. Still latent, still real, still `init`'s batch. **Impact on the batch: none** - `init` was out either way - but a proposal that repeated an `UnboundLocalError` claim into `init`'s brief would have sent the next crew hunting a bug that is not there.

## The DTOs, concretely

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ExecChunk:
    stream: str        # "stdout" | "stderr" - REQUIRED: init.py:171 demuxes; axi needs stdout purity
    text: str          # incrementally decoded, never split mid-character

@dataclass(frozen=True, slots=True, kw_only=True)
class ExecDone:
    exit_code: int     # genuinely known, polled. NOT `int | None` - that is the fail-open, typed out

ExecEvent = ExecChunk | ExecDone

@dataclass(frozen=True, slots=True, kw_only=True)
class RunPlan:
    project: str
    container_id: str  # a Docker ID string, NOT a live Container (Decision 3)
    bench_path: str
    command: str       # the assembled "bench <args>" string
```

- **`ExecChunk.text` carries decoded text, not raw bytes**, because all four consumers immediately decode and the decoding is the thing they historically got wrong; a `bytes` event would hand the bug straight back to them.
- **`ExecDone` is a terminal EVENT, not a generator return value.** A generator's `return` value is reachable only via `StopIteration.value`, which no consumer would remember to read - and a forgotten exit code is exactly the failure this batch exists to end.
- **`ExecDone.exit_code: int` is the fix expressed in the type.** The fail-open is unrepresentable.
- **`RunPlan.command` is the assembled string**, so the shell-quoting decision (`shlex.quote` per arg, `run.py:68`) lives in the core with the rest of the logic rather than in the frontend.

## Boundary discipline

No live Docker object crosses a `core.<verb>` return boundary: every field of `RunPlan`, `ExecChunk`, and `ExecDone` is a builtin.
`exec_stream` **wraps** the `CancellableStream` in a generator yielding typed events and never hands it back, which is locked decision 4's requirement applied to the one return type that could most easily violate it.
`core/exec_stream.py` and `core/run.py` import no `rich`, no `questionary`, no `typer`; the ban is already enforced by `tests/test_core_envelope.py`, which globs `core/*.py` and will cover the new modules automatically.
`exec_stream` closes the stream in a `finally`, so an early `break` by the consumer (or an exception mid-render) releases the socket rather than leaking it, which is what all four consumers do today.

## Risks / Trade-offs

- **`run` is the least-tested command in cwcli and this batch refactors it onto new machinery.** That is the batch's central risk and the reason the captain's ruling 2 stands: unit tests AND the both-modes E2E, not the E2E alone. Batch 1 made the identical call for `unlock` ("a migration would be refactoring blind", `design.md:98`).
- **The `update` re-point is the riskiest edit in the batch.** `_run_frappe_update_reset` (`update.py:227-233`) hardcodes `verbose=True`, so `cwcli apps update <proj> --app frappe` **always** streams, and the re-point runs inside the maintenance-mode `try/finally` (`update.py:709-720`) whose control flow already carries a shipped-bug comment at `:627-631`. Mitigation: `tests/test_apps.py` covers both modes, the multi-site fan-out, and the frappe reset path, so this is refactor-under-green - and the `finally` must be re-verified **by test, not by inspection**.
- **This batch adds architecture, which batches 1 and 2 did not.** A primitive with a single consumer is the overfitting the foundation was rightly criticised for; that is precisely why `apps` and `update` are re-pointed (three real consumers, no command migration) rather than leaving the contract to be proven by `run` alone.
- **The bounded poll changes `update`'s behavior on a dropped connection** from an infinite hang to a typed error (Decision 7). An improvement, in a command this batch does not migrate, disclosed rather than slipped in.
- **Deleting `decode_exec_stream` removes a helper that shipped hours earlier.** Not churn: it becomes dead code by construction (Decision 5). The property it guards survives in the primitive and its test must stay green in substance.
- **A batch with no axi verb** (Decision 6). Accepted: it fixes a live fail-open on a destructive path, closes a documented socket leak, gives `run` its first tests, and is the precondition for `axi apps update`.

## Migration Plan

Ordered so each step is independently reviewable and the risky one lands under green tests.

1. `core/exec_stream.py` + the `ExecChunk`/`ExecDone` DTOs + unit tests against a faked exec API. No consumer changes yet.
2. Re-point `commands/run.py`'s loop at the primitive (smallest consumer, and the one whose fail-open is easiest to test). `tests/test_exec_stream_decode.py`'s `run` case re-points and stays green.
3. Re-point `apps.py:_stream_bench` and `_capture_bench`; then `update.py:_stream_command`, collapsing its verbose/non-verbose fork into a consumption mode. `tests/test_apps.py` stays green **unchanged** - that is the refactor's proof.
4. Delete `decode_exec_stream` once its last caller is gone. If anything still imports it, step 3 is incomplete.
5. `core/run.py`: `run_plan` + `RunPlan` + unit tests. **Report the zero-new-primitives verdict** (Decision 8).
6. Reseat `commands/run.py` fully over `run_plan` + `exec_stream`; add its first command unit tests.
7. `run`'s both-modes E2E, including the >32KB unicode leg.
8. Docs, skills, CHANGELOG.

## Open Questions

- **The poll bound's value** (Decision 7). The contract is settled (never coerce an unknown to success; never hang); the number is an implementation choice to be pinned by a boundary test.
- **Whether the reseated `cwcli run` should PRINT the `bench.default_used` warning** it now receives in the envelope (Decision 8). `unlock` prints its equivalent; `run` is silent today. Additive either way, so it is a rendering call for implementation to make and report.
- **`run`'s argv passthrough** (proposal Non-Goals). `README.md:1330` is corrected here, but the underlying defect - typer claiming the argv before `run` sees it - is a CLI-surface fix (`allow_extra_args`/`ignore_unknown_options`, or making `--` mandatory) deserving its own item. Worth filing regardless of this batch's outcome.
- **Whether `init`'s dead ENOSPC hint should be restored via a retained tail buffer in the primitive** when `init` migrates (proposal Non-Goals). A behavior improvement, deliberately not smuggled in here.
