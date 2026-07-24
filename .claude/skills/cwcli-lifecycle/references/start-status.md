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
  has produced no output has no file yet - `core.logs._existing_files` filters to existing paths so `tail`
  does not error.
- **`logs` MIGRATED onto the core (batch 6, `core/logs.py`), and the tail deliberately did NOT.** The
  resolve (container/bench/program selection, the existence probe, the fallback discovery, `_existing_files`
  and `_discover_bench_log_files`) moved to `core.logs_plan(...) -> Result[LogsPlan]`, and the two reads
  re-pointed from a `docker exec` shell-out to `container.exec_run` (so they are now covered, not
  monkeypatched away). `commands/logs.py` is a renderer that performs the `docker exec -it ... tail` ITSELF.
  The tail is NOT re-pointed onto `core.exec_stream`, and this is measured not asserted
  (`openspec/changes/migrate-logs-core/design.md` Decision 1): `logs --follow` is streamed by `tail`,
  relayed by docker's TTY, and merely awaited by cwcli, so locked decision 4 (streaming ops return typed
  event iterators) does not reach it - the bytes never enter the Python process. Probed against real Docker:
  `exec_stream` + Ctrl+C leaks an orphan `tail -F` per invocation (accumulating; Docker has no kill-exec API,
  closing the socket does not kill the exec'd process), `_poll_exit_code` then raises `exec.stream_lost`
  after ~10s (it sees `Running: True`), and `tty=True` is mutually exclusive with the locked `demux=True`
  stream tag. The `-it` path it keeps is verified clean (exit 130, zero orphans under a pty). An unknown
  `--process` is now a `select_process` `NEEDS_CHOICE` (mirrors `core.restart_process`), rendered by the CLI
  as the identical error + valid-label list, exit 1. **PR #83's exit-code fix is preserved in the frontend**
  where its mechanism lives (the propagated `returncode`, the `130`-is-a-clean-Ctrl+C branch, the
  `except KeyboardInterrupt`); do NOT move it into the core - `subprocess` is banned there, and moving it
  means moving the tail. The bounded `core.read_logs` (`tail -n N`, no follow) SHIPPED (`add-axi-logs-verb`)
  behind `cwcli axi logs`, sharing this module's resolve via `_resolve_log_files` - and it is deliberately
  NOT an `exec_stream` consumer: a bounded read blocks to completion and returns finite output, so it is one
  buffered `container.exec_run` (the `core.backup` shape), not an event stream. See the exec-stream contract
  entry in `AGENTS.md`. **The non-TTY `logs -f` orphan leak is FIXED** (`cwcli-logs-orphan-tail-o5`):
  on that path only (no `-it` -> nothing forwards `^C` to `tail`), the tail is wrapped as `sh -c 'echo $$ >
  pidfile; exec tail ...'` so the exec'd tail inherits the wrapper shell's PID, recorded to a unique
  per-invocation pidfile under the container's `/tmp`; a `finally` block (covering both `KeyboardInterrupt`
  and a normal return) reaps that PID with a fresh `docker exec ... kill $(cat pidfile); rm -f pidfile`.
  Best-effort - a missing PID or an already-dead tail is a harmless no-op, and any reap failure is swallowed
  so it never masks the real exit code. The `-it` path is untouched (already verified clean). See
  `commands/logs.py:_kill_container_tail`.
