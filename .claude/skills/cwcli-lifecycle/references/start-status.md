# start / status / logs / restart - the supervisord supervision substrate (sharp edges)

Migrated onto the UI-pure core by `migrate-start-status-core`, then re-pointed from honcho to
**supervisord** by `add-per-process-supervisor` (REVERSING that change's D1 "keep honcho"; captain-
approved). cwcli launches supervisord in-container over the bench's dev Procfile and owns the READ side
plus discrete mutations via `docker exec`; it is never the live parent across its own invocations, so it
cold-re-discovers the supervisord every call. Per-process restart + auto-heal - the honcho NON-GOAL -
are now the whole point. Each note below guards a real bug.

## `core/supervision.py` - the one tracked-state contract (start writes, status reads)

- **Discovery is keyed to a RESOLVED bench path, not a pattern.** One `ps -eo pid,ppid,etimes,pcpu,rss,args`
  maps PIDs to Procfile labels from their self-describing cmdlines; the supervisord for a bench is found
  by its `-c <bench>/logs/.cwcli-supervisor.conf` arg (self-describing) or, as a fallback, its cwd
  (`readlink /proc/<pid>/cwd`). This keys a multi-bench instance so bench B's processes never attribute to
  bench A. `label_for`, `discover_stack`'s tree-walk, and `expected_labels` are REUSED verbatim from the
  s4 substrate (the children - `bench serve`/`worker`/`node ...socketio`/`redis-server ...` - are
  unchanged); only the supervisor detection + launch changed.
- **The config + launcher are generated from the live Procfile, not baked in.** `render_config` emits one
  `[program:<Procfile-key>]` per line whose `command` is `bash "<launcher>" <key>`; the launcher
  (`{bench}/logs/.cwcli-run.sh`, `_LAUNCHER_SRC`) sources `.env`, greps that program's command out of the
  live Procfile, and `exec`s it - so PATH resolution, shell redirections (`bench worker 1>> ...`), and
  honcho's `.env` auto-load all still work, and the command never goes through the supervisord INI (no
  quoting hell). supervisord `directory=<bench>` gives the launcher its cwd. Program names are the RAW
  Procfile keys (`worker_default`); discovery/CLI labels are normalized (`worker:default`) - `program_for_label`
  bridges them (accepts either), `states_by_label` re-keys `supervisorctl_states` for the status merge.
- **supervisor is installed into the bench venv on first supervise, FAIL-CLOSED.** supervisord is not in
  the frappe dev image. `ensure_supervisor_installed` skips if `import supervisor` already works
  (idempotent), else `<bench>/env/bin/python -m pip install supervisor`; on failure it RAISES
  `CwcliError(PRECONDITION, "supervisor.install_failed")` - it must NEVER silently fall back to honcho
  (that would leave the user thinking they have per-process supervision when they do not).
- **The supervisor marker distinguishes started-then-down from never-started.** supervisord's control
  socket is gone once it dies, so pure discovery cannot tell "container restarted, supervisord gone" from
  "never started". `core.start` writes `{bench}/logs/.cwcli-supervisor.json` (`{supervisor:"supervisord",
  started_at, config_path, log_path}`) on a real launch; `core.status` reads it. The idempotent no-op path
  must NOT rewrite it (it would lose `started_at`) - only a real launch writes.
- **Logs are per-process files, not one combined stream.** Each program writes `{bench}/logs/<program>.supervisor.log`
  via supervisord `stdout_logfile` + built-in rotation (`stdout_logfile_maxbytes`/`_backups`) - the honcho
  combined-stream capper and the prefix-parse are RETIRED. `commands/logs.py` resolves per-process paths from
  the substrate: `--process <label>` tails one file, otherwise it tails every program's file for a combined
  view (the multi-file `tail`, which also supports `--follow`). A program that
  has produced no output has no file yet - `logs.py:_existing_files` filters to existing paths so `tail`
  does not error.
