## Why

**`cwcli axi logs` does not exist, and an agent cannot read a bench's logs without it.**

Every other read on the agent surface has a verb - `axi status` (per-process health), `axi inspect` (benches/sites/apps), `axi apps list` (what is installed) - but the one an agent reaches for when a process is `FATAL` or a migration failed, "show me the log", has no agent-facing form.
The human `cwcli logs` exists, but its whole shape is wrong for `axi`: it is a `docker exec -it ... tail -F` TTY passthrough (`commands/logs.py:141-155`) whose bytes never enter the Python process, streaming indefinitely until Ctrl+C.
An `axi` verb must emit ONE TOON document on stdout and exit; a follow that never terminates cannot.

`core/logs.py`'s own docstring already names the missing piece and pre-approves it (`core/logs.py:31-33`):

> `exec_stream` ... remains the right primitive for a future bounded `core.read_logs` (`tail -n N`, no follow) that an `axi logs` verb would need - a different function, verified fit, deferred with that verb.

The captain approved "build it" (2026-07-17).
This change builds that bounded reader and the verb over it.

**There is deliberately no `axi logs` today, and no test asserting its absence.**
The deferral rationale lives at `CLAUDE.md`'s logs entry ("`logs` is MIGRATED but is a deliberate NON-consumer of [the exec-stream] contract ... `exec_stream` IS the right primitive for a future bounded `core.read_logs` ... a different function, deferred with that verb").
Unlike `axi restore` / `axi apps install` / `axi apps uninstall`, whose absences ARE pinned by tests (`test_axi_skill.py:132`, `test_axi_apps_list.py:135`), the `logs` absence has no guard - that gap is tracked as `cwcli-axi-deferral-guards-g4`, and **building this verb subsumes its `logs` half**: the deferral becomes moot because the verb ships, and a presence assertion replaces the never-written absence one.

## What Changes

- **ADD `core.read_logs(project_name, *, bench=None, bench_path=None, process=None, lines=100, auto_start=False) -> Result[LogsRead]`** to `core/logs.py`: a bounded `tail -n N` (NO follow) that runs the tail INSIDE the core as one buffered `container.exec_run` and returns the captured lines as serializable data. No live Docker object crosses its return boundary (the settled core rule).
- **EXTRACT the shared resolve.** `read_logs` and the existing `logs_plan` resolve the identical thing (container, run-state fork, bench, `--process` selection, existence probe, not-cwcli-supervised fallback). That resolve moves into one private helper both call; `logs_plan` keeps returning a declarative `LogsPlan` for the frontend's tail, `read_logs` reads the same files itself. Both stay PURE READS that launch nothing.
- **ADD the `cwcli axi logs <project>` verb** to `commands/axi.py`: flags `--bench`, `--lines/-n` (default 100), `--process/-p`; emits ONE TOON document (metadata head + per-process log blocks); NO `--follow` (structurally impossible for a one-document verb) and NO `--yes` (no auto-start on the agent surface, matching every bench-scoped `axi` verb).
- **FLIP the guard.** `logs` moves from "deliberately absent, untested" to "shipped, asserted present" - a `test_axi_logs.py` proves the verb is registered and the generated skill (`build_skill.py` walks `registered_commands`) lists it. This closes the `logs` half of `cwcli-axi-deferral-guards-g4`.

## Design questions resolved (recommendations in `design.md`)

1. **`core.read_logs` shape** - bench per-process supervisord log files (the SAME source `cwcli logs` reads, with the identical honcho/`bench start` fallback), NOT `docker logs`; default `lines=100`, `--lines/-n` flag; returns `Result[LogsRead]` carrying per-process line groups as plain strings.
2. **Output shape** - one TOON document: a metadata head (`project`, `bench_path`, `container`, `not_cwcli_supervised`, `lines_requested`) then one raw-line block per process; exit 0 on a successful read, exit 2 usage errors naming the flag (`--bench`, `--process`) or `cwcli start`.
3. **Stopped-bench handling** - a stopped container is a usage error (exit 2) naming `cwcli start`, consistent with `axi backup`/`axi unlock`/`axi inspect`/`axi apps list`; a running-but-quiet bench (manager up, no logs yet) is a successful EMPTY read (exit 0), diverging from the human command's error exactly as `axi self-update --check` diverges its exit code.
4. **Guard** - flip `logs` from deferred to shipped: assert the verb is PRESENT (there was never an absence test to flip), and let the generated skill list it.

## Non-goals

- **NO `--follow` / streaming.** A follow cannot emit one terminal document; `read_logs` is bounded by construction. A future streaming form is a separate decision and a separate function.
- **NO `docker logs` / container-level logs.** cwcli's entire logs model is per-process BENCH logs (`core/logs.py`, `core/supervision.py`); a container-log surface would be a second, inconsistent notion of "logs".
- **NO mutation.** `read_logs` never launches, installs, or restarts supervisord or the bench, on any path including the fallback - identical to `logs_plan`.
- **NO change to `cwcli logs` or `logs_plan` behaviour.** The human command keeps its exact tail, exit codes, and messages; only the shared resolve is extracted beneath it.