- **`cwcli logs` not-cwcli-supervised FALLBACK (regression fix - same class as the status one).** The
  supervisord path builds its file list purely from `supervision.process_log_path` (`<program>.supervisor.log`).
  A bench running under honcho / `bench start` (pre-v3, or a plain `bench start`) has NONE of those files, so
  `_existing_files` came back empty and `logs` falsely errored `No process logs found ... The bench may not
  be running` on a bench that WAS up with real logs (now `core.logs`; the two distinct outcomes are typed
  errors: `logs.none_yet` NOT_FOUND when a manager is up, `logs.no_manager` NOT_RUNNING with the start hint).
  Fix: when the supervisord log files are absent, ask
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
- **Re-aligns the container's `frappe` user to the host uid/gid on every launch** (`align_container_user_to_host`, with no `chown_home`, so only cheap `usermod`/`groupmod` runs on capable hosts).
  A container recreation resets `frappe` back to the image's default uid 1000, so re-aligning here keeps the bind-mounted workspace host-owned across restarts on hosts with uid/gid information.
  This is the root-cause fix for `cwcli rm`'s CI-only `[Errno 13] Permission denied`.
  See the host-uid alignment note in `references/init.md` for the no-op paths.
  A failed attempted remap degrades to a `start.uid_align_failed` warning (rendered unconditionally by `commands/start.py`, not just under `--verbose`) and start still proceeds.
