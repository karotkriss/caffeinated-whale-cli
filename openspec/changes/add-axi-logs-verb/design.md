# Design: `cwcli axi logs` over a bounded `core.read_logs`

This change is small in surface (one core function, one verb, one guard) but it settles four
choices that would otherwise be re-litigated at implementation time. Each is resolved with a
recommendation below.

The evidence base is a real read of `core/logs.py`, `commands/logs.py`, `commands/axi.py`, and
`utils/toon.py` (the four files the verb sits between), plus `core/supervision.py`'s log helpers.

---

## Decision 1 - `core.read_logs` shape

**Recommendation: a bounded `tail -n N` over the SAME bench per-process log files `logs_plan`
resolves, run INSIDE the core as one buffered `container.exec_run`, returning a typed
`Result[LogsRead]` of per-process line groups.**

### Log source

The source is the bench's per-process supervisord log files (`{bench}/logs/<program>.supervisor.log`),
with the identical not-cwcli-supervised fallback to the bench's real `*.log` files that `logs_plan`
already implements (`core/logs.py:198-211`). It is **NOT** `docker logs` and **NOT** container logs.

- Consistency: `cwcli logs`, `cwcli status`, and `axi status` all speak in per-process bench logs
  (`core/supervision.py`). A `docker logs` surface would be a second, contradictory notion of "logs"
  on the same tool, and an agent that ran `axi status` (per-process) then `axi logs` (container) would
  get two incompatible views.
- Reuse: the entire resolve already exists in `logs_plan`. `read_logs` should share it verbatim, not
  re-derive a different source.

### Signature

```python
def read_logs(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    process: str | None = None,
    lines: int = 100,
    auto_start: bool = False,
) -> Result[LogsRead]:
```

This is `logs_plan`'s signature **minus `follow`** (bounded, no follow) and minus nothing else. Default
`lines=100` matches `cwcli logs`' default (`commands/logs.py:42-47`).

### How the tail runs (and why in the core, unlike `logs_plan`)

`logs_plan` keeps its tail in the frontend because `--follow` is an interactive TTY stream cwcli merely
awaits (`core/logs.py:3-33`). A **bounded** `tail -n N` is the opposite: it terminates, its bytes are
finite, and an agent needs them AS DATA. So `read_logs` runs the tail itself with one buffered
`container.exec_run(["tail", "-v", "-n", str(lines), *log_files])` - the `core.backup` shape (a buffered,
blocking `exec_run` that returns real output and a real exit code), NOT `exec_stream` (which is for
streaming ops whose bytes flow through Python event-by-event). This keeps `read_logs` coverable by the
same `exec_run`-interpreting container fakes the rest of `core/logs.py` uses.

