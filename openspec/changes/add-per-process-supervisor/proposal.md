## Why

`cwcli` cannot restart a single dead Procfile worker while the rest of the bench keeps running, and it cannot heal a crashed worker automatically.
Both gaps have the same root cause: `bench start` runs the dev Procfile under **honcho**, and honcho is all-or-nothing.
When any child process exits, honcho SIGTERMs every sibling and exits itself, and it never restarts a crashed process (proven first-hand in the recon `data/cwcli-proc-supervisor-recon-v9/report.md` §2: the healthy `alpha` was killed the instant `beta` exited rc=3; `beta` was never restarted).
There is therefore no stable "web up, worker dead" state to repair - by the time anything notices a dead worker, honcho has already torn the rest of the stack down.

This is exactly the NON-GOAL the previous `migrate-start-status-core` change locked (its D1: "observe, keep honcho; NO in-container supervisor").
That change was right that per-process restart is unreachable under honcho; it deferred the feature rather than solving it.
Delivering per-process restart with auto-heal **requires reversing D1** and putting a real per-process supervisor inside the container.
The captain has approved that reversal.

The "cheap augment" that keeps honcho and respawns one Procfile entry via `docker exec` **does not exist**: keeping honcho means the siblings are already dead by the time you respawn one, and the respawn is an orphan honcho neither owns nor tears down.
It is recorded here as rejected-because-impossible, not deferred, so it cannot be silently reopened.

## What Changes

- **Replace honcho with supervisord (Architecture Option A).**
  On `cwcli start`, cwcli parses the bench's live Procfile, generates a supervisord config (one `[program:<label>]` per Procfile line), and launches `supervisord` **instead of** `bench start`.
  supervisord runs the *dev* Procfile's commands verbatim (werkzeug reloader, `bench watch`, etc.), so dev semantics are preserved; only the supervisor changes.
  This reverses `migrate-start-status-core` D1 ("keep honcho"); the reversal is the whole point of the feature and is captain-approved.
- **Install supervisor into the bench env on first supervise.**
  supervisord is NOT preinstalled in the frappe image, but `pip install supervisor` into the bench's Python env is clean (verified: 4.3.0, pure-python, ~1.5s, 0 extra transitive deps).
  cwcli installs it idempotently on first supervise (skip if already present) and fails closed with a clear typed error if the install cannot complete (e.g. offline), rather than silently falling back to honcho.
