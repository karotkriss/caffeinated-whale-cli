## Context

This change builds a **real per-process supervisor** for cwcli: restart a single dead Procfile worker while the rest of the bench keeps running, with automatic self-healing.
It is design-heavy and reverses a previously locked decision, so it goes through the OpenSpec spec-review gate before any code.

It builds directly on the merged `migrate-start-status-core` (s4) substrate: the UI-pure logic core, the typed `Result[T]` envelope / `CwcliError`, and the shared read-side supervision substrate `core/supervision.py` (one `ps` discovery mapping PIDs to Procfile labels, the live Procfile parse, the supervisor marker, the multi-bench keying).
The design below was investigated end to end by the recon `data/cwcli-proc-supervisor-recon-v9/report.md` (the s4 code map with file:line, the honcho all-or-nothing proof, the failure-mode table, the ranked options §4-5) and then SETTLED by the captain.
OpenSpec reads no code, so the file:line citations here come from that recon and the targeted reading done for this proposal.

The decisions below record the chosen option and the rejected alternatives so a later reader cannot silently reopen a settled fork.

## Goals / Non-Goals

**Goals:** restart one Procfile program while siblings keep running; auto-heal a crashed program on its own with crash-loop protection; enrich status with real per-process supervisor states; do all of it in-container so the supervisor's lifecycle is the container's lifecycle, reusing the s4 substrate rather than rewriting it.

**Non-Goals** (full list in the proposal): auto-relaunch of the supervisor on container BOOT (needs the image entrypoint; a `cwcli start` is still required after a container restart, matching s4); a live `cwcli restart --watch --heal` command or an `axi --watch`; Option B (respawn under honcho - impossible); Option C (a hand-rolled cwcli meta-supervisor); the full core migration of `logs`/`restart`.

## Decisions

### 1. Reverse s4 D1: put a real in-container supervisor in the bench (captain-approved)

`migrate-start-status-core` D1 locked "observe, keep honcho; NO in-container supervisor" as the supervision model, and named per-process restart a first-class NON-GOAL.
That was correct *for that change* - per-process restart is physically unreachable under honcho - but it is precisely the thing this change must deliver, so D1 is reversed here with the captain's approval.

- **Why the reversal is sound.** The reason s4 rejected an in-container supervisor was cost/scope, not a lifecycle flaw.
  The *lifecycle* objections in s4 were aimed at a cwcli **host daemon** (its own lifecycle is a hard problem on a short-lived CLI; its held `docker exec` streams die when cwcli exits).
  An **in-container** supervisor has none of those problems: its lifecycle is the container's lifecycle, and cwcli still reaches it only through stateless `docker exec` calls, exactly as it reaches honcho today.
- **What stays true from s4.** cwcli remains a short-lived CLI that owns only the READ side plus discrete mutations via `docker exec`; it is never the live parent of the bench processes across its own invocations.
  The supervisor - now supervisord instead of honcho - is still launched detached inside the container and cold-re-discovered on every invocation.

### 2. Architecture = Option A: replace honcho with supervisord (not B, not C)

cwcli generates a supervisord config from the live Procfile and launches `supervisord` in place of `bench start`.

- **Why it wins.** Per-process restart's entire value is auto-heal, and supervisord gives `autorestart` + `startretries` + backoff + crash-loop FATAL + per-process logs + a clean control API (`supervisorctl` / the XML-RPC unix socket) for free.
  supervisor is not foreign to Frappe - `bench setup supervisor` is the upstream production supervisor; using it with the *dev* Procfile commands keeps dev semantics while gaining prod-grade supervision.
  The children are unchanged (`bench serve`, `bench worker`, `node ...socketio`, `redis-server ...`), so the s4 `label_for` mapping and tree-walk discovery carry over verbatim.
- **Rejected: Option B (respawn one Procfile entry under honcho).**
  Impossible, not merely inferior: honcho SIGTERMs every sibling the instant one child exits and has no "keep going on one death" mode (recon §2, proven), so there are no live siblings to keep running alongside a respawned process, and the respawn is an orphan honcho neither tracks nor tears down.
  The only way to make B "work" is to stop honcho killing siblings - which means removing honcho, i.e. Option A or C.
  Recorded rejected-because-impossible so it is not reopened as "the cheap version."