`tail -v` forces the `==> <path> <==` header even for a single file, so the combined and single-process
outputs parse uniformly (map each header's path back to its program). Splitting on those headers gives
per-process groups without inventing a sentinel delimiter.
`ponytail:` `tail -v` headers are the parse boundary; a log line literally equal to `==> <path> <==`
would mis-split. Upgrade path if that ever bites: one `exec_run` per file instead of one combined call.

### Return DTO (no live Docker object crosses the boundary)

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessLog:
    process: str          # normalized Procfile label, e.g. "web", "worker_default"
    file: str             # absolute path tailed
    lines: list[str]      # the tail, oldest-first, one string per line

@dataclass(frozen=True, slots=True, kw_only=True)
class LogsRead:
    project: str
    container_name: str
    bench_path: str
    lines_requested: int
    not_cwcli_supervised: bool
    logs: list[ProcessLog]
```

Every field is serializable; no `Container` and no argv cross the return boundary, exactly as `LogsPlan`
forbids (`core/logs.py:53-72`).

### Shared-resolve extraction (the lazy, root-cause move)

`read_logs` and `logs_plan` resolve the identical thing. Rather than duplicate ~90 lines, extract a
private `_resolve_log_files(...)` that both call, returning `(container, bench_path, ordered log_files,
not_cwcli_supervised, warnings)` or a `NEEDS_CHOICE`/raise. `logs_plan` then wraps it into a `LogsPlan`
(adding `follow`/`lines` for the frontend tail); `read_logs` runs the bounded tail over the same files.
One resolve, two consumers - the `backup`/`unlock` twin-caller precedent.

---

## Decision 2 - Output shape

**Recommendation: one TOON document - a metadata head, then one raw-line block per process.**

`toon.encode(asdict(dto))` is WRONG for log lines: a `list[str]` field encodes to an inline
comma-joined scalar list (`utils/toon.py:108-110`), which mangles multi-line logs. Log lines must be
emitted with `toon.block(name, lines)`, which puts one RAW string per indented line - exactly the shape
`help`/`notes` blocks already use (`commands/axi.py:868-882`). So the verb renders explicitly (like
`axi setup` does) rather than via `emit_result`.

```
project: myproj
bench_path: /workspace/frappe-bench
container: myproj-frappe-1
not_cwcli_supervised: false
lines_requested: 100
web[3]:
  10:31:02 web.1  | 127.0.0.1 - "GET /api/method/ping"
  10:31:03 web.1  | 127.0.0.1 - "GET /app"
  10:31:05 web.1  | 200 OK
worker_default[2]:
  10:31:04 worker.1 | executing job frappe.email.queue
  10:31:06 worker.1 | job complete
```

- With `--process web`, only the `web[N]:` block appears (single file).
- Each process name is a safe TOON key (`web`, `worker_default`, `schedule`, ...); log-line CONTENT is
  emitted raw under the block, so a colon or comma inside a line cannot corrupt the document (block
  lines are never re-parsed as key:value).
- Progress/diagnostics (the `not_cwcli_supervised` note, verbose warnings) go to stderr; stdout is pure
  TOON, the one-document contract (`commands/axi.py:9-22`).

### Flags

`--bench <index|label>`, `--lines/-n <N>` (default 100), `--process/-p <label>`. Deliberately NO
`--follow` (a follow cannot terminate into one document) and NO `--yes` (no auto-start on the agent
surface - `axi inspect`'s stated rule, `commands/axi.py:519-525`).

### Exit codes

| Outcome | Exit | Rendering |
| --- | --- | --- |
| Logs found | 0 | the document above |
| Running-but-quiet bench (manager up, no logs yet) | 0 | empty `logs`, a `logs.none_yet` warning line (see Decision 3) |
| Multi-bench, no `--bench` | 2 | `error: multiple benches; pass --bench <index\|label>` + `options[N]:` + `help:` |
| Unknown/ambiguous `--process` | 2 | `error: unknown or ambiguous process; pass --process <label>` + valid labels |
| Stopped container | 2 | `error: <confirm_start prompt>` + `help: start it first with 'cwcli start <project>'` |
| Container up, bench has no live manager | 1 | `error: <logs.no_manager message>` + `help: ... cwcli start <project>` |
| `NOT_FOUND`/`DOCKER`/other `CwcliError` | 1 / `exit_for(kind)` | `error:`/`help:` lines |

The three usage rows reuse the existing `emit_axi_choice_as_usage_error` machinery
(`commands/axi.py:93-109`), which already renders `select_bench`, `select_process`, and `confirm_start`.
No new choice kinds, no new emitter.

---

## Decision 3 - Stopped-bench handling

**Recommendation: a stopped CONTAINER is a usage error (exit 2) naming `cwcli start`; a running-but-quiet
bench is a successful EMPTY read (exit 0); a running container whose bench has no live manager keeps
`logs_plan`'s existing `logs.no_manager` error (exit 1).**

Three distinct not-found-ish states, kept distinct:

1. **Container stopped.** `read_logs(auto_start=False)` returns `NEEDS_CHOICE`/`confirm_start` (the shared
   run-state fork every bench verb hits, `resolvers.resolve_container_state`). The verb renders it as the
   usual usage error, exit 2 naming `cwcli start`. This is the consistency the task asks for and matches
   `axi backup`/`axi unlock`/`axi inspect`/`axi apps list` verbatim - the agent's next move is
   `cwcli axi start`, not a retry.

2. **Container up, manager up, no logs yet.** Today `logs_plan` RAISES `NOT_FOUND`/`logs.none_yet`
   (`core/logs.py:242-248`) because for a human an empty interactive tail is surprising. On the AGENT
   surface, a read that successfully determines "there is nothing yet" has not failed - the precedent is
   `axi self-update --check` (exits 0 while reporting "you are outdated") and `axi status` (exits 0 while
   reporting a fully offline project) and `axi ls` (exits 0 with "0 instances found"). So `read_logs`
   returns `Result(OK, LogsRead(..., logs=[]))` with a `logs.none_yet` warning instead of raising, and
   the verb exits 0. This DIVERGES the two frontends over one core exactly as `self-update --check`'s
   exit code diverges - the human `cwcli logs` keeps its exit-1 error. To make this clean, the shared
   `_resolve_log_files` returns the empty/no-manager facts as data; `logs_plan` maps them to its
   historical raises (byte-exact human behaviour), `read_logs` maps "manager up, no files" to empty-OK.

3. **Container up, bench has NO live manager** (no supervisord and no honcho/`bench start` running the
   bench). `logs_plan` raises `NOT_RUNNING`/`logs.no_manager` with a `cwcli start` hint
   (`core/logs.py:249-254`). `read_logs` keeps that raise; the verb maps `NOT_RUNNING` via `exit_for` to
   exit 1 and prints the message + hint. This is honestly an ERROR (you asked for logs of a bench that is
   not running) rather than a flag-usage problem - the container IS up, so it is not the shared
   confirm_start fork, and exit 1 is cwcli's "error" code. Distinguishing it from the stopped-container
   exit-2 preserves logs' existing two-outcome design (`_raise_no_logs`) rather than inventing a new
   mapping.

Why not serve "whatever is available" and always exit 0? Because case 3 has NOTHING available and the
honest answer is "the bench is not running" - collapsing it into a silent empty-0 would hide a real
failure state an agent must act on differently (start the bench) from case 2 (wait / the process is
quiet).

---

## Decision 4 - The guard

**Recommendation: replace the never-written absence assertion with a PRESENCE assertion, and let the
generated skill list the verb.**

There is no absence test to flip: `axi logs` was deferred by prose (`CLAUDE.md`'s logs entry), not by a
test - that missing guard IS `cwcli-axi-deferral-guards-g4`'s `logs` half. Building the verb closes it:

- Add `tests/test_axi_logs.py` asserting `"logs" in {c.name for c in axi_mod.app.registered_commands}`
  (the mirror of `test_axi_apps_list.py:141-145`'s absence assertions).
- The installable skill's verb table is generated by `build_skill.py` walking Typer's own
  `registered_commands` (`CLAUDE.md`'s AXI-shell entry), so `axi logs` appears automatically once
  registered; `tests/test_axi_skill.py`'s `--check` unit test keeps it from drifting.
- Nothing in `test_axi_skill.py:132`'s absent-verb parametrization changes - `axi logs` was never in it,
  and the still-deferred `axi restore` / `axi apps install` / `axi apps uninstall` stay listed.

This subsumes g4's `logs` half by making the deferral moot: the verb ships, so "is it deliberately
absent?" is no longer a question a future agent can get wrong.

---

## Alternatives considered

- **Return a single combined `text` blob instead of per-process groups.** Rejected: it drops process
  attribution (an agent debugging a `FATAL` worker wants that process's lines), and a multi-line scalar
  is awkward in a line-oriented format. Per-process blocks cost the same one exec.
- **Re-point the tail onto `exec_stream` with a bounded consumer.** Unnecessary: `exec_stream` is for
  ops whose bytes flow through Python event-by-event; a bounded `tail -n N` is a single buffered
  `exec_run` (the `backup` precedent), simpler and already the coverable idiom in this module. The
  module docstring's own note that `exec_stream` is "the right primitive for a future bounded
  `core.read_logs`" refers to a streaming `tail -n N` if one were ever wanted; a one-shot buffered read
  needs no event iterator.
- **Add `--yes` to auto-start a stopped bench.** Rejected: every bench-scoped `axi` verb refuses to open
  a start-from-axi path (`axi inspect`'s explicit rule); an agent composes `cwcli axi start` then
  `cwcli axi logs`.
