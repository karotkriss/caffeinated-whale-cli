"""Install the ``cwcli axi`` SessionStart hook into the supported agent harnesses.

An agent landing in a repo cannot use a surface it does not know exists. A
SessionStart hook runs ``cwcli axi`` (the content-first home) once per session and
feeds its TOON straight into the agent's opening context, so the surface plus the
live instance list are already there before the agent takes an action.

Three targets are supported, per the AXI standard's default app set: Claude Code,
Codex, and OpenCode. Each is installed ONLY when its config directory already
exists - cwcli does not scatter config dirs for harnesses the user does not run.

Every write here lands in a file the USER owns, which drives three rules:

* **Idempotent.** A hook already pointing at this executable is left alone
  (``unchanged``); only a moved executable is rewritten (``updated``).
* **Never reformat what we did not write.** ``~/.codex/config.toml`` is appended
  to as TEXT when it needs ``[features] hooks = true``, because ``toml.dump``
  drops the comments and section ordering out of a hand-maintained file. When a
  ``[features]`` section already exists but does not enable hooks, this reports
  ``manual`` and tells the user the one line to add rather than rewriting it. A
  ``settings.json``/``hooks.json`` this cannot parse gets the same treatment:
  ``manual``, file untouched, never silently replaced.
* **Explicit opt-in.** Nothing here runs off an ordinary command; it is reached
  only from the user-invoked ``cwcli axi setup``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import toml

#: What the hook runs. ``cwcli axi`` prints the content-first home as TOON.
_SUBCOMMAND = "axi"

#: Seconds the harness gives the hook before killing it. The home makes one
#: Docker call, so this is generous; a hung daemon must not stall a session.
_TIMEOUT = 10

#: Console-script names that mean "this is our hook" when found in a config.
_BIN_NAMES = ("cwcli", "caffeinated-whale-cli")


@dataclass(frozen=True)
class HookOutcome:
    """One target's result. ``status`` is installed/updated/unchanged/skipped/manual."""

    agent: str
    path: str
    status: str
    detail: str = ""


def hook_command() -> str:
    """The command a hook config should run.

    Per the AXI standard: use the bare binary name when PATH resolves it to the
    executable running right now, so a global install stays portable across
    machines; otherwise pin the absolute path, so the hook cannot silently run a
    DIFFERENT cwcli than the one the user set up.
    """
    current = _current_executable()
    on_path = shutil.which("cwcli")
    if on_path and current and _same_file(on_path, current):
        return f"cwcli {_SUBCOMMAND}"
    return f"{current or 'cwcli'} {_SUBCOMMAND}"


def _current_executable() -> str:
    raw = sys.argv[0]
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve())
    except OSError:  # pragma: no cover - resolve is robust in practice
        return raw


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_cwcli_hook(command: str) -> bool:
    """Does this existing hook entry belong to cwcli (at ANY executable path)?

    Matched on shape rather than an exact string so a hook installed when cwcli
    lived elsewhere is still recognised as ours and repaired, not duplicated.
    Splits on the LAST space, not ``.split()``: a Windows install path (or any
    path containing a space) is one token, not several.
    """
    binary, sep, subcommand = command.rpartition(" ")
    if not sep or subcommand != _SUBCOMMAND:
        return False
    name = Path(binary).name
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    return name in _BIN_NAMES


def _sync_session_start(config: dict, command: str) -> str:
    """Insert/repair our SessionStart entry in a Claude-Code-shaped hook config."""
    hooks = config.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    for group in session_start:
        for entry in group.get("hooks", []):
            if not _is_cwcli_hook(str(entry.get("command", ""))):
                continue
            if entry.get("command") == command:
                return "unchanged"
            entry["command"] = command
            return "updated"

    session_start.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": command, "timeout": _TIMEOUT}],
        }
    )
    return "installed"


class _ConfigUnreadableError(Exception):
    """An existing JSON hook config could not be safely parsed as an object."""


def _read_json(path: Path) -> dict:
    """Read an existing JSON config, distinguishing ABSENT (install fresh) from
    UNREADABLE (raise, so the caller refuses rather than overwrites)."""
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError) as exc:
        raise _ConfigUnreadableError(str(path)) from exc
    if not isinstance(loaded, dict):
        raise _ConfigUnreadableError(str(path))
    return loaded


def _install_json_hook(agent: str, path: Path, command: str) -> HookOutcome:
    try:
        config = _read_json(path)
    except _ConfigUnreadableError:
        # This file can hold the user's model settings, permissions, MCP config,
        # or another tool's hooks - never overwrite it blind. Mirrors the codex
        # TOML path below: report `manual` and leave it byte-for-byte untouched.
        return HookOutcome(
            agent, str(path), "manual", f"{path} is not valid JSON - fix or remove it by hand"
        )
    status = _sync_session_start(config, command)
    if status != "unchanged":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return HookOutcome(agent, str(path), status)


