## ADDED Requirements

### Requirement: supervisord replaces honcho as the in-container bench supervisor

`cwcli start` SHALL launch `supervisord` inside the frappe container as the bench supervisor instead of `bench start` (honcho), running the bench's live dev Procfile commands verbatim (preserving dev semantics such as the werkzeug reloader and `bench watch`).
The supervisor SHALL be launched detached inside the container and cold-re-discovered on each cwcli invocation, so cwcli remains a short-lived CLI that owns only the READ side plus discrete `docker exec` mutations, never the live parent of the bench processes.
This reverses the previous "observe, keep honcho" supervision model; a bench started under supervisord SHALL support per-process restart and auto-heal, which honcho's all-or-nothing behavior made impossible.

#### Scenario: A started bench runs under supervisord

- **WHEN** `cwcli start <project>` runs against a bench that supports it
- **THEN** the bench's Procfile programs run as supervisord programs (not under honcho), and one program can later be restarted or auto-healed without disturbing its siblings

#### Scenario: The supervisor is cold-re-discovered, not held

- **WHEN** a later cwcli invocation inspects or restarts a program on an already-started bench
- **THEN** it re-discovers the running supervisord via `docker exec` rather than relying on a held handle, PID manifest, or long-lived cwcli process

### Requirement: The supervisord config is generated from the live Procfile

`cwcli start` SHALL generate the supervisord config by parsing the bench's live `Procfile`, emitting one `[program:<label>]` section per Procfile line whose `command` is that line's command and whose `directory` is the bench path.
Program labels SHALL match the shared discovery labels (`web`, `socketio`, `worker`/`worker:<queue>`, `schedule`, `watch`, `redis_cache`, `redis_queue`), so a program maps to the same label whether observed via `ps` or named in a restart.
The config SHALL be written on the workspace volume and its path SHALL be recorded so supervisor detection can key on it.

#### Scenario: Each Procfile line becomes a supervisord program

- **WHEN** the supervisord config is generated for a bench whose Procfile defines web, socketio, worker, schedule, watch, and redis programs
- **THEN** the config contains one `[program:<label>]` per Procfile line with that line's command and the bench directory, labelled to match discovery

#### Scenario: The config reflects the live Procfile, not a stale snapshot

- **WHEN** the Procfile differs across frappe versions (v14/v15/v16 program shapes)
- **THEN** the generated config is produced from the live Procfile read in-container, so it matches whatever programs that version actually defines

### Requirement: supervisor is installed into the bench env on first supervise, idempotently and fail-closed

Because `supervisord` is not preinstalled in the frappe dev image, `cwcli start` SHALL install `supervisor` into the bench's own Python env on first supervise.
The install SHALL be idempotent: if supervisord is already available for the bench, cwcli SHALL skip the install.
If the install cannot complete (offline, read-only env, pip failure), `core.start` SHALL raise a typed `CwcliError` with a clear message and SHALL NOT silently fall back to honcho.

#### Scenario: First supervise installs supervisor once

- **WHEN** `cwcli start` runs against a bench that has no supervisor installed
- **THEN** cwcli installs `supervisor` into the bench Python env, then launches supervisord; a subsequent `cwcli start` finds it present and skips the install

#### Scenario: A failed install fails closed

- **WHEN** the `supervisor` install cannot complete (e.g. no network)
- **THEN** `core.start` raises a typed `CwcliError` explaining the install failed, and does NOT start the bench under honcho as a silent fallback

### Requirement: Auto-heal is delivered by supervisord config state, not a cwcli loop

The generated supervisord config SHALL make crashed programs self-heal via supervisord state, with no cwcli polling loop or foreground `--watch --heal` command.
Programs SHALL default to `autorestart=unexpected` (restart on a crash / unexpected exit, not on a clean exit), with `startretries` and `startsecs` bounding the retries so a program that keeps crashing enters supervisord's visible `FATAL` state instead of silently hammering.
Programs SHALL set `stopasgroup`/`killasgroup` so a stop or restart signals the whole process group and cleans up grandchildren.
A `--autorestart` / `--no-autorestart` toggle on `cwcli start` (and `cwcli axi start`) SHALL set this autorestart config state when the config is generated at launch.

#### Scenario: A crashed program is auto-restarted

- **WHEN** a supervised worker crashes (an unexpected exit) while the rest of the bench keeps running
- **THEN** supervisord restarts that worker on its own, without a cwcli process running and without touching the siblings

#### Scenario: A clean exit is not fought

- **WHEN** a program exits cleanly (expected exit)
- **THEN** `autorestart=unexpected` does not restart it, so an intentionally-stopped program is not fought

