## Context

This is the second of the rework's two changes (the captain's two-PR structure): the E2E net (`add-start-status-e2e-net`, PR 1) lands first and pins the preserved invariants; this change migrates `start` + `status` onto the core under it.
It follows the pattern the `backup` reference slice established (`core-logic-foundation`): a UI-pure core function returning a typed `Result[T]` envelope or raising `CwcliError`, a reseated thin CLI frontend, and a `cwcli axi` verb over the same core.

OpenSpec reads no code, so the `file:line` citations below come from the recon (`data/cwcli-startstatus-recon-h9/report.md`), which was fed to this proposal as the real audit, and from the targeted reading done for it.
The recon verified the `bench start` / honcho mechanics against upstream `frappe/bench` and `honcho` source; the three points it flagged `[verify on a container]` (that `pkill -f 'bench start'` never matches steady-state honcho; that re-running `start` double-starts; the exact app-log set under `logs/`) are confirmed during implementation, not assumed here.

The forks below were settled with the captain (recon §7, D1-D6).
Each records the chosen option and the rejected alternative so a later reader cannot silently reopen a settled fork.

## Goals / Non-Goals

**Goals:** migrate `start` + `status` onto the core as ONE unit behind one shared tracked-state contract; fix the double-start bug via discovered-PID idempotency; give `status` honest per-process health with a pre-computed `overall`; persist and bound the log; add the `cwcli axi start`/`status` verbs - all behind the green net.