- **Rejected: Option C (a cwcli meta-supervisor over independent `nohup` launches + polling).**
  It reuses more s4 code and needs no image dependency, but it re-implements a supervisor out of shell: `.env` loading, process-group kill for grandchildren, and - fatally for this change - a retry/backoff/crash-loop state machine plus a polling loop for auto-heal.
  That polling loop is a long-running cwcli process, the exact daemon shape s4 rejected, and it cannot live on `axi` (the one-TOON-document contract).
  supervisord provides all of it correctly; hand-rolling it is where the long-tail bugs live.

### 3. Bootstrap = idempotent `pip install supervisor` into the bench env, fail-closed

supervisord is not preinstalled in the frappe dev image, so cwcli installs it on first supervise.

- **Why it wins.** The install is clean and cheap (verified: supervisor 4.3.0, pure-python, ~1.5s, 0 extra transitive deps), installed into the bench's own Python env (the frappe venv), never into cwcli's env.
  It is idempotent: cwcli checks whether supervisord is already importable/on PATH for the bench and skips the install if so.
- **Fail closed, do not fall back to honcho.**
  If the install cannot complete (offline, a read-only env, a pip failure), `core.start` raises a typed `CwcliError` with a clear message rather than silently reverting to honcho - a silent fallback would leave the user thinking they have per-process supervision when they do not.
- **The one runtime unknown from recon closed here.** The recon flagged `[verify on a container]` whether supervisor is present in the dev image; the build's first E2E leg confirms the install on the v14/v15/v16 matrix.

### 4. Auto-heal = supervisord config STATE, `autorestart=unexpected`, crash-loops surface as FATAL

Auto-heal is delivered by the generated config, not by any cwcli loop.

- **`autorestart=unexpected`** (restart on a crash / unexpected exit, NOT on a clean exit) is the default, so a worker that exits 0 on purpose is not fought, while a crashed one comes back.
- **`startretries` + `startsecs` backoff** bound the retries; after `startretries` a crash-looping program enters supervisord's visible **FATAL** state instead of silently hammering.
  A silent auto-restarter that hides a crashing worker is worse than a visible dead one, so FATAL is surfaced in status (Decision 6).
- **`stopasgroup=true` / `killasgroup=true`** so a stop/restart signals the whole process group, cleaning up the grandchildren `bench worker`/`bench serve` fork (the "orphaned children" failure mode in the recon table).
- **The `--autorestart` / `--no-autorestart` toggle lives on `cwcli start`** (and `cwcli axi start`), setting this config state when the supervisord config is generated at launch - NOT as a live command.
- **Rejected: a `cwcli restart --watch --heal` foreground command.**
  Under Option A it is redundant (supervisord heals), and any polling implementation is the daemon shape s4 rejected and cannot live on `axi`.
  Auto-heal is config state owned by the in-container supervisor, full stop.

### 5. Command surface = `cwcli restart <project> [--process <label>] [--bench <sel>]`

`--process` scopes the restart to one program; omitting it preserves today's whole-stack `restart`.

- **Whole-stack (no `--process`)** keeps today's behavior: stop + start the containers, which now relaunches supervisord (a genuine relaunch via `core.start(restart=True)`).
- **Single-process (`--process <label>`)** calls `core.restart_process`, which does `supervisorctl restart <label>` - one program cycles, every sibling keeps running.
- **`--bench <sel>`** selects the bench on a multi-bench instance, via the shared `resolvers.resolve_bench` `NEEDS_CHOICE` (same selector as `start`/`status`, so all three agree on the bench).
- **`cwcli axi restart <project> --process <label>`** is the agent verb: a one-shot single-program mutation emitting one TOON document.
  `--process` is required on the axi verb (a usage error names it if omitted); whole-stack restart via axi is out of scope for this change (`cwcli axi start` already brings a stack up).
  No `axi --watch` (the one-TOON-document contract).

### 6. Status/health = report supervisord's real per-process states; drop the all-or-nothing assumption

`ProcessHealth` gains a `state` field carrying supervisord's state token (`RUNNING` / `STARTING` / `BACKOFF` / `EXITED` / `FATAL` / `STOPPED`), read from `supervisorctl status`.

- **Why `supervisorctl status`, not just `ps`.** supervisord distinguishes a crash-looping `BACKOFF` and a give-up `FATAL` from a clean down, which a `ps`-only view (a PID is present or not) cannot.
  The s4 `ps` discovery still runs for PIDs/uptime/CPU/RSS; the `state` field comes from supervisord, which is the authoritative owner of per-process state.
