"""``core.auto_inspect`` - the auto-inspect daemon + boot-hook orchestration, UI-pure.

The desired-state half of the ``rework-config-dx`` change (openspec, Decision 5):
:func:`enable` fuses configure + start + boot-hook sync, :func:`disable` fuses
stop + disable + hook removal, :func:`stop` stays the one daemon-only verb, and
:func:`status`/:func:`log_tail` are the reads. The daemon spawn/kill and the
boot-unit mechanics stay in ``utils/auto_inspect.py`` / ``utils/startup.py``
(the storage/process layer this module calls); a mechanics failure surfaces as
``CwcliError(INTERNAL)`` with the util's message.

The F3 fix is structural: :func:`enable` validates EVERY input before anything
persists, then performs ONE config write, so a failed enable can never leave
the config half-mutated the way the old ``enable --interval 30`` did.

:class:`AutoInspectState` separates the three state stores explicitly -
``enabled``/``interval``/``startup_enabled`` (the TOML), ``daemon_running``/
``daemon_pid`` (the live process), ``boot_installed`` (the OS boot unit) - so
every frontend renders them honestly rather than fusing them back into the
ten-verb ambiguity the rework removed.

The two Known-hazards entries (PID reuse, SIGTERM re-entry) live in the
untouched utils and are deliberately NOT absorbed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..utils import auto_inspect as daemon
from ..utils import config_utils, startup
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

MIN_INTERVAL = 60


@dataclass(frozen=True, slots=True, kw_only=True)
class AutoInspectState:
    """The three auto-inspect state stores, read together, never fused."""

    enabled: bool  # config: the TOML flag
    interval: int  # config: seconds between cycles
    daemon_running: bool  # process: a live daemon right now
    daemon_pid: int | None  # process: its pid, when running
    startup_enabled: bool  # config: the startup_enabled flag
    boot_installed: bool  # OS: the boot unit actually present
    log_file: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AutoInspectOutcome:
    """What a mutation actually did (``actions`` tokens) plus the resulting state."""

    actions: list[str]
    state: AutoInspectState


@dataclass(frozen=True, slots=True, kw_only=True)
class LogTail:
    """The last ``lines`` lines of the daemon log."""

    content: str
    lines: int
    log_file: str


def _state() -> AutoInspectState:
    config = config_utils.get_auto_inspect_config()
    running = daemon.is_running()
    return AutoInspectState(
        enabled=bool(config.get("enabled", False)),
        interval=int(config.get("interval", 3600)),
        daemon_running=running,
        daemon_pid=daemon.get_pid() if running else None,
        startup_enabled=bool(config.get("startup_enabled", False)),
        boot_installed=startup.is_startup_installed(),
        log_file=str(daemon.LOG_FILE),
    )


def _start_daemon() -> None:
    try:
        daemon.start_daemon()
    except (RuntimeError, OSError) as e:
        raise CwcliError(
            ErrorKind.INTERNAL, "daemon.start_failed", f"Could not start auto-inspect: {e}"
        ) from e


def _stop_daemon() -> None:
    try:
        daemon.stop_daemon()
    except (RuntimeError, OSError) as e:
        raise CwcliError(
            ErrorKind.INTERNAL, "daemon.stop_failed", f"Could not stop auto-inspect: {e}"
        ) from e


def _sync_boot_hook(at_boot: bool, actions: list[str], warnings: list[Message]) -> None:
    """Bring the OS boot unit to the desired state; a mechanics failure is a warning.

    Fail-soft deliberately (the old ``enable --startup`` behavior): the enable
    itself succeeded, and the DTO's ``startup_enabled``/``boot_installed`` split
    keeps the miss visible rather than lying about it.
    """
    try:
        installed = startup.is_startup_installed()
        if at_boot and not installed:
            if startup.install_startup():
                actions.append("hook.installed")
            else:
                warnings.append(
                    Message("startup.install_failed", "Could not install startup configuration.")
                )
        elif not at_boot and installed:
            if startup.uninstall_startup():
                actions.append("hook.removed")
            else:
                warnings.append(
                    Message("startup.uninstall_failed", "Could not remove startup configuration.")
                )
    except OSError as e:  # e.g. an unsupported platform
        warnings.append(Message("startup.sync_failed", f"Could not update boot startup: {e}"))


def enable(interval: int | None = None, at_boot: bool | None = None) -> Result[AutoInspectOutcome]:
    """Bring auto-inspect to the enabled-and-running desired state, idempotently.

    Validates first, writes the config ONCE, then starts the daemon (restarting
    it when a changed interval must take effect), then syncs the boot hook when
    ``at_boot`` was requested (``None`` leaves the hook untouched).
    """
    if interval is not None and interval < MIN_INTERVAL:
        raise CwcliError(
            ErrorKind.USAGE,
            "interval.too_small",
            f"Interval must be at least {MIN_INTERVAL} seconds.",
        )

    config = config_utils.load_config()
    previous_interval = int(config["auto_inspect"].get("interval", 3600))
    config["auto_inspect"]["enabled"] = True
    if interval is not None:
        config["auto_inspect"]["interval"] = interval
    if at_boot is not None:
        config["auto_inspect"]["startup_enabled"] = at_boot
    config_utils.save_config(config)

    interval_changed = interval is not None and interval != previous_interval

    actions = ["config.enabled"]
    if interval_changed:
        actions.append("interval.set")

    if daemon.is_running():
        if interval_changed:
            _stop_daemon()
            _start_daemon()
            actions.append("daemon.restarted")
        else:
            actions.append("daemon.already_running")
    else:
        _start_daemon()
        actions.append("daemon.started")

    warnings: list[Message] = []
    if at_boot is not None:
        _sync_boot_hook(at_boot, actions, warnings)

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=AutoInspectOutcome(actions=actions, state=_state()),
        warnings=warnings,
    )


def disable() -> Result[AutoInspectOutcome]:
    """Bring auto-inspect fully down: stop the daemon, disable, remove the boot hook."""
    actions: list[str] = []
    warnings: list[Message] = []

    if daemon.is_running():
        _stop_daemon()
        actions.append("daemon.stopped")

    config = config_utils.load_config()
    config["auto_inspect"]["enabled"] = False
    config["auto_inspect"]["startup_enabled"] = False
    config_utils.save_config(config)
    actions.append("config.disabled")

    _sync_boot_hook(False, actions, warnings)

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=AutoInspectOutcome(actions=actions, state=_state()),
        warnings=warnings,
    )


def stop() -> Result[AutoInspectOutcome]:
    """Stop the daemon only; config and boot hook stay (it returns at boot if hooked).

    Idempotent: asking to stop an already-stopped daemon is a no-op success.
    """
    if not daemon.is_running():
        return Result(
            status=Status.OK,
            data=AutoInspectOutcome(actions=["daemon.not_running"], state=_state()),
        )
    _stop_daemon()
    return Result(
        status=Status.OK, data=AutoInspectOutcome(actions=["daemon.stopped"], state=_state())
    )


def status() -> Result[AutoInspectState]:
    """Read all three state stores; a pure read, never launches anything."""
    return Result(status=Status.OK, data=_state())


def log_tail(lines: int = 20) -> Result[LogTail]:
    """The last ``lines`` lines of the daemon log; a read failure is a hard error."""
    try:
        content = daemon.get_log_tail(lines)
    except OSError as e:
        raise CwcliError(ErrorKind.INTERNAL, "logs.read_failed", f"Error reading logs: {e}") from e
    return Result(
        status=Status.OK,
        data=LogTail(content=content, lines=lines, log_file=str(daemon.LOG_FILE)),
    )
