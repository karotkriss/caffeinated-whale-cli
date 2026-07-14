## 1. supervisord supervision substrate (re-point core/supervision.py from honcho)

- [ ] 1.1 Generate the supervisord config from the live Procfile: one `[program:<label>]` per Procfile line (command + `directory`=bench path), labels matching the discovery set (web, socketio, worker[/queue], schedule, watch, redis_cache, redis_queue), written on the workspace volume; add the `[supervisord]`/`[unix_http_server]`/`[supervisorctl]`/`[rpcinterface:supervisor]` sections and the unix control socket. Reuse `expected_labels` + the Procfile command strings; no baked-in per-version assumptions.
- [ ] 1.2 Auto-heal config state on every program: `autorestart=unexpected` (default; gated by the `--autorestart`/`--no-autorestart` toggle), `startretries`+`startsecs` backoff, `stopasgroup`/`killasgroup` for grandchild cleanup; per-program `stdout_logfile` + `redirect_stderr=true` + `stdout_logfile_maxbytes`/`stdout_logfile_backups` rotation under the bench `logs/` dir.
- [ ] 1.3 Idempotent, fail-closed `pip install supervisor` into the bench Python env on first supervise: skip if already available; raise a typed `CwcliError` (never silently fall back to honcho) on failure.
- [ ] 1.4 Re-point detection: `SUPERVISOR` -> `"supervisord"`; `_is_honcho`/`_honcho_pids_for_bench` -> supervisord-keyed detection by config path (or cwd), multi-bench-safe; the marker records `{supervisor:"supervisord", started_at, config_path, ...}`. REUSE `discover_stack` tree-walk, `label_for`, `expected_labels`, the multi-bench keying - do not rewrite.
- [ ] 1.5 `launch` writes the config + ensures supervisor installed + launches `supervisord -c <config>` detached; `stop_supervisor` SIGTERMs supervisord by discovered PID and waits bounded for the tree to exit before returning (preserve the no-race-on-relaunch guarantee); `supervisorctl` state read + single-program restart helpers.
- [ ] 1.6 Retire the honcho combined-stream capper (`_LOG_CAPPER_SRC`) and the prefix parse (`per_process_log_lines`); add per-process log-path resolution and a combined-view synthesizer that reads the per-process files label-attributed.
- [ ] 1.7 Unit-test config generation (per-version Procfile shapes), the pip-install bootstrap (present/absent/failure), `supervisorctl` state parsing, supervisord detection + bench-keying (no cross-bench mis-attribution), and the marker, against faked exec I/O.

## 2. core.status / core.start enrichment (drop all-or-nothing; --autorestart toggle)

- [ ] 2.1 Add `ProcessHealth.state` (`RUNNING`/`STARTING`/`BACKOFF`/`EXITED`/`FATAL`/`STOPPED`) sourced from `supervisorctl status`, alongside the `ps`-derived PID/uptime/CPU/RSS.
- [ ] 2.2 Update `overall`: a stable partial stack (web up, a program not `RUNNING`) reports `degraded`, honestly; keep the four tokens (`offline`/`online`/`running`/`degraded`); update the now-false all-or-nothing rationale comment in `core/status.py`.
- [ ] 2.3 Thread `--autorestart`/`--no-autorestart` into `core.start`'s generated supervisord config (default on = `autorestart=unexpected`); keep `core.start` idempotent by discovered supervisor PID and honoring an explicit `bench_path` verbatim (post-restore restart preserved).
- [ ] 2.4 Unit-test each `overall` branch under supervisord (including the new stable-partial `degraded`), the `state` field mapping, and the `--autorestart` config toggle.

## 3. core.restart_process (single-program restart)

