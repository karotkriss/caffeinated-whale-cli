## 1. Shared supervision substrate (the one tracked-state contract)

- [ ] 1.1 Add `src/caffeinated_whale_cli/core/supervision.py` (or `procs.py`): one `ps -eo pid,etimes,%cpu,rss,args` discovery mapping PIDs to Procfile labels (honcho, web, socketio, worker[/queue], schedule, watch, redis_cache, redis_queue) keyed to a RESOLVED bench path; returns serializable per-process data (up/uptime_s/cpu_pct/rss_kb), no live Docker object.
- [ ] 1.2 Live Procfile parse for the expected-label set (source of truth for which labels SHOULD run).
- [ ] 1.3 Supervisor marker: write `{supervisor, started_at, log_path}` under the bench on the workspace volume on launch; read it in status; the idempotent no-op must not lose the original `started_at`.
- [ ] 1.4 The bench-start log path helper (single source of truth) at `{bench_path}/logs/bench-start.log`; the honcho `HH:MM:SS name|` prefix parse for per-process views.
- [ ] 1.5 Log persistence + bound: truncate-on-(re)launch + a fixed byte cap during a run; no new image dependency, no cwcli daemon; the no-op start does NOT truncate a live session log.
- [ ] 1.6 Unit-test discovery/label-mapping/expected-set/marker against faked `ps`/Procfile/exec I/O, including the bench-keying (no cross-bench mis-attribution) and marker-present-vs-absent.

## 2. core.start (idempotent, discovered-PID guard)

- [ ] 2.1 Implement `core.start(project, *, bench=None, bench_path=None, auto_start=False, ...) -> Result[StartOutcome]`: start stopped containers, resolve the bench via `resolvers.resolve_bench`, discover an already-running honcho for the resolved bench path, no-op with `already_running=True` if present, else launch `bench start` detached to the persisted bounded log + write the marker + discover the launched set. No print/prompt/`typer.Exit`.
- [ ] 2.2 Define `StartOutcome`/`ProcessLaunch` DTOs (`project`, `container`, `bench_path`, `supervisor`, `log_path`, `already_running`, `processes`).
- [ ] 2.3 Return `select_bench` `NEEDS_CHOICE` on multi-bench-no-selector; raise `CwcliError(NOT_FOUND)`/`(DOCKER)` for hard failures; honor an explicit `bench_path` verbatim (post-restore restart preserved); document that port-conflict prompting is a CLI-frontend precondition.
- [ ] 2.4 Unit-test every branch: launch outcome; idempotent no-op (no second honcho); multi-bench choice; explicit `bench_path` verbatim; missing project `NOT_FOUND`.

## 3. core.status (per-process health, pre-computed overall, one-shot)

- [ ] 3.1 Implement `core.status(project, *, bench=None, bench_path=None) -> Result[StatusReport]`: resolve the bench (SAME selector as start), read the marker + live discovery + expected Procfile set + the web HTTP probe, compute `overall`; treat absent/stopped as `offline` (return, don't raise); only daemon-unreachable raises `CwcliError(DOCKER)`.
- [ ] 3.2 Define `StatusReport`/`ProcessHealth` DTOs (`overall`, `container_running`, `supervisor_up`, `web_http_code`, `processes`).
- [ ] 3.3 Implement the `overall` aggregation table: offline / online (no marker) / running (marker + honcho + all expected up + web answers) / degraded (marker + honcho down, or up with an expected label/web down).
- [ ] 3.4 Unit-test each `overall` branch + the offline-not-raised contract + supervisor-down-vs-never-started + start/status agreeing on the bench.

## 4. Reseat cwcli start / cwcli status; update logs/restart

- [ ] 4.1 Reseat `commands/start.py` over `core.start`: keep the variadic loop, stdin piping, trailing-flag recovery, `_check_port_conflicts` (prompting stays here), spinner, `rich`; resolve `select_bench` via the CLI wrapper; multi-bench no-selector now prompts (interactive) / errors (non-TTY) - the D5 change; preserve honest per-project exit codes; fix the stale "tmux" docstrings.
- [ ] 4.2 Reseat `commands/status.py` over `core.status`: render `overall` as the primary line (now able to say `degraded`) plus per-process detail; exit 0 across lifecycle states; keep `-v` diagnostics on stderr.
- [ ] 4.3 Update `commands/logs.py` to read the single-source-of-truth bench-start log path (kill the `/tmp/bench-<project>.log` duplication), resolving the bench via the shared `resolve_bench_path` wrapper (single-bench unchanged; multi-bench uses `select_bench`); verify `commands/restart.py` and `commands/utils.py:_start_containers_for_command` still work over the reseated start (post-restore `bench_path_override` preserved).

## 5. cwcli axi start / cwcli axi status verbs

- [ ] 5.1 `cwcli axi start <project> [--bench] [--yes]` (~15-25 lines): call `core.start`, emit `StartOutcome` TOON (idempotent no-op included), map `select_bench` -> usage error naming `--bench` (exit 2), surface an unresolved port conflict as a `CONFLICT` error naming `--yes` (`--yes` auto-resolves), never prompt.
- [ ] 5.2 `cwcli axi status <project>`: call `core.status`, emit `StatusReport` TOON with `overall` first + the per-process table + a definitive offline state, exit 0; map `CwcliError` via the shared exit mapper.
- [ ] 5.3 Unit-test both verbs: TOON on stdout, no progress text on stdout, exit codes, needs-choice/`CONFLICT` flag-naming, offline empty state.

## 6. Validation + docs

- [ ] 6.1 New real-Docker E2E for the NEW behavior (per-process health, `degraded`, idempotent single-supervisor no-op, relocated bounded log, multi-bench prompt/error) in both modes, on top of PR 1's net; keep the `start-status-e2e` net GREEN unchanged.
- [ ] 6.2 Keep `uv run mypy src/` at zero errors and `black`/`ruff` clean over `src/`; the new core modules and axi verbs fully typed, no `# type: ignore`.
- [ ] 6.3 `README.md`: enriched `cwcli status` output + the `cwcli axi start`/`status` verbs; `CHANGELOG.md` entry (idempotent start, real per-process status, relocated logs); note the supervision substrate in the `cwcli-lifecycle`/`cwcli-inspect-benches` skills.
- [ ] 6.4 Re-run the real-instance start/status/logs/restart E2E AFTER the no-mistakes run and after any review fixes (captain standard), confirming both modes still behave end to end on a throwaway instance.

## 7. Deferred - NOT in this change (boundary made explicit)

- [ ] 7.1 DEFERRED (explicit NON-GOAL): per-process restart resilience / an in-container supervisor (supervisord/s6/pm2) / a cwcli daemon. honcho is all-or-nothing and never restarts a crashed process; health here is an honest snapshot, not resilience.
- [ ] 7.2 DEFERRED: the live `status --watch` stream (a typed event iterator) - backlog item `cwcli-status-watch-w7`; only the one-shot snapshot ships here.
- [ ] 7.3 DEFERRED: a plan/apply split for `start` (non-destructive; idempotency is the correctness fix).
- [ ] 7.4 DEFERRED: the full core migration of `logs`/`restart` (updated here only to keep working over the relocated log and the reseated start).
- [ ] 7.5 DEFERRED: the AXI cross-cutting shell (SessionStart hook, installable skill, `skills-lock.json`) - the narrowed `cwcli-axi-pass-x9`.
