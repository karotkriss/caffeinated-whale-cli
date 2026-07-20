---
name: cwcli
description: "Manage local Frappe/ERPNext Docker instances through the cwcli CLI - list instances, start/stop/restart them, check per-process health, back up and unlock sites, manage bench apps, and search cached apps and sites. Use whenever a task touches a local Frappe or ERPNext development instance, bench, or site: checking whether an instance is running, starting or restarting one, backing up or restoring a site, installing or updating bench apps, or finding which bench holds an app or site."
user-invocable: false
author: Christopher McKay
---

# cwcli

Manage Frappe and ERPNext Docker instances - structured and non-interactive.

cwcli manages Frappe/ERPNext Docker instances for local development: instance lifecycle, bench and site operations, and backup/restore.

Use the `cwcli axi` surface, not the bare human commands.
It is the agent-facing half of the same tool: every verb prints one TOON document on stdout, never prompts, and takes every decision as a flag.

You do not need cwcli installed globally - invoke it with `uvx --from caffeinated-whale-cli cwcli axi ...`.
If cwcli output suggests a follow-up command starting with `cwcli`, run it as `uvx --from caffeinated-whale-cli cwcli ...` instead.

cwcli needs a running Docker daemon.
If a command reports `error: Could not connect to Docker`, ask the user to start Docker rather than retrying.

## When to use

Use cwcli whenever a task touches a local Frappe or ERPNext instance: checking what instances exist and whether they are running, starting/stopping/restarting one, reading per-process health, backing up or unlocking a site, listing or updating a bench's apps, or finding which bench holds a given app or site.

## Workflow

1. Run `uvx --from caffeinated-whale-cli cwcli axi` with no arguments for the home view: the live instance list plus suggested next commands.
   Start here - it answers "what exists and is it up?" in one call.
2. Drill in with the verb you need: `status <project>`, `benches <project>`, `apps list <project>`, `backup <project> --site <site>`.
3. A project with several benches needs `--bench <index|label>`.
   When a verb reports `multiple benches; pass --bench`, run `axi benches <project>` - it is the verb that answers every other verb's `--bench`, and it lists the valid values.
4. Every response ends with `help:` hints. Follow them.

## Reading the output

Every verb prints one [TOON](https://toonformat.dev) document on stdout:

```
instances[2]{projectName,status,ports}:
  my-erp,running,8000 8001
  old-proj,exited,N/A
```

Errors are structured on stdout too, with an actionable `help:` line:

```
error: multiple benches; pass --bench <index|label>
options[2]:
  [0] /workspace/development/frappe-bench
  [1] /workspace/development/staging-bench
help: re-run with --bench <index|label>
```

Exit codes: **0** success (a completed operation, including one with warnings, and a successful read that reports bad news), **1** error, **2** usage error - a flag is missing or wrong, and the `help:` line names the fix.

## Verbs

```
  ls             List all Frappe/ERPNext instances; emit them as TOON.
  where          Search cached instances for apps/sites matching a string; emit matches as TOON.
  backup         Back up a site's database (and optionally files); emit the outcome as TOON.
  unlock         Remove a site's locks folder; emit the outcome as TOON.
  stop           Stop a project's containers; emit the outcome as TOON.
  start          Start a project's containers + bench; emit the outcome as TOON (never prompts).
  status         Report a project's per-process health; emit the report as TOON (`overall` first).
  logs           Read a bounded tail of a bench's per-process logs; emit them as ONE TOON document.
  restart        Restart ONE supervised process; emit the outcome as TOON (never prompts, no --watch).
  inspect        Inspect a project's benches, sites, and apps; emit the report as TOON.
  benches        List a project's benches with their indices and labels; emit them as TOON.
  label          Set or clear a bench's durable user label; emit the outcome as TOON.
  migrate        Run 'bench migrate' against ONE site under maintenance mode; emit the report as TOON.
  run-tests      Run 'bench run-tests' for ONE app against ONE named site; emit the report as TOON.
  config         Report the effective cwcli configuration; emit it as one TOON document. READ-ONLY.
  init           Provision a new instance, bench, and site; emit the report as TOON (never prompts).
  self-update    Report whether a newer cwcli is available; emit the check as TOON. READ-ONLY.
  setup          Install the SessionStart hook into every detected agent harness; emit TOON.
  apps list      List a bench's available apps, and (with --installed/--site) installed per site.
  apps update    Update app(s) and migrate affected sites; emit the report as TOON.
  apps checkout  Fetch and check out a ref into an app already in the bench; emit the report as TOON.
```

Run `uvx --from caffeinated-whale-cli cwcli axi <verb> --help` for a verb's flags.

## Rules

- **Never prompts.** A missing decision is a usage error naming the flag, never a hang. If a verb seems to want input, pass the flag its `help:` line names.
- **Read verbs exit 0 even when the news is bad.** `axi status` on a fully stopped project, and `axi self-update --check` on an outdated install, both succeeded - the answer is in the document, not the exit code.
- **`axi` output is always TOON, never JSON.** JSON lives on the human commands' `--json` flag. Do not pass `--json` to an `axi` verb.
- **Destructive verbs are deliberately absent.** There is no `axi apps install` or `axi apps uninstall` (uninstalling an app drops its tables), and no `axi restore` or `axi rm`. Ask the user to run the human command; do not work around this.

## Ambient context (optional)

`uvx --from caffeinated-whale-cli cwcli axi setup` installs a SessionStart hook into Claude Code, Codex, and OpenCode, so the home view lands in every session's opening context automatically.
It is the same information this skill describes, plus live state.
The user only needs one of the two.