- [ ] 3.1 Implement `core.restart_process(project, label, *, bench=None, bench_path=None) -> Result[ProcessRestartOutcome]`: resolve the bench (shared `resolve_bench`), validate `label` against the program set, discover `old_pid`, `supervisorctl restart <label>`, discover `new_pid` + `supervisor_state`, return the outcome. No print/prompt/`typer.Exit`.
- [ ] 3.2 Define the `ProcessRestartOutcome` DTO (`project`, `bench_path`, `label`, `old_pid`, `new_pid`, `supervisor_state`); serializable, no live Docker object.
- [ ] 3.3 Return `NEEDS_CHOICE` listing valid labels for an unknown/ambiguous `--process`; return the shared `select_bench` `NEEDS_CHOICE` for multi-bench-no-selector; raise `CwcliError(NOT_FOUND)`/`(DOCKER)` for hard failures (project/bench/daemon).
- [ ] 3.4 Unit-test: single-program restart (siblings' PIDs untouched); restart of a down program (`old_pid=None`); unknown-label `NEEDS_CHOICE` with the valid list; multi-bench `NEEDS_CHOICE`; explicit `--bench` honored.

## 4. Reseat the CLI: cwcli restart / logs / start

- [ ] 4.1 `commands/restart.py`: add `--process <label>` and `--bench <sel>`; `--process` given -> `core.restart_process` (single-program) with CLI-side `select_bench`/unknown-label choice resolution; `--process` omitted -> today's whole-stack restart preserved; honest per-project exit codes; fix stale "under honcho" docstrings to supervisord.
- [ ] 4.2 `commands/start.py` + `commands/axi.py`: add the `--autorestart`/`--no-autorestart` flag on `cwcli start` and `cwcli axi start`, threaded to `core.start`; the flag is a config toggle set at launch, not a live command.
- [ ] 4.3 `commands/logs.py`: add `--process <label>` (tail one program's file) and synthesize the combined view when omitted; resolve the bench via the shared wrapper; drop the honcho-prefix assumptions.

## 5. cwcli axi restart verb

- [ ] 5.1 `cwcli axi restart <project> --process <label>`: call `core.restart_process`, emit the `ProcessRestartOutcome` TOON on stdout (no progress text on stdout), map the unknown-label/`select_bench` `NEEDS_CHOICE` to a usage error naming the flag/listing valid labels (exit 2), map `CwcliError` via the shared exit mapper, never prompt; `--process` required (usage error if omitted); NO `--watch`.
- [ ] 5.2 Unit-test the verb: TOON on stdout, exit codes, unknown-label + multi-bench flag-naming usage errors, `--process`-required error.

## 6. Validation + docs

- [ ] 6.1 New real-Docker E2E on the v14/v15/v16 matrix (both modes): `pip install supervisor` succeeds and is idempotent; a killed worker auto-restarts while web keeps serving; `cwcli restart --process <label>` restarts one and leaves siblings' PIDs intact; a crash-loop reaches `FATAL`; `--no-autorestart` leaves a crashed program down; per-process logs land in their own files and a combined view synthesizes; drive interactive prompts via a pty and non-interactive paths via `--process`/`--bench`/`--yes`. Keep the `add-start-status-e2e-net` invariant net GREEN.
- [ ] 6.2 Keep `uv run mypy src/` at zero errors and `black`/`ruff` clean over `src/`; the new core module + supervisord helpers + axi verb fully typed, no `# type: ignore`.
- [ ] 6.3 `README.md`: the supervisord model, `cwcli restart --process`, the `--autorestart` toggle, the enriched `cwcli status` (per-process `state`, stable-partial `degraded`), `cwcli logs --process`, and `cwcli axi restart`; `CHANGELOG.md` entry; update the `cwcli-lifecycle` skill and root `CLAUDE.md` supervision notes from honcho to supervisord.
- [ ] 6.4 Re-run the real-instance restart/status/logs/start E2E AFTER the no-mistakes run and after any review fixes (captain standard), confirming both modes still behave end to end on a throwaway isolated instance - not just that the unit suite is green.

## 7. Deferred - NOT in this change (boundary made explicit)

- [ ] 7.1 DEFERRED (explicit NON-GOAL): auto-relaunch of the supervisor on container BOOT (needs the image entrypoint, unsettable via `docker exec`); a `cwcli start` is still required after a container restart, matching s4.
- [ ] 7.2 REJECTED (recorded, not reopened): Option B (respawn one Procfile entry under honcho) - impossible, honcho kills all siblings on one death; Option C (a hand-rolled cwcli meta-supervisor over `nohup`+polling) - reinvents a supervisor and reintroduces the daemon shape.
- [ ] 7.3 DEFERRED: a live `cwcli restart --watch --heal` command / an `axi --watch` - auto-heal is supervisord config state, not a foreground loop.
- [ ] 7.4 DEFERRED: the full core migration of `logs`/`restart` - updated here only for per-process logs and the single-program restart verb.
