---
name: cwcli-lifecycle
description: >
  Incident-level root-cause internals of cwcli's lifecycle and destructive commands - init
  (Frappe/bench version gating, admin/db-root secret env-transport, existing-bench reuse flow),
  rm (what deletion actually removes, conf/-only archive, recache-under-spinner deadlock, the
  C1/H5/M11 backup+name+exit-code data-safety gates, multi-bench per-bench backup, the
  start-back-up-delete transient-start flow for a stopped project), and restore
  (receive-mode and normal-path data-safety, non-interactive selectors, secrets off the argv,
  streamed copies, shared site detection, default-site resolution, post-restore migrate+restart),
  and start/status/logs/restart (the supervisord per-process supervision substrate: PID-keyed
  idempotency, config/launcher generation, per-process logs, the stdout-token-only status
  contract, single-program restart + auto-heal). Use this whenever you edit or debug
  core/init.py, commands/init.py, commands/rm.py, commands/restore.py, commands/start.py, commands/status.py,
  commands/restart.py, commands/logs.py, core/supervision.py, core/start.py, core/status.py,
  core/restart.py, core/logs.py, or utils/bench_sites.py - each note guards a real shipped bug, so
  keep the root-cause "why" so a later change does not silently re-break the fix.
metadata:
  internal: true
---

# cwcli lifecycle commands: init / rm / restore (sharp edges)

Each note guards a real shipped bug; keep the root-cause "why" so a later change does not silently
re-break the fix. The one-line contracts for these commands live in the always-loaded `AGENTS.md`
"Important Components" section; the deep incident detail is split by command into the reference
files below. Read only the one for the command you are touching.

| You are touching... | Read |
| --- | --- |
| `core/init.py` / `commands/init.py` - version gating, secret env-transport, existing-bench reuse, container-start spinner race, the exec-stream exit-code honesty | `references/init.md` |
| `commands/rm.py` - what deletion removes, conf/-only archive, recache-under-spinner deadlock, the C1/H5/M11 data-safety gates, multi-bench per-bench backup, the stopped-project start -> back up -> delete flow | `references/rm.md` |
| `commands/restore.py` - receive-mode + normal-path data-safety, selectors, secrets off argv, streamed copies, and the six restore+inspect bugs (shared `utils/bench_sites.py` site detection, default-site resolution, migrate+restart) | `references/restore.md` |
| `commands/start.py`, `status.py`, `restart.py`, `logs.py` + `core/supervision.py`, `start.py`, `status.py`, `restart.py`, `logs.py` - the supervisord per-process supervision substrate: PID-keyed idempotency (the `pkill` double-start bug), config/launcher generation, per-process log files, the stdout-token-only status contract, single-program restart + auto-heal, the restore-restart tension | `references/start-status.md` |

`rm` is the most safety-critical command in the codebase: its backup gate fails closed and a
regression there can silently destroy data. Read `references/rm.md` in full before changing it, and
validate any change with the full-lifecycle dangerous-delete E2E required by the `AGENTS.md`
"Captain standards" (real init -> seed data -> rm-with-backup, against the worktree's editable
install). The `restore`/`init` credential and confirm flows are the canonical example of the
"support BOTH interactive and non-interactive modes" captain standard - see the `auto_enter=False`
note in `references/restore.md` and the `cwcli-e2e-testing` skill for the pty recipe.