- **`overall` stops assuming all-or-nothing.** Under honcho, `core/status.py` keyed `running` on honcho-up alone with the comment "honcho is all-or-nothing ... so honcho-up already implies the stack is up."
  That comment/assumption is now false and is updated: with supervisord keeping siblings alive, "web serving while a worker is FATAL" is a real, stable state and reports **`degraded`**, honestly.
  `overall` keeps its four tokens (`offline` / `online` / `running` / `degraded`); the crash-loop / FATAL detail lives in the per-process `state` field so a frontend can highlight it, without a fifth `overall` token that would churn the E2E net and the `axi` contract again.
- **`running`** now means: supervisord up for this bench, every expected program `RUNNING`, and (when probed) the web HTTP code answers.
  **`degraded`** now covers a genuinely stable partial stack (some program not `RUNNING`) in addition to the s4 cases (marker present + supervisor down; web not answering).

### 7. Logs = per-process files with supervisord rotation; combined view synthesized on read

Each `[program:<label>]` writes its own `stdout_logfile` under the bench `logs/` dir with `redirect_stderr=true` and supervisord's built-in rotation (`stdout_logfile_maxbytes` + `stdout_logfile_backups`).

- **Why it wins.** Per-process files are the natural fit for per-process restart: a survivor's log is untouched when a sibling restarts, and the fragile honcho combined-stream capper (`_LOG_CAPPER_SRC`) plus the prefix parse (`per_process_log_lines`) are retired.
  supervisord's rotation replaces the hand-rolled byte-cap-and-rotate capper with no cwcli daemon and no custom code.
- **Combined view on read.** `cwcli logs` with no `--process` synthesizes a combined view by reading the per-process files (label-prefixed), so the "one stream to tail" ergonomics survive; `cwcli logs --process <label>` tails one program's file directly.
  The per-process files are the source of truth; the combined view is derived.
- **Still on the workspace volume** (under the bench `logs/` dir), so logs survive a container restart, exactly as the relocated s4 log did.

### 8. Supervisor detection = supervisord-for-this-bench, keyed by config path; reuse the substrate