- **Per-process restart, first-class.**
  Add `core.restart_process(project, label, *, bench=None, bench_path=None) -> Result[ProcessRestartOutcome]` that restarts ONE program (`supervisorctl restart <label>`) while every sibling keeps running.
  Wire it to `cwcli restart <project> [--process <label>] [--bench <sel>]`: whole-stack restart when `--process` is omitted (preserves today's `restart`), single-process restart when a `--process` label is given.
  Add the agent verb `cwcli axi restart <project> --process <label>` emitting one TOON document.
- **Auto-heal is IN SCOPE, as supervisord config state (not a foreground loop).**
  The generated config sets `autorestart=unexpected` (restart on crash, not on clean exit), `startretries` + `startsecs` backoff, and `stopasgroup`/`killasgroup` (process-group kill for grandchildren).
  A crash-loop surfaces as supervisord's visible **FATAL** state after `startretries`, rather than silently hammering.
  A `--autorestart` / `--no-autorestart` toggle on `cwcli start` (and `cwcli axi start`) sets this config state at launch.
  There is deliberately NO live `cwcli restart --watch --heal` command: auto-heal is supervisord config STATE, owned by the in-container supervisor, not a cwcli polling loop.
- **Enrich status/health with supervisord's real per-process states.**
  `ProcessHealth` gains a `state` field carrying supervisord's `RUNNING` / `STARTING` / `BACKOFF` / `EXITED` / `FATAL` / `STOPPED`, read from `supervisorctl status` (more accurate than inferring liveness from `ps`, which cannot distinguish a crash-looping BACKOFF from a clean down).
  `overall` stops assuming all-or-nothing: a genuinely stable partial state ("web serving, a worker FATAL") is now a real, honest `degraded`, which honcho made impossible.
  The all-or-nothing rationale baked into `core/status.py` is updated accordingly.
- **Move logs to per-process files.**
  Each `[program:<label>]` writes its own `stdout_logfile` with supervisord's built-in rotation (`stdout_logfile_maxbytes` + `stdout_logfile_backups`), retiring the honcho combined-stream capper (`_LOG_CAPPER_SRC`) and the honcho-prefix parse (`per_process_log_lines`).
  A survivor's log is untouched when a sibling restarts (the combined-stream coherence problem disappears).
  `cwcli logs` gains a `--process <label>` tail of one program's file and synthesizes a combined view on read when no process is named.
- **Update supervisor detection to supervisord, reusing the s4 substrate.**
  The `SUPERVISOR` constant, the `_is_honcho` / `_honcho_pids_for_bench` detectors, and the marker are updated to detect **supervisord-for-this-bench**, keyed by its config path (so a multi-bench instance never mis-attributes another bench's supervisor).
  Everything else in `core/supervision.py` is REUSED, not rewritten: `discover_stack`'s tree-walk, `label_for` (the children - `bench serve`, `bench worker`, `node ...socketio`, `redis-server ...` - are unchanged), `expected_labels`, and the multi-bench keying all carry over.
- **BREAKING**: the in-container supervisor changes from honcho to supervisord; `cwcli status` output gains a per-process `state` field and `degraded` can now reflect a stable partial stack; the captured-log layout moves from one combined `bench-start.log` to per-process files.
  All deliberate, all covered by this change's new tests, floored by the start/status E2E net from `add-start-status-e2e-net`.

## Capabilities

### New Capabilities

- `per-process-supervision`: the supervisord-based in-container supervision model that replaces honcho - Procfile-to-supervisord config generation, the idempotent `pip install supervisor` bootstrap, the auto-heal config contract (`autorestart=unexpected`, backoff, FATAL crash-loop surfacing, the `--autorestart` toggle), per-process log files with built-in rotation and a synthesized combined view, supervisord-keyed supervisor detection/marker reusing the s4 discovery substrate, and the `ProcessHealth`/`overall` enrichment that reports real per-process supervisord states now that stable partial stacks exist.
- `process-restart-slice`: `core.restart_process(...) -> Result[ProcessRestartOutcome]` (single-program restart leaving siblings running, `NEEDS_CHOICE` for an unknown/ambiguous `--process` label and for multi-bench-no-selector), the reseated `cwcli restart <project> [--process <label>] [--bench <sel>]` (whole-stack when omitted, single-process when given), and the `cwcli axi restart <project> --process <label>` verb (one TOON document, one-shot mutation, no `--watch`).

### Modified Capabilities

- (none archived - `openspec/specs/` holds no archived capabilities, the same state `migrate-start-status-core` recorded.
  This change supersedes the honcho "observe, keep honcho" supervision model defined by that still-unarchived change; because that model was never archived into `openspec/specs/`, the supersession is captured as the new `per-process-supervision` requirements above rather than as MODIFIED deltas.
  The reversal of its D1 is called out explicitly in `design.md` so no reader treats "keep honcho" as still-current.)

## Impact

- **New source:** `src/caffeinated_whale_cli/core/restart.py` (`core.restart_process` + the `ProcessRestartOutcome` DTO).
  New helpers in `src/caffeinated_whale_cli/core/supervision.py` for supervisord: config generation from the Procfile, the `pip install supervisor` bootstrap, `supervisorctl` state read + single-program restart, the supervisord launch/stop, and per-process log paths.
- **Modified source:** `core/supervision.py` (`SUPERVISOR` -> `"supervisord"`; `_is_honcho`/`_honcho_pids_for_bench` -> supervisord-keyed detection; `launch`/`stop_supervisor` drive supervisord; the combined-stream capper + `per_process_log_lines` retired in favor of per-process files; the marker records the supervisord config path).
  `core/status.py` (`ProcessHealth.state` enrichment from `supervisorctl status`; the all-or-nothing `overall` rationale updated so a stable partial stack reports `degraded`).
  `core/start.py` (the `--autorestart` config toggle threaded into the generated supervisord config; still idempotent by discovered supervisor PID).
  `commands/start.py` + `commands/axi.py` (the `--autorestart`/`--no-autorestart` flag on `start`; the new `restart` axi verb).
  `commands/restart.py` (the `--process` / `--bench` options over `core.restart_process` for single-process, today's whole-stack path preserved).
  `commands/logs.py` (`--process` per-program tail + synthesized combined read).
- **Reuses (no changes):** `core/resolvers.py` (multi-bench `resolve_bench` `NEEDS_CHOICE`), `core/envelope.py`, `core/errors.py`, `core/docker.py`, `utils/toon.py`, and the `cwcli axi` serializer/exit-mapper.
  The s4 discovery internals (`discover_stack` tree-walk, `label_for`, `expected_labels`, multi-bench keying) are reused, only re-pointed from honcho to supervisord.
- **Tests:** unit tests for supervisord config generation, the pip-install bootstrap (present/absent/failure), `supervisorctl` state parsing, `core.restart_process` (single-program restart, unknown-label `NEEDS_CHOICE`, multi-bench `NEEDS_CHOICE`, sibling-untouched), the enriched `overall`/`ProcessHealth.state`, and per-process log resolution, all against faked exec I/O.
  New real-Docker E2E on the v14/v15/v16 matrix: `pip install supervisor` succeeds and is idempotent; a killed worker auto-restarts while web keeps serving; `cwcli restart --process <label>` restarts one and leaves siblings' PIDs intact; a crash-loop reaches FATAL; per-process logs land in their own files; both interactive and non-interactive (`--process`/`--bench`) modes.
- **Docs:** `README.md` documents the supervisord supervision model, `cwcli restart --process`, the `--autorestart` toggle, the enriched `cwcli status`, and `cwcli axi restart`; `CHANGELOG.md` entry; the `cwcli-lifecycle` skill and root `CLAUDE.md` supervision-substrate notes updated from honcho to supervisord.
- **Dependencies:** one runtime addition installed INTO the bench container's Python env (not into cwcli's own env): `supervisor` (pure-python, installed on first supervise).
  No new dependency in cwcli's own `pyproject.toml`.

## Non-Goals

- **Auto-relaunch of the supervisor on container BOOT (explicit NON-GOAL).**
  Restarting the supervisor when the container itself restarts needs the image entrypoint to launch it, which cwcli cannot set via `docker exec`.
  A `cwcli start` is still required after a container restart - unchanged from the honcho model.
  supervisord's in-container lifecycle is the container's lifecycle: it heals crashed *programs* on its own, but it does not survive a container restart any more than honcho did.
- **A live `cwcli restart --watch --heal` command / an `axi --watch` (explicit NON-GOAL).**
  Auto-heal is supervisord config STATE, not a foreground cwcli loop; a live TUI would also break the one-TOON-document-per-invocation `axi` contract.
- **Option B - respawn one Procfile entry under honcho (rejected-because-impossible, not deferred).**
  honcho kills all siblings the instant one dies and has no "keep going on one death" mode, so there is nothing to keep running alongside a respawned process; recorded rejected so it is not reopened.
- **Option C - a hand-rolled cwcli meta-supervisor over `nohup` + polling (rejected).**
  It re-implements a supervisor out of shell (env loading, process-group kill, a retry/backoff/crash-loop state machine) and pushes auto-heal back into a long-running cwcli loop - exactly the daemon shape s4 rejected.
  supervisord provides all of that, correct and battle-tested, for free.
- **Migrating `logs`/`restart` fully onto the core.**
  They are updated only as needed for per-process logs and the single-process restart verb; their own full core migration is a later change.
