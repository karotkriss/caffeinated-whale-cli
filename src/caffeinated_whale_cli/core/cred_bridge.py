"""``core.cred_bridge`` - the persistent credential-bridge desired-state verbs, UI-pure.

The ``config auto-inspect`` precedent, for a second config-gated background host
daemon: :func:`enable` fuses configure + start + ensure-running-instances,
:func:`disable` fuses stop + config-write + shim/socket/config hygiene,
:func:`start`/:func:`stop` are the daemon-only pair, and :func:`status` is the
read. The daemon spawn/kill, registry, and per-boot rotation mechanics stay in
``utils/cred_daemon.py`` (the storage/process layer this module calls); a
mechanics failure surfaces as ``CwcliError(INTERNAL)``.

Every decision lives HERE (never in a frontend), so ``cwcli config cred-bridge``
and any future GUI can never bypass them. Opt-in by design (captain ruling): the
feature is off until ``enable``; there is deliberately NO config-mutating ``axi``
verb (the registry-absence assertion stays true), only the read-only state on
``cwcli axi config`` / ``cwcli axi doctor``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from ..utils import config_utils
from ..utils import cred_daemon as daemon
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class CredBridgeState:
    """The bridge's state stores, read together, never fused."""

    enabled: bool  # config: the TOML flag
    daemon_running: bool  # process: a live daemon right now
    daemon_pid: int | None  # process: its pid, when running
    transport: str  # "unix" (native Linux) | "tcp" (Docker Desktop)
    registered_projects: list[str]  # instances currently wired into the bridge
    log_file: str
    recent_audit: list[str]  # the last few audit lines (never a credential byte)


@dataclass(frozen=True, slots=True, kw_only=True)
class CredBridgeOutcome:
    """What a mutation actually did (``actions`` tokens) plus the resulting state."""

    actions: list[str]
    state: CredBridgeState


def _state() -> CredBridgeState:
    running = daemon.is_running()
    return CredBridgeState(
        enabled=daemon.is_enabled(),
        daemon_running=running,
        daemon_pid=daemon.get_pid() if running else None,
        transport=daemon.transport(),
        registered_projects=daemon.registered_projects(),
        log_file=str(daemon.LOG_FILE),
        recent_audit=daemon.recent_audit(),
    )


def _start_daemon() -> None:
    try:
        daemon.start_daemon()
    except (RuntimeError, OSError) as e:
        raise CwcliError(
            ErrorKind.INTERNAL, "daemon.start_failed", f"Could not start credential bridge: {e}"
        ) from e


def _stop_daemon() -> None:
    try:
        daemon.stop_daemon()
    except (RuntimeError, OSError) as e:
        raise CwcliError(
            ErrorKind.INTERNAL, "daemon.stop_failed", f"Could not stop credential bridge: {e}"
        ) from e


def enable() -> Result[CredBridgeOutcome]:
    """Bring the credential bridge to the enabled-and-running desired state.

    Idempotent. Missing ``gh``/``glab`` is a WARNING, never a refusal - the
    daemon degrades per-tool exactly as ``host_credential`` already does (a host
    without the tool simply answers nothing), so a user may enable it before
    installing them. Writes the config once, starts the daemon, then best-effort
    ensures every currently-running instance so the bridge takes effect without
    waiting for the next open.
    """
    warnings: list[Message] = []
    if shutil.which("gh") is None and shutil.which("glab") is None:
        warnings.append(
            Message(
                "cred_bridge.no_host_tool",
                "Neither 'gh' nor 'glab' was found on PATH; the bridge will answer nothing "
                "until you install and authenticate one of them.",
            )
        )

    config_utils.set_cred_bridge_enabled(True)
    actions = ["config.enabled"]

    if daemon.is_running():
        actions.append("daemon.already_running")
    else:
        _start_daemon()
        actions.append("daemon.started")

    ensured = daemon.ensure_running_instances()
    if ensured:
        actions.append("instances.ensured")

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=CredBridgeOutcome(actions=actions, state=_state()),
        warnings=warnings,
    )


def disable() -> Result[CredBridgeOutcome]:
    """Bring the bridge fully down: stop the daemon, set enabled = false, and run
    the shim/socket/config hygiene (inert stubs, unlinked sockets, best-effort
    container config unset). Non-destructive: nothing an ``enable`` cannot
    recreate, so no ``--yes`` axis is needed."""
    actions: list[str] = []

    if daemon.is_running():
        _stop_daemon()
        actions.append("daemon.stopped")

    daemon.disable_bridge_artifacts()
    actions.append("artifacts.cleared")

    config_utils.set_cred_bridge_enabled(False)
    actions.append("config.disabled")

    return Result(status=Status.OK, data=CredBridgeOutcome(actions=actions, state=_state()))


def start() -> Result[CredBridgeOutcome]:
    """Start the daemon only, leaving config as-is. Refuses when the feature is
    disabled (the ``config auto-inspect start`` guard: a start against a disabled
    feature would run a daemon the config says should not exist)."""
    if not daemon.is_enabled():
        raise CwcliError(
            ErrorKind.USAGE,
            "cred_bridge.not_enabled",
            "The credential bridge is not enabled.",
            hint="run `cwcli config cred-bridge enable` first",
        )
    if daemon.is_running():
        return Result(
            status=Status.OK,
            data=CredBridgeOutcome(actions=["daemon.already_running"], state=_state()),
        )
    _start_daemon()
    return Result(
        status=Status.OK, data=CredBridgeOutcome(actions=["daemon.started"], state=_state())
    )


def stop() -> Result[CredBridgeOutcome]:
    """Stop the daemon only; config is untouched, so it auto-starts again on the
    next ``open``/``start`` while enabled. Idempotent."""
    if not daemon.is_running():
        return Result(
            status=Status.OK,
            data=CredBridgeOutcome(actions=["daemon.not_running"], state=_state()),
        )
    _stop_daemon()
    return Result(
        status=Status.OK, data=CredBridgeOutcome(actions=["daemon.stopped"], state=_state())
    )


def status() -> Result[CredBridgeState]:
    """Read all state stores; a pure read, never launches anything."""
    return Result(status=Status.OK, data=_state())