- **`SUPERVISOR`** constant becomes `"supervisord"`; the marker records `{supervisor: "supervisord", started_at, config_path, ...}`.
- **`_is_honcho` / `_honcho_pids_for_bench`** become supervisord detectors keyed by the config path (`supervisord -c <bench>/config/cwcli-supervisor.conf`) or cwd, so a multi-bench instance never mis-attributes another bench's supervisor - the same keying discipline s4 applied to honcho.
- **REUSE, don't rewrite.** `discover_stack`'s tree-walk from the supervisor root, `label_for` (children unchanged), `expected_labels`, `stop_supervisor` (now SIGTERM to supervisord, which shuts programs down cleanly rather than honcho's panic teardown), and the multi-bench keying all carry over.
  The start begins from `core/supervision.py`, not a rewrite.

## The DTOs, concretely

Modeled on the s4 DTOs, all `@dataclass(frozen=True, slots=True, kw_only=True)`, serializable, no live Docker object:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessRestartOutcome:
    project: str
    bench_path: str
    label: str               # the restarted program's Procfile label
    old_pid: int | None      # discovered before the restart (None if it was down)
    new_pid: int | None      # discovered after the restart
    supervisor_state: str     # supervisord state after restart: RUNNING/STARTING/BACKOFF/FATAL/...

@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessHealth:          # s4 DTO, one field added
    label: str
    up: bool
    pid: int | None = None
    uptime_s: int | None = None
    cpu_pct: float | None = None
    rss_kb: int | None = None
    state: str | None = None  # NEW: supervisord state (RUNNING/BACKOFF/FATAL/...); None if not supervised
```

`StartOutcome` keeps its shape; `supervisor` now reads `"supervisord"`.
`dataclasses.asdict` -> the existing TOON serializer, exactly as the s4 verbs do.
`cwcli axi restart` emits the `ProcessRestartOutcome`; a `select_bench`/unknown-`--process` fork maps to a usage error naming the flag / listing the valid labels.

## The NEEDS_CHOICE contract for `--process`

`core.restart_process` returns two kinds of `NEEDS_CHOICE`, resolved by each frontend its own way (never a prompt in the core):

- **Multi-bench, no selector** -> the existing `resolvers.resolve_bench` `select_bench` `NEEDS_CHOICE` (interactive CLI prompts; `axi` returns a usage error naming `--bench`), identical to `start`/`status`.
- **Unknown or ambiguous `--process` label** -> a `NEEDS_CHOICE` whose choices are the valid program labels (from `expected_labels` / `supervisorctl status`), so the interactive CLI can present them and `axi` can render "unknown process 'X'; valid: web, socketio, worker, ..." as a usage error.
  This mirrors how `select_bench` names the valid benches.

## Boundary discipline

- No live Docker `Container` crosses a `core.restart_process` / enriched `core.status` return boundary; the container stays internal to the exec calls, DTOs carry only serializable data (the s4 rule).
- The core never imports `rich`/`questionary`/`typer`; the variadic loop, spinner, `rich` output, and choice resolution stay in the reseated `commands/restart.py` frontend (the unit test in `tests/test_core_envelope.py` enforces the import ban).
- `supervisorctl` is invoked only through the container's `exec_run`; its text output is parsed into DTOs inside the core, never surfaced raw.

## Risks / Trade-offs

- **The `pip install supervisor` step is a new in-container mutation** -> idempotent (skip if present), fail-closed (typed error, no silent honcho fallback), and confirmed on the v14/v15/v16 E2E matrix (closing the recon's one `[verify on a container]`).
- **cwcli now owns Procfile -> supervisord config generation** and must track Procfile shape across frappe v14/v15/v16 -> the same version matrix `label_for` is already tested against; the generator reads the live Procfile (no baked-in assumptions) and is E2E-verified per version.
- **supervisord shutdown semantics differ from honcho** (clean per-program stop vs honcho's panic teardown) -> `stop_supervisor` targets supervisord by discovered PID and waits bounded for the tree to exit before a relaunch, preserving the s4 no-race-on-relaunch guarantee.
- **Behavior drift during the swap** -> the change runs under the `add-start-status-e2e-net` invariant net (structure-agnostic, real side effects, both modes); new-behavior tests pin the additions (auto-heal, single-process restart, FATAL, per-process logs).
- **Two supervisors across a fleet transition** (a bench started under old honcho code, inspected by new supervisord code) -> detection is keyed to the supervisor identity in the marker and the live config path; a bench still under honcho is detected as such (or reported not-supervised-by-supervisord) rather than mis-read, and a `cwcli start` migrates it to supervisord.

## Migration Plan

Additive core (`core/restart.py`, supervisord helpers in `core/supervision.py`) plus a re-point of the existing supervision from honcho to supervisord and a behavior-preserving reseat of `restart`/`logs`/`start`.
No cwcli-side dependency added; the one new dependency (`supervisor`) is installed into the bench container on first supervise.
The deliberate behavior changes (supervisor identity, `status` `state` field, per-process log layout) are covered by new tests and floored by the E2E net.
Rollback is deleting `core/restart.py`, reverting the supervisord helpers + the honcho->supervisord re-point in `core/supervision.py`/`core/status.py`/`core/start.py`, and reverting `commands/{restart,logs,start,axi}.py`; a bench then simply goes back to honcho on its next `cwcli start`.

## Open Questions

- **Where the generated supervisord config lives** - `{bench}/config/cwcli-supervisor.conf` vs under `{bench}/logs/` alongside the marker.
  Deferred to implementation; the fixed invariant is that it is on the workspace volume and its path is recorded in the marker so detection can key on it.
- **How `.env` reaches each program** - supervisord `environment=` populated from the bench `.env`, vs a small wrapper that sources `.env` before exec, vs `bench`'s own env already covering it.
  Deferred to implementation under the constraint that dev semantics (the values honcho got from the bench env) are preserved and verified per frappe version.
- **Whether `overall` should gain a distinct crash-loop token** (e.g. `crashed`) instead of folding FATAL into `degraded`.
  Lean is to keep four tokens and surface FATAL per-process (less churn to the E2E net and the `axi` contract); revisit only if a coarse "a program is crash-looping" signal proves needed at the aggregate level.
- **Whether whole-stack restart should use `supervisorctl restart all` instead of a container stop/start** when supervisord is already up.
  Lean is to preserve today's container stop/start for the no-`--process` path (it is what users expect from `cwcli restart` and it re-reads a changed Procfile); revisit if a faster in-place whole-stack restart is wanted.
