## ADDED Requirements

### Requirement: Per-process discovery maps container PIDs to Procfile labels

The system SHALL provide a shared read-side discovery function that enumerates a bench's process tree in the frappe container with ONE cheap `ps` call and maps each PID to its Procfile label from its self-describing cmdline (`honcho`; `bench serve` -> `web`; `node .../socketio` -> `socketio`; `bench watch` -> `watch`; `bench schedule` -> `schedule`; `bench worker [--queue <q>]` -> `worker`/`worker:<q>`; `redis-server <conf>` -> `redis_cache`/`redis_queue`).
For each discovered process it SHALL report up/down (label present), uptime seconds (`etimes`), CPU% and RSS (KB) - all from the single `ps` call - and it SHALL key discovery to a RESOLVED bench path so a multi-bench instance never mis-attributes another bench's processes.
Discovery SHALL leak no live Docker object across the boundary: it returns serializable data only.

#### Scenario: Live processes map to labels with resource data

- **WHEN** discovery runs against a bench whose honcho stack is up
- **THEN** it returns a per-process set keyed by Procfile label, each with `up`, `pid`, `uptime_s`, `cpu_pct`, and `rss_kb`, from one `ps` call

#### Scenario: A down process is reported absent

- **WHEN** an expected Procfile label has no matching process in the `ps` output
- **THEN** discovery reports that label as `up=False` with no PID/resource data

#### Scenario: Discovery is keyed to the resolved bench

- **WHEN** two benches in one instance each run a honcho stack
- **THEN** discovery keyed to bench A's path reports only bench A's processes, never bench B's

### Requirement: Expected process labels come from the live Procfile

The system SHALL determine which process labels SHOULD be running by parsing the bench's `Procfile` in-container (the source of truth for the expected set), so the health aggregate can distinguish "all expected up" from "some expected down" without a stale snapshot.

#### Scenario: Expected set reflects the bench's Procfile

- **WHEN** the expected-label set is computed for a bench
- **THEN** it lists exactly the labels the bench's Procfile defines (web, socketio, worker[/queue], schedule, watch, redis_cache, redis_queue as gated by the Procfile), read live

### Requirement: The supervisor marker distinguishes started-then-down from never-started

`core.start` SHALL write a minimal supervisor marker on launch recording `{supervisor, started_at, log_path}` under the bench on the workspace volume (so it survives a container restart), and `core.status` SHALL read it.
The marker's presence-plus-live-discovery diff SHALL let status report "started, supervisor now down" (marker present, honcho absent) distinctly from "bench never started" (no marker) - a distinction pure discovery cannot make (honcho writes no pidfile).
The idempotent no-op path of `core.start` SHALL NOT rewrite the marker in a way that loses the original `started_at`.

#### Scenario: Marker present but supervisor down

- **WHEN** `core.status` runs after a container restart left the marker but no honcho process
- **THEN** it reports the bench as started-but-supervisor-down (feeding a `degraded` overall), NOT `online`/never-started

#### Scenario: No marker means never started

- **WHEN** `core.status` runs on a running container that has never had `bench start`
- **THEN** it finds no marker and reports never-started (feeding an `online` overall)

### Requirement: Captured logs persist on the workspace volume and are bounded

The bench-start stream SHALL be captured to `{bench_path}/logs/bench-start.log` on the frappe_docker workspace volume (surviving a container restart), replacing the ephemeral `/tmp/bench-<project>.log`.
The captured log SHALL be bounded so it cannot grow unbounded: a real (re)launch truncates it (bounding growth across restarts) and a fixed byte cap bounds it within a run.
The bound SHALL add no new image dependency and SHALL NOT introduce a cwcli daemon.
The idempotent no-op start (already-running) SHALL NOT truncate a running session's log.

#### Scenario: Log survives a container restart

- **WHEN** the frappe container restarts after `cwcli start`
- **THEN** the captured `bench-start.log` under the bench `logs/` dir is still present (it is on the workspace volume), unlike the old `/tmp` location

#### Scenario: A real relaunch bounds the log; a no-op does not

- **WHEN** `cwcli start` genuinely relaunches the bench
- **THEN** it starts a fresh bounded session log; **WHEN** `cwcli start` no-ops on an already-running bench, it does NOT truncate the live session log

### Requirement: Per-process log views parse honcho's line prefix

Per-process log views SHALL be recovered by parsing honcho's built-in `HH:MM:SS name|` line prefix in the combined stream, adding no per-process redirect plumbing.

#### Scenario: A process's lines are filtered from the combined stream

- **WHEN** a per-process view for a label (e.g. `web`) is requested from the captured combined log
- **THEN** it is produced by selecting the honcho-prefixed lines for that name, with no separate per-process file required

### Requirement: cwcli logs reads the single-source-of-truth log path

`commands/logs.py` SHALL read the bench-start log path from the shared substrate (one source of truth), NOT re-derive the hardcoded `/tmp/bench-<project>.log`, eliminating the cross-file path duplication.
Because the log is now per-bench, `cwcli logs` SHALL resolve the bench via the shared `resolve_bench_path` wrapper (single-bench unchanged; multi-bench uses the same `select_bench` prompt/`--bench` contract as the rest of the family).
`cwcli logs` and `cwcli restart` SHALL keep working over the relocated log and the reseated start.

#### Scenario: logs follows the relocated log

- **WHEN** `cwcli logs <project>` runs against a started single-bench instance
- **THEN** it tails `{bench_path}/logs/bench-start.log` (resolved from the shared contract), not `/tmp/bench-<project>.log`, and shows the bench output

#### Scenario: restart still works after the reseat

- **WHEN** `cwcli restart <project>` runs (reusing the reseated start path)
- **THEN** the instance stops and restarts, and the site is reachable again - unchanged user-facing behavior
