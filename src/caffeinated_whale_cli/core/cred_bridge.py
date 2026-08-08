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

Phase 3 adds three opt-in axes, none changing a do-nothing user's behavior:
``enable(at_boot=...)`` syncs an OS boot unit (the shared
``core.auto_inspect.sync_boot_hook`` with ``startup.CRED_BRIDGE``; the unit
execs ``cwcli config cred-bridge start``, whose refuse-when-disabled guard keeps
a stale unit inert), the ``[cred_bridge] allowed_hosts`` config key scopes which
hosts the daemon answers (enforced in ``credbridge.host_credential``, read per
request), and ``enable`` tightens the projects dir to owner-only (the cache-dir
0700 precedent).
"""

from __future__ import annotations

import contextlib
import shutil
import stat
from dataclasses import dataclass

from ..utils import config_utils, startup
from ..utils import cred_daemon as daemon
from .auto_inspect import sync_boot_hook
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class CredBridgeState:
    """The bridge's state stores, read together, never fused."""

    enabled: bool  # config: the TOML flag
    daemon_running: bool  # process: a live daemon right now
    daemon_pid: int | None  # process: its pid, when running
    transport: str  # "unix" (native Linux) | "tcp" (Docker Desktop)
    startup_enabled: bool  # config: start the daemon at boot/login
    boot_installed: bool  # OS: the boot unit actually present
    allowed_hosts: list[str]  # config: hosts the daemon answers for (exact match)
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
    config = config_utils.read_cred_bridge_config()
    return CredBridgeState(
        enabled=daemon.is_enabled(),
        daemon_running=running,
        daemon_pid=daemon.get_pid() if running else None,
        transport=daemon.transport(),
        startup_enabled=bool(config.get("startup_enabled", False)),
        boot_installed=startup.is_startup_installed(startup.CRED_BRIDGE),
        allowed_hosts=sorted(daemon.allowed_hosts()),
        registered_projects=daemon.registered_projects(),
        log_file=str(daemon.LOG_FILE),
        recent_audit=daemon.recent_audit(),
    )


def _harden_projects_dir(actions: list[str]) -> None:
    """Tighten ``~/.cwcli/projects`` to owner-only (0700) at enable time.

    The per-instance socket lives inside a workspace dir under it, and the daemon
    keeps that socket alive for its whole lifetime, so the multi-user-host window
    is long; the cache dir already ships 0700 (the same precedent), and the owner
    sees no behavior change. Best-effort and reported only when the mode actually
    changed - a missing dir or a chmod-less filesystem is silently left alone.
    """
    projects = config_utils.PROJECTS_DIR
    with contextlib.suppress(OSError):
        if projects.is_dir():
            if stat.S_IMODE(projects.stat().st_mode) != 0o700:
                projects.chmod(0o700)
                actions.append("projects.hardened")


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


def enable(at_boot: bool | None = None) -> Result[CredBridgeOutcome]:
    """Bring the credential bridge to the enabled-and-running desired state.

    Idempotent. Missing ``gh``/``glab`` is a WARNING, never a refusal - the
    daemon degrades per-tool exactly as ``host_credential`` already does (a host
    without the tool simply answers nothing), so a user may enable it before
    installing them. Writes the config, hardens the projects dir to owner-only,
    starts the daemon, best-effort ensures every currently-running instance so
    the bridge takes effect without waiting for the next open, then syncs the
    boot unit when ``at_boot`` was requested (``None`` leaves it untouched -
    the ``core.auto_inspect.enable`` shape).
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
    if at_boot is not None:
        config_utils.set_cred_bridge_startup(at_boot)

    _harden_projects_dir(actions)

    if daemon.is_running():
        actions.append("daemon.already_running")
    else:
        _start_daemon()
        actions.append("daemon.started")

    ensured = daemon.ensure_running_instances()
    if ensured:
        actions.append("instances.ensured")

    if at_boot is not None:
        sync_boot_hook(at_boot, actions, warnings, unit=startup.CRED_BRIDGE)

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=CredBridgeOutcome(actions=actions, state=_state()),
        warnings=warnings,
    )


def disable() -> Result[CredBridgeOutcome]:
    """Bring the bridge fully down: stop the daemon, set enabled = false, run
    the shim/socket/config hygiene (inert stubs, unlinked sockets, best-effort
    container config unset), and remove the boot unit. Non-destructive: nothing
    an ``enable`` cannot recreate, so no ``--yes`` axis is needed."""
    actions: list[str] = []
    warnings: list[Message] = []

    if daemon.is_running():
        _stop_daemon()
        actions.append("daemon.stopped")

    daemon.disable_bridge_artifacts()
    actions.append("artifacts.cleared")

    config_utils.set_cred_bridge_enabled(False)
    config_utils.set_cred_bridge_startup(False)
    actions.append("config.disabled")

    sync_boot_hook(False, actions, warnings, unit=startup.CRED_BRIDGE)

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=CredBridgeOutcome(actions=actions, state=_state()),
        warnings=warnings,
    )


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
