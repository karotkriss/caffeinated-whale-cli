#!/usr/bin/env python3
"""Render the installable `cwcli` Agent Skill from the CLI's own definitions.

The skill is the SECOND discovery path for the agent surface: the SessionStart
hook (`cwcli axi setup`) carries live state but needs harness support and costs
tokens on every session; the skill loads on demand, in any agent, for free. A
user installs whichever fits, or both.

Single source of truth: the verb table is built by walking the live `cwcli axi`
Typer app, so a verb added or renamed WITHOUT regenerating this file fails the
staleness check rather than shipping a skill that teaches a surface that moved.
The prose is hand-written here because prose is what a skill is for; only what
the CLI already knows about itself is generated.

    uv run python scripts/build_skill.py            # write skills/cwcli/SKILL.md
    uv run python scripts/build_skill.py --check    # fail if it is stale (CI)

The `--check` step runs as a unit test (`tests/test_axi_skill.py`), so the
existing test gate covers it and no extra CI workflow step is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from caffeinated_whale_cli.commands import axi  # noqa: E402

SKILL_PATH = REPO_ROOT / "skills" / "cwcli" / "SKILL.md"

SKILL_NAME = "cwcli"
SKILL_AUTHOR = "Christopher McKay"

# The trigger an agent matches to load the skill: terse and outcome-focused, so
# it fires on "needs to touch a local Frappe instance" rather than on the tool's
# name, which an agent that has never heard of cwcli would never think to say.
SKILL_DESCRIPTION = (
    "Manage local Frappe/ERPNext Docker instances through the cwcli CLI - list instances, "
    "start/stop/restart them, check per-process health, back up and unlock sites, manage "
    "bench apps, and search cached apps and sites. Use whenever a task touches a local "
    "Frappe or ERPNext development instance, bench, or site: checking whether an instance "
    "is running, starting or restarting one, backing up or restoring a site, installing or "
    "updating bench apps, or finding which bench holds an app or site."
)

# How an agent runs cwcli without a global install. `--from` is required because
# the console script name differs from the package name.
UVX = "uvx --from caffeinated-whale-cli cwcli"


def _verb_rows() -> list[tuple[str, str]]:
    """Walk the live `cwcli axi` Typer app for (verb, one-line help).

    Reading Typer's own registry rather than keeping a hand-maintained list is
    what makes the staleness check meaningful: there is nothing to forget to
    update except this file itself.
    """
    rows: list[tuple[str, str]] = []
    for command in axi.app.registered_commands:
        rows.append((command.name or "", _summary(command)))
    for group in axi.app.registered_groups:
        for command in group.typer_instance.registered_commands:  # type: ignore[union-attr]
            rows.append((f"{group.name} {command.name}", _summary(command)))
    return rows


def _summary(command) -> str:
    """A command's first help line: its short description, without the essay.

    Docstrings here are reStructuredText, so ``x`` is collapsed to `x` - the
    skill is rendered as Markdown, where a double backtick is just noise.
    """
    text = command.help or (command.callback.__doc__ or "")
    summary = text.strip().split("\n")[0].strip()
    return summary.replace("``", "`")


def _verb_table() -> str:
    rows = _verb_rows()
    width = max(len(name) for name, _ in rows)
    return "\n".join(f"  {name.ljust(width)}  {summary}" for name, summary in rows)


def render() -> str:
    """The full SKILL.md text."""
    return f"""\
---
name: {SKILL_NAME}
description: "{SKILL_DESCRIPTION}"
user-invocable: false
author: {SKILL_AUTHOR}
---

# cwcli

{axi._DESCRIPTION}

cwcli manages Frappe/ERPNext Docker instances for local development: instance lifecycle, bench and site operations, and backup/restore.

Use the `cwcli axi` surface, not the bare human commands.
It is the agent-facing half of the same tool: every verb prints one TOON document on stdout, never prompts, and takes every decision as a flag.

You do not need cwcli installed globally - invoke it with `{UVX} axi ...`.
If cwcli output suggests a follow-up command starting with `cwcli`, run it as `{UVX} ...` instead.

cwcli needs a running Docker daemon.
If a command reports `error: Could not connect to Docker`, ask the user to start Docker rather than retrying.

## When to use

Use cwcli whenever a task touches a local Frappe or ERPNext instance: checking what instances exist and whether they are running, starting/stopping/restarting one, reading per-process health, backing up or unlocking a site, listing or updating a bench's apps, or finding which bench holds a given app or site.

## Workflow

1. Run `{UVX} axi` with no arguments for the home view: the live instance list plus suggested next commands.
   Start here - it answers "what exists and is it up?" in one call.
2. Drill in with the verb you need: `status <project>`, `benches <project>`, `apps list <project>`, `backup <project> --site <site>`.
3. A project with several benches needs `--bench <index|label>`.
   When a verb reports `multiple benches; pass --bench`, run `axi benches <project>` - it is the verb that answers every other verb's `--bench`, and it lists the valid values.
4. When a response includes a `help:` hint, follow it.

## Reading the output

Every verb prints one [TOON](https://toonformat.dev) document on stdout:

```
instances[2]{{projectName,status,ports}}:
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
{_verb_table()}
```

Run `{UVX} axi <verb> --help` for a verb's flags.

## Rules

- **Never prompts.** A missing decision is a usage error naming the flag, never a hang. If a verb seems to want input, pass the flag its `help:` line names.
- **Read verbs exit 0 even when the news is bad, with one exception: `axi doctor`.** `axi status` on a fully stopped project, and `axi self-update --check` on an outdated install, both succeeded - the answer is in the document, not the exit code. `axi doctor` is a chainable preflight gate and exits non-zero when a check fails by design; read its `ok`/`failed` fields rather than treating the exit code as "doctor itself failed".
- **`axi` output is always TOON, never JSON.** JSON lives on the human commands' `--json` flag. Do not pass `--json` to an `axi` verb.
- **Destructive access is verb-specific.** Registered verbs such as `axi rm` and `axi rm-site` carry their own consent and data-safety gates; follow each verb's help exactly. There is no `axi apps uninstall` (uninstalling an app drops its tables) or `axi restore`. Ask the user to run the human command for those operations; do not work around their absence.

## Ambient context (optional)

`{UVX} axi setup` installs a SessionStart hook into Claude Code, Codex, and OpenCode, so the home view lands in every session's opening context automatically.
It is the same information this skill describes, plus live state.
The user only needs one of the two.
"""


def main() -> int:
    content = render()

    if "--check" in sys.argv:
        if not SKILL_PATH.exists():
            print(f"skill:check failed - {SKILL_PATH} does not exist. Run:", file=sys.stderr)
            print("  uv run python scripts/build_skill.py", file=sys.stderr)
            return 1
        if SKILL_PATH.read_text(encoding="utf-8") != content:
            print("skill:check failed - SKILL.md is stale. Run:", file=sys.stderr)
            print("  uv run python scripts/build_skill.py", file=sys.stderr)
            return 1
        print("skill:check passed - SKILL.md is up to date.")
        return 0

    SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {SKILL_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