**Non-Goals** (see the proposal's Non-Goals for the full list): per-process restart resilience, an in-container supervisor, a cwcli daemon, the `status --watch` live stream (backlog `cwcli-status-watch-w7`), a plan/apply split, and the full core migration of `logs`/`restart`.

## Decisions

### 1. Supervision model = observe, keep honcho (D1 option 1)

`start` keeps launching bench's own honcho (via `bench start` detached); cwcli owns only the READ side.

- **Why it wins.** cwcli is a short-lived CLI reaching long-lived in-container processes only through `docker exec`, so it can never be the live parent of those processes across its own invocations (recon TL;DR).
  "Own supervision" therefore means "own observation": the cheapest, most honest option, faithful to how bench actually runs, needing no image change and nothing new installed.
- **Rejected: an in-container supervisor** (supervisord/s6/pm2) - real per-process restart, but it diverges from vanilla bench (cwcli would own supervisor-config generation and must track upstream Procfile changes), adds an image dependency, and is the biggest surface (recon §7 option 2).
  **Rejected: a cwcli daemon** - maximal control, but a long-lived daemon's own lifecycle is a hard problem on an otherwise short-lived CLI, and its held `docker exec` streams die with it (recon §7 option 3).
- **Consequence - the explicit NON-GOAL.** honcho is all-or-nothing: when any child exits it terminates the rest, and it NEVER restarts a crashed process (`honcho/manager.py`, recon §2).
  So this change builds NO per-process restart, and per-process liveness is a low-signal snapshot of an all-or-nothing stack - an honest liveness + resource view, not resilience.
  This is documented as a first-class non-goal because it is the load-bearing reason the health model is a snapshot, not a supervisor.

### 2. One shared tracked-state contract (start writes, status reads)

`start` and `status` are ONE unit, not two verbs.
The moment `status` reports per-process health it must read what `start` produced, so the shared contract is the change's central artifact (recon §6).

The contract has three parts, all in the shared substrate:

- **Live per-process discovery** - one cheap `ps -eo pid,etimes,%cpu,rss,args` in the frappe container, mapping each PID to a Procfile label from its self-describing cmdline (`honcho`; `bench serve` -> web; `node .../socketio` -> socketio; `bench watch`/`bench schedule`; `bench worker [--queue X]` -> worker/queue; `redis-server ...conf` -> redis_cache/redis_queue). up/down = label present; uptime = `etimes`; CPU/RSS straight from `ps` (recon §5). One command yields up/down + uptime + CPU + RSS, so there is little reason to stop at up/down (recon D3).
- **Live Procfile parse for expected labels** - read the bench's `Procfile` in-container to know which labels SHOULD be up, so `overall` can tell `running` from `degraded` without a stale snapshot. The Procfile is the source of truth for the expected set (recon §2 table).
- **A minimal supervisor marker** - `core.start` writes a tiny marker on launch recording `{supervisor: "honcho", started_at, log_path}` under the bench (on the workspace volume, so it survives a container restart). `core.status` reads it. This is what lets status distinguish "was started, supervisor now down" (marker present, honcho absent - the container-restart / crash case) from "bench never started" (no marker) - the distinction the design requires (design point 2) that pure discovery alone cannot make (there is no honcho pidfile).

`start` and `status` share ONE bench selector (`resolvers.resolve_bench`), so they always report the SAME bench (recon §6, D5).

### 3. `overall` aggregation (pre-computed, per axi rule 4)

`core.status` computes `overall` up front so no frontend re-derives it:

| `overall` | Condition |
| --- | --- |
| `offline` | no containers / no frappe service / frappe container not running |
| `online` | container running, but no start marker (bench never started) |
| `running` | marker present, honcho up, all expected Procfile labels up, and the web HTTP probe answers |
| `degraded` | marker present, honcho up but some expected label down OR web not answering; OR marker present and honcho DOWN (started, supervisor died) |

The web HTTP code is kept as one field alongside process liveness (recon §7 "web booting vs up"), so "supervisor up, web present but not yet answering :8000" is distinguishable from "web down".
Given honcho's all-or-nothing behavior, `degraded` is rare/transient under vanilla bench - but it is still reported honestly, and the marker-present + honcho-down case (a container restart with nothing relaunched) is a real, stable `degraded` state worth surfacing.

### 4. `start` mutation model = idempotent, discovered-PID guard (D4)

Detect an already-running bench by DISCOVERED supervisor PID keyed to the resolved bench path, NOT by `pkill -f 'bench start'`.

- **Why it wins.** `pkill -f 'bench start'` never matches steady state: bench `os.execv`s into honcho, so the cmdline is `honcho start ...` and the children are `bench serve`/`worker`/`schedule`/`watch` - none contain `bench start` (recon §2, §3). Today that means re-running `start` does not kill the old stack and likely spawns a SECOND honcho (duplicate workers, in-container port clashes). Idempotency is a correctness fix, not a nicety.
  So `core.start` discovers the honcho supervisor for the RESOLVED bench (via `ps`/`/proc` cwd or the `-f <bench>/Procfile` arg, keyed to `bench_path`); if it is already running, `core.start` no-ops and returns an `already_running` `StartOutcome` (a "already running: N/N processes up" status line), exit 0. If not, it launches `bench start` and then discovers the launched set for the outcome.
- **Rejected: plan/apply.** `start` is non-destructive, so a preview boundary is over-build (YAGNI, D4); the envelope + needs-choice primitives suffice.
- **Container state.** Unlike `backup`, `start`'s whole job is to bring things up, so `core.start` starts stopped containers directly (no `confirm_start` fork) and then handles the bench. The `confirm_start` fork stays only in the OTHER commands that need an already-running container.

### 5. Multi-bench = align to the family (D5)

Drop `start`'s unique `on_ambiguous="first"` + note; use the shared `resolvers.resolve_bench`, which returns `select_bench` `NEEDS_CHOICE` on multi-bench ambiguity.

- **Why it wins.** `start` and `status` must agree on which bench they report; sharing the one resolver guarantees it. On multi-bench with no selector the interactive CLI prompts (existing `resolve_bench_path` wrapper) and `cwcli axi` returns a `select_bench` usage error naming `--bench` - exactly like `backup`/`inspect`. Single-bench is unaffected.
- **Preserved.** The post-restore restart passes an explicit `bench_path` (restart the SAME bench it just migrated, never the first sorted one - a shipped safety behavior, `start.py` `bench_path_override`); `core.start` keeps a `bench_path` param so that path is used verbatim, skipping resolution.
- **Behavior change, explicit.** The pre-migration `first-with-note` default is gone. PR 1's net drives multi-bench with an explicit `--bench` so it stays green; this change's new tests cover the prompt/error.

### 6. Port-conflict logic = CLI-frontend host-side pre-step (D6)

`_check_port_conflicts` (host-side, stops other Frappe projects, prompts) keeps its prompting in the frontend, mirroring how `ensure_containers_running` was split.

- **Why it wins.** It inspects HOST ports and stops OTHER projects - a host/frontend concern, not container logic - and it prompts, so it is naturally a frontend pre-step (recon D6). `core.start` assumes ports are already clear, a documented precondition (`_start_project` already documents "Port conflict checks should be performed by the caller").
- **axi.** `cwcli axi start` never prompts: it surfaces an unresolved conflict as a `CONFLICT` structured error naming `--yes`, and `--yes` auto-resolves conflicting Frappe projects (today's non-interactive `assume_yes` branch). Whether axi reuses `_check_port_conflicts`'s non-interactive branch directly or a small core detection helper is an implementation choice (Open Questions); the fixed invariant is: axi start never prompts, an unresolved conflict is a `CONFLICT` error, `--yes` auto-resolves.

### 7. Logs = persist on the volume + bound (D2)

Move the captured honcho stream from ephemeral `/tmp/bench-<project>.log` to `{bench_path}/logs/bench-start.log` (on the frappe_docker workspace volume, so it survives a container restart), bounded so it cannot grow unbounded.

- **Why it wins.** The ephemeral `/tmp` combined log is the weakest link and the cheapest win (recon §4); the bench `logs/` dir is its natural home, co-located with frappe's own logs and already on a persistent volume.
- **Bound.** Truncate-on-(re)launch bounds growth across restarts (each real launch starts a fresh session log; the idempotent no-op does NOT truncate, so a running session's log is never nuked), plus a fixed byte cap during a run. The exact rotation mechanism is an implementation choice with two hard constraints: no new image dependency and no cwcli daemon.
- **Per-process views for free.** honcho already emits a `HH:MM:SS name|` prefix per line (`honcho/printer.py`, recon §2), so per-process log views are recovered by parsing that prefix - no per-process redirect plumbing.
- **Single source of truth.** The bench-start log path lives in the shared substrate; `commands/logs.py` (which today hardcodes `/tmp/bench-<project>.log`, duplicated from `start.py:365`) reads it from there, killing the cross-file duplication the recon flags (§1a). Because the log is now per-bench, `logs.py` resolves the bench via the shared `resolve_bench_path` wrapper (single-bench: unchanged; multi-bench: the same `select_bench` contract). `restart` (reusing the reseated start) is verified to still work.

## The DTOs, concretely

Modeled on `BackupOutcome`, all `@dataclass(frozen=True, slots=True, kw_only=True)`, serializable, no live Docker object:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessLaunch:
    label: str            # "web" | "socketio" | "worker" | "worker:<queue>" | "schedule" | "watch" | "redis_cache" | ...
    pid: int | None       # discovered post-launch

@dataclass(frozen=True, slots=True, kw_only=True)
class StartOutcome:
    project: str
    container: str
    bench_path: str
    supervisor: str        # "honcho"
    log_path: str          # {bench_path}/logs/bench-start.log
    already_running: bool  # True -> idempotent no-op
    processes: list[ProcessLaunch]

@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessHealth:
    label: str
    up: bool
    pid: int | None
    uptime_s: int | None
    cpu_pct: float | None
    rss_kb: int | None

@dataclass(frozen=True, slots=True, kw_only=True)
class StatusReport:
    project: str
    container_running: bool
    supervisor_up: bool
    web_http_code: str | None   # today's curl signal, kept as one field
    processes: list[ProcessHealth]
    overall: str                # "offline" | "online" | "running" | "degraded"
```

`dataclasses.asdict` -> the existing TOON serializer, exactly as `axi backup` does.
`cwcli axi status` emits `overall` first (pre-computed aggregate), then the `processes` table; `cwcli axi start` emits the `StartOutcome` and maps a `select_bench`/`confirm`-less flow to a usage error naming `--bench` / a `CONFLICT` error naming `--yes`.

## Boundary discipline

- No live Docker `Container` crosses a `core.start`/`core.status` return boundary; the container stays internal to the exec calls, DTOs carry only serializable data (mirrors `core/docker.py`'s rule).
- The core never imports `rich`/`questionary`/`typer`; the variadic multi-project loop, stdin piping, trailing-flag recovery, spinner, `rich` output, and choice resolution all stay in the reseated `commands/start.py` frontend.
- `core.status` treats an absent project / absent frappe service / stopped container as `offline` (returns a `StatusReport`, does NOT raise) to preserve today's "offline, exit 0" contract; only a genuinely unreachable Docker daemon raises `CwcliError(DOCKER)`, which the frontend maps as it does today.

## Risks / Trade-offs

- **Behavior drift during the migration** -> the change runs under PR 1's green invariant net (structure-agnostic, real side effects, both modes); the new-behavior tests pin the additions.
- **Discovery mis-mapping a PID to the wrong bench on a multi-bench instance** -> discovery is keyed to the RESOLVED `bench_path` (cwd / `-f Procfile` arg), and `start`/`status` share the one selector, so they never disagree on the bench.
- **The log bound must hold during a long run without a daemon** -> truncate-on-launch bounds across restarts; a fixed byte cap bounds within a run; the mechanism is constrained to "no new dependency, no daemon" and verified in E2E.
- **`ps`/Procfile cmdline shapes vary across frappe versions** -> the label mapping is matched against the known Procfile set and verified on the v14/v15/v16 E2E matrix (the `[verify on a container]` points the recon flagged).

## Migration Plan

Additive core + a behavior-preserving-where-required reseat of `start`/`status`/`logs`.
No data migration, no schema change, no dependency added.
The deliberate behavior changes (multi-bench policy, status output) are covered by new tests and floored by PR 1's invariant net.
Rollback is deleting `core/start.py`/`core/status.py`/the shared substrate module and `commands/axi.py`'s two verbs, and reverting `commands/{start,status,logs}.py` to their pre-change form.

## Open Questions

- **How `cwcli axi start` surfaces a port conflict** - reuse `_check_port_conflicts`'s non-interactive branch directly vs a small core detection helper. Deferred to implementation; the fixed invariant is: axi start never prompts, an unresolved conflict is a `CONFLICT` structured error, `--yes` auto-resolves conflicting Frappe projects.
- **The exact log-bound mechanism** (truncate-on-launch + byte cap; the specific rotation primitive) - deferred to implementation under the fixed constraints (no new image dependency, no cwcli daemon, survives container restart).
- **Whether the supervisor marker also snapshots expected labels vs always reading the live Procfile** - lean is live Procfile (no stale snapshot); revisit only if a version's Procfile read is unreliable in-container.
- **Whether `cwcli status` grows a `--json` flag** (like other commands) in addition to the human line + `cwcli axi status` TOON - deferred; the human default stays a concise `overall`-led view, the structured surface is `cwcli axi status`.
