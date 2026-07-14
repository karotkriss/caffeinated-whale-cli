# start / status / logs - the honcho supervision substrate (sharp edges)

Migrated onto the UI-pure core by `migrate-start-status-core`. cwcli owns only the READ side of
bench's own honcho supervisor (D1: observe, keep honcho). There is NO in-container supervisor and NO
cwcli daemon; per-process restart resilience is an explicit NON-GOAL (honcho is all-or-nothing - one
process dies, it tears the rest down and never restarts one). Each note below guards a real bug.

## `core/supervision.py` - the one tracked-state contract (start writes, status reads)

- **Discovery is keyed to a RESOLVED bench path, not a pattern.** One `ps -eo pid,ppid,etimes,pcpu,rss,args`
  maps PIDs to Procfile labels from their self-describing cmdlines; the honcho for a bench is found by
  its cwd (`readlink /proc/<pid>/cwd`) or an explicit `-f <bench>/Procfile` arg. This is what keeps a
  multi-bench instance from mis-attributing bench B's processes to bench A. If you change the launch
  so honcho's cwd is no longer the bench dir, discovery breaks - keep `cd {bench} && bench start`.
- **The supervisor marker distinguishes started-then-down from never-started.** honcho writes no
  pidfile, so pure discovery cannot tell "container restarted, honcho gone" from "never started".
  `core.start` writes `{bench}/logs/.cwcli-supervisor.json` on a real launch; `core.status` reads it.
  The idempotent no-op path must NOT rewrite it (it would lose `started_at`) - only a real launch writes.
- **The log is relocated + bounded (D2).** From the ephemeral `/tmp/bench-<project>.log` to
  `{bench}/logs/bench-start.log` on the workspace volume (survives a container restart). honcho's
  stdout is piped through a python3 stream capper (`_LOG_CAPPER_SRC`, guaranteed present, no new
  dependency, no daemon) that truncates on (re)launch and rotates at a byte cap within a run. The
  capper NEVER exits early or lets a write error propagate - if it did, it would SIGPIPE honcho and
  kill the stack. `commands/logs.py` reads `supervision.bench_start_log_path(bench)` - the single
  source of truth - so the old cross-file `/tmp` duplication is gone.

## `core/start.py` - idempotency (D4)

- **The double-start bug this fixes:** the old `pkill -f 'bench start'` never matched honcho's
  steady-state cmdline (bench `os.execv`s into `honcho start ...`; the children are `bench serve` /
  `worker` / ...), so a re-run did NOT kill the old stack and spawned a SECOND honcho (duplicate
  workers, in-container port clashes). `core.start` now detects an already-running bench by DISCOVERED
  supervisor PID keyed to the resolved bench path and no-ops (`already_running=True`), exit 0.
- **`restart=True` forces a genuine relaunch.** The post-restore restart (`commands/restore.py`) needs
  the app to reconnect to the restored/migrated DB, so it passes `restart=True`, which
  `stop_supervisor`s the honcho (by PID, bounded-wait for it to die, then SIGKILL) BEFORE relaunching -
  never the idempotent no-op. `cwcli restart` does NOT need it (its `stop` already killed the container's
  honcho, so `start` launches fresh).
- **Container start is unconditional (no `confirm_start` fork).** start's whole job is to bring things
  up. Port-conflict resolution stays a CLI-frontend host-side pre-step (D6); `core.start` assumes ports
  are clear. The human `cwcli start` frontend skips the port check when the frappe container is already
  running (its ports are its own), which is what lets an idempotent re-run reach the no-op instead of
  self-conflicting on its own ports.

## `core/status.py` - the stdout-token-only contract

- **The human `cwcli status` prints ONLY the `overall` token on stdout** (`offline`/`online`/`running`/
  `degraded`); the per-process breakdown and the web probe go to stderr. The PR-1 E2E net asserts
  `stdout.strip() == "running"` / `"offline"`, so any per-process detail on stdout breaks the net. Keep
  the token alone on stdout; `cwcli axi status` is the structured (TOON) surface.
- **`overall` is keyed on honcho-up + web-answers, not on every label being individually detected.**
  Because honcho is all-or-nothing, honcho-up already implies the stack is up; keying `running` on the
  reliable web signal keeps `overall` robust across frappe versions' cmdline shapes (the `[verify on a
  container]` risk). `degraded` = marker present but honcho down (container restart), or honcho up but
  web not answering. `online` = container up, no marker (never started). `offline` = a real-but-stopped
  project (containers exist, frappe not running) - RETURNED, never raised, preserving the "offline, exit
  0" contract. A truly-nonexistent project (no containers with the label at all, or none labeled
  `frappe`) instead RAISES `CwcliError(ErrorKind.NOT_FOUND, ...)`, so a typo/never-created name exits
  non-zero with a "no such project" message instead of masquerading as `offline`. Only that and a dead
  Docker daemon (`ErrorKind.DOCKER`) raise.
- **`cwcli status --watch` must NOT probe the web server** (the load-bearing reason the flag exists).
  The one-shot path calls `supervision.web_http_code()` = `curl -s localhost:8000` in-container, and THAT
  is the request that spams the bench's Frappe/werkzeug access logs. So `core.status(..., probe_web=False)`
  suppresses that call entirely (`web_http_code=None`) while per-process health still comes from the one
  `ps` read (which leaves no log trace) - repeated watch ticks must produce ZERO HTTP requests. When
  `probe_web=False`, `_overall` is driven by honcho-up alone (`web_probed=False` short-circuits to
  `running`); a missing web code must NOT falsely `degrade` the aggregate. `--watch` is a **frontend
  re-poll** of the one-shot snapshot on an interval (`commands/status.py:_watch_loop` with `rich.Live` on
  stderr) - the core stays one-shot (the data is `ps`-pull, nothing to stream). Since the live view renders
  to stderr, the loop only starts when BOTH stdout and stderr are TTYs; either one not a TTY (e.g. stderr
  redirected) degrades to a single quiet snapshot (still `probe_web=False`); `--interval` floors at 1s;
  Ctrl-C exits 0 cleanly.
  The plain one-shot `cwcli status` keeps `probe_web=True` unchanged. `cwcli axi status` stays one-shot
  (no `--watch`): a live TUI breaks the one-TOON-document-per-invocation agent contract.

## Multi-bench (D5) and the axi verbs

- `start`/`status` share `resolvers.resolve_bench`; on multi-bench with no `--bench` the human CLI
  prompts (`resolve_bench_path(on_ambiguous="prompt")`, TTY) and refuses non-zero on a non-TTY. The
  internal `_start_project` (restart / auto-start callers) keeps the lenient first-bench fallback since
  it has no `--bench` and runs under a spinner. `cwcli axi start`/`status` take `--bench` or emit a
  `select_bench` usage error naming it. `cwcli axi start` never prompts a port conflict: it surfaces a
  `CONFLICT` error naming `--yes` (which auto-resolves conflicting Frappe projects) via the non-printing
  `detect_port_conflicts` helper (so stdout stays TOON-clean).