- **`cwcli logs` not-cwcli-supervised FALLBACK (regression fix - same class as the status one).** The
  supervisord path builds its file list purely from `supervision.process_log_path` (`<program>.supervisor.log`).
  A bench running under honcho / `bench start` (pre-v3, or a plain `bench start`) has NONE of those files, so
  `_existing_files` came back empty and `logs.py` falsely errored `No process logs found ... The bench may not
  be running` on a bench that WAS up with real logs. Fix: when the supervisord log files are absent, ask
  `discover_unsupervised_stack` if a honcho/bench-start manager is live for the bench; if so, DISCOVER the real
  `{bench}/logs/*.log` files (`_discover_bench_log_files` globs the dir - honcho log names differ from
  supervisord's, so NEVER assume `<program>.supervisor.log`; excludes `.supervisor.log`, and the `*.log` glob
  skips cwcli's `.cwcli-*` dotfiles) and tail them, with `--process` filtered by file stem
  (`_program_log_matches`: `web`->`web.log`/`web.error.log`, `worker_default`->`worker.log`,
  `schedule`->`scheduler.log`, `_`/`-` treated alike). It is a PURE READ - `cwcli logs` must NOT
  launch/install/restart supervisord or resurrect a deliberately-honcho-run bench (captain's
  read-commands-don't-mutate rule); the `ensure_containers_running` call stays container-running detection only.
  The supervisord path is byte-for-byte unchanged (the fallback triggers ONLY when the `.supervisor.log` files
  are absent), and the "may not be running" hint now shows only when NEITHER supervisord nor honcho is running,
  so a genuinely-running-but-unsupervised bench no longer reads as down.

## `core/start.py` - idempotency (D4) + the autorestart toggle

- **Idempotent by DISCOVERED supervisord PID.** An already-running supervisord for the resolved bench is a
  clean no-op (`already_running=True`, exit 0); it does NOT relaunch or rewrite the config/marker (so the
  running autorestart state is left intact).
- **`restart=True` forces a genuine relaunch.** The post-restore restart (`commands/restore.py`) needs the
  app to reconnect to the restored/migrated DB, so it passes `restart=True`, which `stop_supervisor`s the
  supervisord (SIGTERM by PID -> supervisord shuts its program group down cleanly, bounded-wait, then
  SIGKILL) BEFORE relaunching. `cwcli restart` (whole-stack) does NOT need it (its `stop` already killed the
  container's supervisord, so `start` launches fresh).
- **`--autorestart`/`--no-autorestart` is config STATE set at launch, not a loop.** `core.start(autorestart=...)`
  threads into `render_config`: `autorestart=unexpected` (restart on crash, not a clean exit) + `startretries`
  backoff (a crash-loop reaches supervisord's visible `FATAL`, not a silent hot loop) + `stopasgroup`/`killasgroup`
  (grandchild cleanup). Only a genuine (re)launch regenerates the config; the no-op leaves it untouched.
- **Container start is unconditional (no `confirm_start` fork).** Port-conflict resolution stays a
  CLI-frontend host-side pre-step (D6); `core.start` assumes ports are clear. The human frontend skips the
  port check when the frappe container is already up, which lets an idempotent re-run reach the no-op
  instead of self-conflicting on its own ports.

## `core/restart.py` - single-program restart (the per-process feature)

- **`core.restart_process(project, label, bench=..., bench_path=...)`** restarts ONE program
  (`supervisorctl restart <program>`) leaving siblings running; returns `ProcessRestartOutcome`
  (`label`, `old_pid`, `new_pid`, `supervisor_state`). The bench must be supervised (supervisord up) or it
  raises `NOT_RUNNING("supervisor.not_running")`; a down container raises `NOT_RUNNING("container.not_running")`.
- **Unknown/ambiguous `--process` returns `NEEDS_CHOICE` `select_process`** listing the valid labels (from
  the live `supervisorctl status` keys), NEVER a prompt; multi-bench with no selector returns the shared
  `select_bench`. The CLI frontend (`commands/restart.py`) prompts (TTY) / errors listing valid labels
  (non-TTY); `cwcli axi restart` renders both as usage errors (exit 2). `--process` is REQUIRED on the axi
  verb; whole-stack restart is not an axi verb.

## `core/status.py` - the stdout-token-only contract + per-process state

- **The human `cwcli status` prints ONLY the `overall` token on stdout** (`offline`/`online`/`running`/
  `degraded`); the per-process breakdown + web probe go to stderr. The E2E net asserts `stdout.strip() ==
  "running"`/`"offline"`, so any per-process detail on stdout breaks it. `cwcli axi status` is the TOON surface.
- **`overall` DROPS honcho's all-or-nothing assumption.** supervisord keeps siblings alive when one dies, so
  a STABLE partial stack is real: `running` now requires supervisord up AND every expected program healthy
  (`RUNNING`/`STARTING`) AND (when probed) web answering; a program `FATAL`/`BACKOFF`/`EXITED`/`STOPPED`/down
  while the rest serve is honestly `degraded`. The four tokens are kept (a `FATAL` folds into `degraded`, no
  fifth token - that would churn the E2E net and the axi contract); the crash detail rides each process's
  `state` field. Per-program `state` comes from `supervisorctl_states` (over the unix socket), which
  distinguishes a crash-looping `BACKOFF` and give-up `FATAL` from a clean down - detail `ps` alone cannot.
  `online` = container up, no marker (never started). `offline` = a real-but-stopped project - RETURNED,
  never raised. A truly-nonexistent project RAISES `NOT_FOUND`; a dead daemon RAISES `DOCKER`.
- **Not-cwcli-supervised FALLBACK (regression fix - v3 broke this).** v3 switched detection to supervisord-ONLY,
  so a bench running under honcho / a plain `bench start` (how EVERY instance started before v3 looks) found no
  supervisord, walked no tree, and falsely reported EVERY process `down`. Fix: when `discover_stack` finds no
  cwcli supervisord, status falls back to `supervision.discover_unsupervised_stack` - the SAME `ps` +
  `label_for` + `_descendants` machinery walking the honcho / `bench start` process tree (keyed by honcho's
  `/proc/<pid>/cwd == bench`, the same cwd fallback supervisord keying uses). It reports each process's TRUE
  `up`/pid/uptime from `ps`; the per-process supervisord `state` is `None` here (there is no supervisord to ask -
  correct: `up` is the observable truth). The report is flagged `StatusReport.not_cwcli_supervised=True` with a
  `supervisor.not_cwcli` hint warning ("run `cwcli start`..."), the heading reads `(not under cwcli supervision)`
  not `(supervisor down)`, and `overall` is the honest `running`/`degraded` off the real processes. It is a PURE
  READ - status NEVER launches supervisord or mutates the instance (captain decision; migrating is `cwcli start`'s
  job) - and applies to `status`, `status --watch`, and `axi status`. **`discover_stack` stays supervisord-ONLY**
  (its `supervisor_up` gates `start` idempotency + `restart`'s precondition - do NOT reroute it through the
  fallback, or start/restart mistake a honcho bench for a cwcli-supervised one).
- **`label_for` must match the runtime `frappe <cmd>` form, not just `bench <cmd>`.** A Procfile `web: bench serve`
  resolves at runtime (once the `bench`/`sh -c` wrapper execs away) to `python -m frappe.utils.bench_helper frappe
  serve` - so `label_for` matches BOTH `bench serve|schedule|watch|worker` AND `frappe serve|schedule|watch|worker`
  (worker already had the dual form). Miss this and a genuinely-serving web/watch/schedule reads as `down` under
  the supervisord path too (the fakes used `bench serve` cmdlines and masked it; `test_real_bench_helper_cmdlines_map_to_labels` guards it now).
- **`cwcli status --watch` must NOT probe the web server** (the load-bearing reason the flag exists).
  `probe_web=False` suppresses the `curl localhost:8000` call (which is what spams the bench's access logs);
  per-program health still comes from `ps` + `supervisorctl` (the control socket, NOT :8000), so repeated
  ticks leave ZERO HTTP requests. With `probe_web=False`, `_overall` is driven by supervisor-up + all-healthy
  (a missing web code must NOT falsely `degrade`). `--watch` is a frontend re-poll (`commands/status.py:_watch_loop`,
  `rich.Live` on stderr) - the core stays one-shot. The loop starts only when BOTH stdout+stderr are TTYs;
  otherwise one quiet snapshot. `--interval` floors at 1s; Ctrl-C exits 0. `cwcli axi status` stays one-shot.

## Multi-bench (D5), the axi verbs, and the container-boot NON-GOAL

- `start`/`status`/`restart --process` share `resolvers.resolve_bench`; on multi-bench with no `--bench` the
  human CLI prompts (`on_ambiguous="prompt"`, TTY) and refuses non-zero on a non-TTY. The internal
  `_start_project` (restart / auto-start callers) keeps the lenient first-bench fallback. `cwcli axi
  start`/`status`/`restart` take `--bench` or emit a `select_bench` usage error naming it. `cwcli axi start`
  never prompts a port conflict: a `CONFLICT` naming `--yes` via the non-printing `detect_port_conflicts`.
- **Container-boot auto-relaunch is an explicit NON-GOAL.** supervisord's lifecycle is the container's
  lifecycle: it heals crashed PROGRAMS on its own, but it is NOT auto-relaunched when the container itself
  restarts (that needs the image entrypoint, which cwcli cannot set via `docker exec`). A `cwcli start` is
  still required after a container restart - unchanged from honcho.