def _enable_codex_hooks(config_path: Path) -> str:
    """Ensure ``[features] hooks = true``, without reformatting a user's TOML.

    Codex ignores ``hooks.json`` entirely unless this flag is on, so installing
    the hook without it would report success for a hook that never fires.
    Returns ``unchanged`` (already set), ``enabled`` (just written - a real
    mutation the caller must not report as a no-op), or ``manual``.
    """
    try:
        config = toml.load(config_path) if config_path.exists() else {}
    except (toml.TomlDecodeError, OSError):
        return "manual"

    features = config.get("features")
    if isinstance(features, dict) and features.get("hooks") is True:
        return "unchanged"
    if features is not None:
        # A [features] section exists but does not enable hooks. Rewriting the
        # file would strip the user's comments, so ask rather than clobber.
        return "manual"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}\n[features]\nhooks = true\n")
    return "enabled"


def _opencode_plugin(command: str) -> str:
    """The OpenCode plugin source. OpenCode has no hook config; it loads plugins."""
    binary, _, subcommand = command.rpartition(" ")
    return f"""\
// cwcli-managed OpenCode plugin: injects `cwcli axi` ambient context.
// Generated by `cwcli axi setup`. Re-run that command to repair the path.
import {{ spawn }} from "node:child_process";

const BIN = {json.dumps(binary)};
const ARGS = {json.dumps([subcommand])};
const HEADER = "## AXI ambient context: cwcli";
const TIMEOUT_MS = {_TIMEOUT * 1000};

function runHomeView(cwd) {{
  return new Promise((resolve) => {{
    const child = spawn(BIN, ARGS, {{
      cwd: cwd && cwd.length > 0 ? cwd : process.cwd(),
      env: process.env,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    }});

    let stdout = "";
    let settled = false;
    const finish = (value) => {{
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    }};
    const timer = setTimeout(() => {{
      child.kill("SIGTERM");
      finish("");
    }}, TIMEOUT_MS);

    child.stdout?.setEncoding("utf-8");
    child.stdout?.on("data", (chunk) => {{
      stdout += chunk;
    }});
    // A missing binary or an unreachable Docker daemon must never break a
    // session: fall back to no ambient context rather than surfacing an error.
    child.on("error", () => finish(""));
    child.on("close", (code) => finish(code === 0 ? stdout.trim() : ""));
  }});
}}

export const AxiCwcliAmbientContextPlugin = async ({{ directory }}) => {{
  const cache = new Map();
  return {{
    "experimental.chat.system.transform": async (input, output) => {{
      const sessionID = input.sessionID ?? "__global__";
      if (!cache.has(sessionID)) {{
        cache.set(sessionID, await runHomeView(directory));
      }}
      const homeView = cache.get(sessionID);
      if (homeView) output.system.push(HEADER + "\\n" + homeView);
    }},
  }};
}};
"""


def install(command: str | None = None, *, home: Path | None = None) -> list[HookOutcome]:
    """Install/repair the SessionStart hook for every detected agent harness."""
    command = command or hook_command()
    root = home or Path.home()
    outcomes: list[HookOutcome] = []

    # -- Claude Code: native SessionStart hook in settings.json.
    claude_dir = root / ".claude"
    if claude_dir.is_dir():
        outcomes.append(_install_json_hook("claude-code", claude_dir / "settings.json", command))
    else:
        outcomes.append(HookOutcome("claude-code", str(claude_dir), "skipped", "not detected"))

    # -- Codex: same hook shape in hooks.json, gated on a config.toml feature flag.
    codex_dir = root / ".codex"
    if codex_dir.is_dir():
        outcome = _install_json_hook("codex", codex_dir / "hooks.json", command)
        toml_status = _enable_codex_hooks(codex_dir / "config.toml")
        if toml_status == "manual" and outcome.status != "manual":
            outcome = HookOutcome(
                outcome.agent,
                outcome.path,
                "manual",
                "add 'hooks = true' under [features] in ~/.codex/config.toml",
            )
        elif toml_status == "enabled" and outcome.status == "unchanged":
            # hooks.json needed no change, but config.toml just got its first
            # write - a real mutation, so this must not read as a no-op.
            outcome = HookOutcome(
                outcome.agent,
                outcome.path,
                "updated",
                "enabled 'hooks = true' in ~/.codex/config.toml",
            )
        outcomes.append(outcome)
    else:
        outcomes.append(HookOutcome("codex", str(codex_dir), "skipped", "not detected"))

    # -- OpenCode: a managed plugin; the file IS the integration, so it is
    #    rewritten whenever its contents drift from what this version generates.
    opencode_dir = root / ".config" / "opencode"
    plugin_path = opencode_dir / "plugins" / "axi-cwcli.js"
    if opencode_dir.is_dir():
        source = _opencode_plugin(command)
        if plugin_path.exists() and plugin_path.read_text(encoding="utf-8") == source:
            status = "unchanged"
        else:
            status = "updated" if plugin_path.exists() else "installed"
            plugin_path.parent.mkdir(parents=True, exist_ok=True)
            plugin_path.write_text(source, encoding="utf-8")
        outcomes.append(HookOutcome("opencode", str(plugin_path), status))
    else:
        outcomes.append(HookOutcome("opencode", str(opencode_dir), "skipped", "not detected"))

    return outcomes