- **Blocks until the web server actually binds THE BENCH'S OWN PORT before reporting running** (`fm/cwcli-start-web-readiness-w3`; the port became explicit in `report-status-per-bench`).
  supervisord reports its programs up a beat before `bench serve` binds the port, so a caller that declared
  "running" the instant the launch returned raced the web - a scripted `cwcli start && cwcli status` (or `cwcli
  init && cwcli status`) caught a transient `degraded`. `start` closes that race itself, at the ONE shared point
  every caller (`cwcli start`, init's auto-start, `axi start`/`axi init`, whole-stack `restart`, and the
  post-restore restart) already funnels through, rather than each caller re-implementing its own wait.
  `supervision.wait_web_ready(container, port=...)` (bounded 60s, 1s poll interval) reuses the existing
  `web_http_code` probe via the `web_is_serving` helper (status's own "up" definition: any code not in
  `(None, "000")`). **The port is REQUIRED, keyword-only, and undefaulted**, and the port itself is READ from the
  bench's own `sites/common_site_config.json` via `resolvers.resolve_assigned_ports(..., fill_defaults=False)` -
  this wait used to poll a hardcoded `:8000`, so `cwcli start <p> --bench 1` sat 60 seconds watching bench 0's
  port and then warned that a perfectly healthy bench had not started (audit F5), then pointed the user at
  `cwcli status`, which confirmed the phantom fault by making the same mistake. **With no resolvable port the
  wait is SKIPPED** (`web_ready` stays None - its existing "not probed" value - plus a `start.web_port_unknown`
  warning): spending the timeout on a guess is worse than saying nothing, and `StartOutcome` needed no new field.
  The wait runs ONLY on a genuine launch and ONLY when the Procfile defines a `web` program - the idempotent
  no-op returns BEFORE it (stays fast, `web_ready=None`), and a no-web bench is never blocked
  (`web_ready=None`, zero added latency). A timeout NEVER fails the start (the stack IS launched): it degrades
  to a `start.web_not_ready` warning and `StartOutcome.web_ready=False`; a bench already serving passes on the
  first poll with no added latency (`web_ready=True`). `start.web_not_ready` is on every caller's unconditional
  warning list (`commands/start.py`'s `_run_start` AND `_start_project` - the latter is what `restart`'s
  whole-stack path and the auto-start path share), `init`'s `_start_services` prints it and withholds "Dev
  services are running" when `web_ready is False`, and `core/restore.py`'s post-restore `_restart` forwards it
  rather than swallowing it (a restored site that never begins serving must not read as a silent success).
  See `tests/e2e/test_start_status_e2e.py::test_status_is_running_immediately_after_start` for the regression
  net (asserts `status` reads `running` the instant `start` returns, with no readiness wait between the two
  calls) and `test_core_start.py`/`test_core_supervision.py` for the unit coverage of the branches above.

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
- **A whole-stack restart preserves the selected bench.** The frontend still stops every container, then
  calls the shared `_start_project`, but now threads `--bench` into that call on the no-`--process` path too.
  Without this, the accepted selector was discarded and `_start_project`'s intentional first-bench fallback
  relaunched a sibling at exit 0. An omitted selector keeps that documented fallback; an explicit selector
  must never retarget.

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
  correct: `up` is the observable truth). The report is flagged `BenchStatus.not_cwcli_supervised=True` with a
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
  `probe_web=False` suppresses the per-bench `curl` call (which is what spams the bench's access logs);
  per-program health still comes from `ps` + `supervisorctl` (the control socket, which touches no web port), so repeated
  ticks leave ZERO HTTP requests. With `probe_web=False`, `_overall` is driven by supervisor-up + all-healthy
  (a missing web code must NOT falsely `degrade`). `--watch` is a frontend re-poll (`commands/status.py:_watch_loop`,
  `rich.Live` on stderr) - the core stays one-shot. The loop starts only when BOTH stdout+stderr are TTYs;
  otherwise one quiet snapshot. `--interval` floors at 1s; Ctrl-C exits 0. `cwcli axi status` stays one-shot.
- **The web probe names the site in its `Host` header and report.** Frappe routes by Host, so a host-less
  curl correctly returns 404 even when the selected site serves 200. `resolve_representative_site` first uses
  the declared default site, then falls back to the cached site list because benches created by `cwcli init`
  do not necessarily run `bench use`. If several undefaulted sites exist, choosing one is a disclosed pick:
  `BenchStatus.web_site` and the human `web <site>:<port> -> <code>` line name the site whose response was
  measured. No resolvable site keeps the host-less probe and reports `web_site=None`.

## `core.stop.py` - project stop and bench stop are distinct operations

- **`stop_bench(project, bench=..., bench_path=...)` stops one bench's supervisord, not a container.** A
  multi-bench instance has one frappe container shared by sibling benches, so `cwcli stop --bench` reuses
  `supervision.stop_supervisor` for the resolved bench and leaves every container and sibling bench running.
  The human and `axi` frontends share this core function, and an already-stopped bench is an idempotent
  success.
- **A deliberate bench stop clears its launch marker only after teardown succeeds.** The marker distinguishes
  started-then-died (`degraded`) from never-started (`online`). Leaving it after an intentional stop would make
  a healthy instance cry wolf forever; clearing it before verified teardown would hide a failed stop. An
  unreadable process state, failed signal, surviving supervisor, or marker-clear failure therefore fails
  closed.

## Multi-bench (D5), the axi verbs, and the container-boot NON-GOAL

- `start`/`status`/`restart`/`stop --bench` share `resolvers.resolve_bench`. `start`/`restart` MUTATE one bench, so
  on multi-bench with no `--bench` the human CLI prompts (`on_ambiguous="prompt"`, TTY) and refuses non-zero on
  a non-TTY, while `cwcli axi start`/`restart` emit a `select_bench` usage error naming the flag. The internal
  `_start_project` (restart / auto-start callers) keeps the lenient first-bench fallback only when no selector
  was supplied; whole-stack restart threads an explicit `--bench` through to it. `cwcli axi start`
  never prompts a port conflict: a `CONFLICT` naming `--yes` via the non-printing `detect_port_conflicts`.
- **`status` no longer joins that refusal, and the difference is READ vs MUTATE** (`report-status-per-bench`).
  It is a read, so the bare form ANSWERS the question the refusal used to send the caller away to
  reconstruct: it reports EVERY cached bench in one document (`StatusReport.benches: list[BenchStatus]`, the
  `InspectReport.benches` model, uniform even for one bench) and `core.status` never returns `NEEDS_CHOICE` at
  all. Both resolvers of that choice are DELETED, not left unreachable - `commands/status.py:_fetch`'s
  prompt-and-retry loop and `commands/axi.py:axi_status`'s `emit_axi_choice_as_usage_error` branch - because a
  retained-but-unreachable prompt is how the refusal comes back. `status` is therefore non-prompting on EVERY
  path, which satisfies the both-modes standard trivially. **Each bench is probed on its OWN port**, read from
  its `sites/common_site_config.json` (`resolvers.resolve_assigned_ports(..., fill_defaults=False)`) and passed
  explicitly to `supervision.web_http_code(container, port=..., site=...)`; the probe used to hardcode `localhost:8000`,
  so a healthy bench 1 read `degraded` (F3) and a dead bench 1 reported bench 0's live code (F4). An unresolved
  port is `web_port: null`/`web_port_verified: false`, NO probe, a `status.web_port_unknown` warning, and
  `_overall(web_probed=False)` - it does NOT degrade, because degrading on an unreadable JSON file would
  manufacture a fresh F3 while fixing the old one. The instance `overall` folds the per-bench ones over the same
  four tokens (no fifth): `degraded` dominates, and a `running` bench beats a never-started `online` one.
- **`stop --bench` is the bench-scoped inverse of `start --bench`.** It ends only that bench's supervised dev
  processes and clears its launch marker; the project-wide form still stops the instance's containers and all
  benches with them. `cwcli axi stop --bench` exposes the same idempotent operation without prompting.
- **Container-boot auto-relaunch is an explicit NON-GOAL.** supervisord's lifecycle is the container's
  lifecycle: it heals crashed PROGRAMS on its own, but it is NOT auto-relaunched when the container itself
  restarts (that needs the image entrypoint, which cwcli cannot set via `docker exec`). A `cwcli start` is
  still required after a container restart - unchanged from honcho.

## `commands/logs.py` - tail's exit code, and why `logs` is NOT on `exec_stream`

- **The fail-open (fixed).** `logs` ran `subprocess.run(tail_cmd)` with no `check=`, discarded the
  result, and caught `subprocess.CalledProcessError` beneath it. `subprocess.run` raises that ONLY
  when `check=True`, so the returncode was thrown away and the handler was **unreachable dead code**:
  `cwcli logs` exited 0 no matter what `tail` did. Verified E2E against a real container - a `tail`
  exiting 1 gave `cwcli exit=0`. The fix reads `result.returncode` and PROPAGATES it (`tail` 137 ->
  `cwcli` 137), rather than flattening to 1.
- **Ctrl+C arrives as exit 130, NOT as a `KeyboardInterrupt`** - this is why the fix is not simply
  `check=True`. On the interactive path `docker exec -it` puts the terminal in raw mode and forwards
  `^C` INTO the container, so `tail` takes the SIGINT and exits 130 while the cwcli process is never
  signalled (measured through a real pty). 130 is therefore the user's normal stop and must exit 0;
  the `except KeyboardInterrupt` handler still covers the NON-TTY path, where SIGINT does reach cwcli.
  `check=True` would raise on that 130 and make every interactive `cwcli logs -f` exit non-zero.
  `tests/test_logs.py`'s fake models `subprocess.run`'s real `check=` semantics so a future
  `check=True` "fix" fails the 130 test rather than shipping.
- **`logs` is a deliberate NON-consumer of `core/exec_stream.py`** despite reaching the same fail-open
  class (`run`/`apps` leaked it via `ExitCode: None`; `logs` via a discarded returncode). `exec_stream`
  is a non-TTY docker-py exec with no `tty` parameter, while `logs` is a `docker exec -it` passthrough
  whose job is an interactive `tail -F`. Re-pointing it would (1) need a `tty` param on a primitive
  that deliberately has none, (2) leak a `tail -F` inside the container on every Ctrl+C, since without
  a TTY the SIGINT never reaches it, and (3) make `_poll_exit_code` see the still-`Running` exec and
  raise `exec.stream_lost` for what is a normal user stop. `logs` was long mis-filed as a
  process-handover command; it hands over nothing (no `execvp`), but it is still not an `exec_stream`
  consumer.
- The two other `subprocess.run` calls here (`_existing_files`, `_discover_bench_log_files`) are
  deliberately tolerant probes (`[ -f ]`, `ls ... 2>/dev/null`): a non-zero exit is expected and
  yields an empty list, which the "No process logs found" branch already turns into a non-zero exit.
  They are NOT the same bug - do not "fix" them with `check=True`.