#### Scenario: A crash-loop reaches FATAL, not a silent hot loop

- **WHEN** a program crashes immediately on every restart beyond `startretries`
- **THEN** supervisord marks it `FATAL` (a visible, non-looping state) rather than hammering it silently

#### Scenario: --no-autorestart disables self-heal at start

- **WHEN** `cwcli start --no-autorestart <project>` runs
- **THEN** the generated config disables autorestart, so a crashed program stays down until an explicit restart

### Requirement: supervisor detection is keyed to supervisord-for-this-bench, reusing the discovery substrate

The `SUPERVISOR` identity SHALL be `supervisord`, and the supervisor marker SHALL record the supervisord identity plus its config path.
Detection SHALL identify the supervisord supervising THIS bench by its config path (or cwd), so a multi-bench instance never mis-attributes another bench's supervisor.
The existing discovery substrate SHALL be reused, not rewritten: the process-tree walk from the supervisor root, the cmdline-to-label mapping (unchanged, since the child processes are unchanged), the live Procfile expected-label parse, the bounded stop-and-wait-before-relaunch, and the multi-bench keying all carry over from honcho to supervisord.

#### Scenario: Detection keys supervisord to the right bench

- **WHEN** two benches in one instance each run their own supervisord
- **THEN** detection keyed to bench A's config path reports only bench A's supervisor and processes, never bench B's

#### Scenario: The marker records the supervisord identity and config

- **WHEN** `cwcli start` launches supervisord for a bench
- **THEN** the marker records `supervisor: "supervisord"` and the config path, so a later `cwcli status` can distinguish started-then-supervisor-down from never-started and can key detection on the config path

### Requirement: status reports supervisord's real per-process states and drops the all-or-nothing assumption

`core.status` SHALL enrich per-process health with supervisord's authoritative state token (`RUNNING`/`STARTING`/`BACKOFF`/`EXITED`/`FATAL`/`STOPPED`) read from `supervisorctl`, exposed as a `state` field on each process, in addition to the `ps`-derived PID/uptime/CPU/RSS.
`overall` SHALL no longer assume the stack is all-or-nothing: a stable partial stack (e.g. web serving while a worker is `FATAL`) SHALL report `degraded`, honestly, rather than `running`.
`overall` SHALL keep its four tokens (`offline`/`online`/`running`/`degraded`); the crash-loop/`FATAL` detail SHALL live in the per-process `state` field so a frontend can highlight it.

#### Scenario: A FATAL worker with web serving reports degraded

- **WHEN** `cwcli status <project>` runs on a bench where web is `RUNNING` but a worker is `FATAL`
- **THEN** `overall` is `degraded` (not `running`), and the worker's per-process `state` is `FATAL`

#### Scenario: A healthy stack reports running with per-process states

- **WHEN** `cwcli status <project>` runs on a bench where every expected program is `RUNNING` and the web probe answers
- **THEN** `overall` is `running` and each process carries its supervisord `state` (`RUNNING`)

### Requirement: Captured logs are per-process files with a synthesized combined view

Each supervised program SHALL write its own `stdout_logfile` under the bench `logs/` dir on the workspace volume (surviving a container restart), with supervisord's built-in size-based rotation, replacing the honcho combined-stream capper and the honcho line-prefix parse.
`cwcli logs --process <label>` SHALL tail one program's file directly; `cwcli logs` with no `--process` SHALL synthesize a combined view by reading the per-process files (label-attributed).
Restarting one program SHALL NOT disturb a surviving sibling's log file.

#### Scenario: Per-process logs are isolated across a restart

- **WHEN** one program is restarted while a sibling keeps running
- **THEN** the sibling's `stdout_logfile` is untouched, and the restarted program continues appending to its own file

#### Scenario: Combined and per-process views both available

- **WHEN** `cwcli logs <project>` runs with no `--process`
- **THEN** it synthesizes a combined, label-attributed view from the per-process files; **WHEN** run with `--process web`, it tails only the web program's file

### Requirement: Auto-relaunch of the supervisor on container boot is out of scope

The supervisord supervisor SHALL live and die with the container: it heals crashed programs on its own, but it SHALL NOT be auto-relaunched when the container itself restarts, because setting that up needs the image entrypoint, which cwcli cannot set via `docker exec`.
A `cwcli start` SHALL still be required to bring the bench back up after a container restart, unchanged from the honcho model.

#### Scenario: A container restart still needs cwcli start

- **WHEN** the frappe container restarts (taking supervisord and all programs down with it)
- **THEN** the programs do not come back on their own, and a `cwcli start <project>` is required to relaunch supervisord and the bench
