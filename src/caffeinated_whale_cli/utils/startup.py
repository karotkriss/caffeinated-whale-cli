"""
Platform-specific boot-persistence for cwcli's background daemons.

Installs, detects, and removes an opt-in "start at boot/login" unit using the
platform's own mechanism - macOS LaunchAgent plist, Linux systemd user service,
Windows Task Scheduler task - for any cwcli daemon described by a
:class:`BootUnit`. Two units exist today: auto-inspect (the original consumer;
it stays the default so pre-existing callers are untouched) and the persistent
credential bridge.

Every unit's start command is the daemon's own ``cwcli config <group> start``
verb, whose refuse-when-disabled guard is load-bearing: a boot unit left
installed after a ``disable`` execs a start that refuses, so a stale hook stays
inert rather than resurrecting a daemon the config says should not exist.
"""

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple


class BootUnit(NamedTuple):
    """One daemon's boot-unit identity across the three platform mechanisms."""

    label: str  # macOS LaunchAgent label (com.cwcli.<name>)
    service_name: str  # Linux systemd user unit file name (<name>.service)
    task_name: str  # Windows Task Scheduler task name
    description: str  # human description (systemd Description=)
    start_args: tuple[str, ...]  # cwcli argv that starts the daemon
    # True when start_args backgrounds the daemon and returns (``config <group>
    # start``); False when it runs in the foreground (``schedule daemon``), which a
    # systemd unit must start as Type=simple or systemd waits for it forever.
    forks: bool = True

    @property
    def log_stem(self) -> str:
        """The /tmp log-file stem the macOS LaunchAgent redirects to."""
        return self.service_name.removesuffix(".service")


AUTO_INSPECT = BootUnit(
    label="com.cwcli.auto-inspect",
    service_name="cwcli-auto-inspect.service",
    task_name="CaffeinatedWhaleCliAutoInspect",
    description="Caffeinated Whale CLI Auto-Inspect Service",
    start_args=("config", "auto-inspect", "start"),
)

SCHEDULER = BootUnit(
    label="com.cwcli.scheduler",
    service_name="cwcli-scheduler.service",
    task_name="CaffeinatedWhaleCliScheduler",
    description="Caffeinated Whale CLI Scheduler",
    start_args=("schedule", "daemon"),
    forks=False,
)

CRED_BRIDGE = BootUnit(
    label="com.cwcli.cred-bridge",
    service_name="cwcli-cred-bridge.service",
    task_name="CaffeinatedWhaleCliCredBridge",
    description="Caffeinated Whale CLI Credential Bridge",
    start_args=("config", "cred-bridge", "start"),
)


# A boot unit whose start keeps failing (its cwcli was uninstalled, moved, or
# exits non-zero) retries at most START_LIMIT_BURST times per
# START_LIMIT_INTERVAL_SEC, then stays failed until the next login or a manual
# start. Without it, systemd's default 10s limit window never trips at
# RestartSec=10, and a vanished cwcli logged a failure every few seconds forever.
START_LIMIT_INTERVAL_SEC = 600
START_LIMIT_BURST = 3


def get_platform() -> str:
    """Get the current platform (darwin, linux, windows)."""
    return platform.system().lower()


def get_cwcli_path() -> str:
    """Get the path to the cwcli executable."""
    # Try to find cwcli in PATH
    cwcli_path = shutil.which("cwcli")
    if cwcli_path:
        return cwcli_path

    # Fallback: use the Python executable path to construct cwcli path
    # This works when running from a virtual environment
    python_dir = Path(sys.executable).parent
    cwcli_candidate = python_dir / "cwcli"
    if cwcli_candidate.exists():
        return str(cwcli_candidate)

    # Last resort: just return "cwcli" and hope it's in PATH
    return "cwcli"


def running_cwcli_dir() -> str:
    """The directory of the cwcli executing now: the script ``sys.argv[0]`` names
    when that directory holds a ``cwcli``, else the interpreter's own directory.

    Unlike :func:`get_cwcli_path`, this never consults the caller's PATH, so a
    unit written by one install never points at another one that happens to come
    first on PATH.
    """
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv[0] else ""
    if script_dir and os.access(os.path.join(script_dir, "cwcli"), os.X_OK):
        return script_dir
    return str(Path(sys.executable).parent)


def unit_search_path() -> str:
    """The PATH a boot unit finds ``cwcli`` on.

    The unit runs ``/usr/bin/env cwcli`` rather than an absolute path, so it
    survives a reinstall or a move between a per-user and the shared install:
    the directory of the cwcli writing the unit comes first (see
    :func:`running_cwcli_dir`), then the usual per-user and shared install
    directories, so a removed install falls through to whichever cwcli remains.
    It is a superset of systemd's default PATH, so the daemons still find
    ``docker``/``gh``/``glab``.
    """
    dirs = [running_cwcli_dir(), str(Path.home() / ".local" / "bin")]
    if get_platform() == "darwin":
        dirs.append("/opt/homebrew/bin")
    dirs += ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def stale_reason(unit: BootUnit = AUTO_INSPECT) -> str | None:
    """Why the installed boot unit needs rewriting, or None when absent or current.

    Stale means its program no longer exists, or it was written by an older cwcli
    (no systemd restart limit, or a macOS absolute path). Read-only; an unreadable
    unit is never claimed stale. An old unit whose cwcli still exists repairs
    itself at the next boot, because the ``start`` verb it runs rewrites it.
    """
    inspect = {
        "darwin": lambda: _macos_unit_program(unit),
        "linux": lambda: _systemd_unit_program(_get_linux_service_path(unit)),
        "windows": lambda: _windows_unit_program(unit),
    }.get(get_platform())
    return _stale(inspect) if inspect else None


def system_unit_stale_reason(unit: BootUnit) -> str | None:
    """:func:`stale_reason` for the shared-mode system unit in ``/etc/systemd/system``.

    Read-only, so any user can run it; rewriting the unit needs root (see
    :func:`repair_system_unit`).
    """
    if get_platform() != "linux":
        return None
    path = system_unit_path(unit)
    if path.exists() and not os.access(path, os.R_OK):
        return "only root can read it, so it could not be checked"
    return _stale(lambda: _systemd_unit_program(path))


_UnitProgram = tuple[str, str | None, str | None] | None


def _stale(inspect: Callable[[], _UnitProgram]) -> str | None:
    try:
        found = inspect()
    except Exception:
        return None
    if found is None:
        return None
    named, resolved, outdated = found
    if resolved is None:
        return f"it runs {named}, which no longer exists"
    return outdated


def _resolve_program(argv: list[str], path: str | None) -> tuple[str, str | None]:
    """``(named, resolved)`` for a unit's argv: what it names and the file it runs."""
    if not argv:
        return "no program", None
    if argv[:1] == ["/usr/bin/env"] and len(argv) > 1:
        return f"{argv[1]} (on the unit's PATH)", shutil.which(argv[1], path=path or "")
    program = argv[0]
    return program, program if os.access(program, os.X_OK) else None


def repair_startup(unit: BootUnit = AUTO_INSPECT) -> bool:
    """Rewrite an installed boot unit that :func:`stale_reason` reports stale.

    Returns True when a unit was rewritten. Its enabled state is untouched and
    nothing is started: Linux only reloads systemd, macOS rewrites the plist
    launchd reads at the next login, and Windows replaces the logon task. A failed
    systemd reload restores the old file, so the next call retries, and raises
    because systemd is still using the old unit.
    """
    if stale_reason(unit) is None:
        return False
    plat = get_platform()
    if plat == "linux":
        service_path = _get_linux_service_path(unit)
        original = service_path.read_text()
        _write_linux_unit(unit)
        result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
        )
        if result.returncode != 0:
            service_path.write_text(original)
            raise OSError(_systemctl_reason("daemon-reload", result.stderr))
    elif plat == "darwin":
        _write_macos_plist(unit)
    else:
        ok, reason = _install_windows_startup(unit)
        if not ok:
            raise OSError(reason or "schtasks create failed")
    return True


def is_startup_installed(unit: BootUnit = AUTO_INSPECT) -> bool:
    """Check if the unit's startup is currently installed for this platform."""
    plat = get_platform()

    if plat == "darwin":
        return _is_macos_startup_installed(unit)
    elif plat == "linux":
        return _is_linux_startup_installed(unit)
    elif plat == "windows":
        return _is_windows_startup_installed(unit)
    else:
        return False


def install_startup(unit: BootUnit = AUTO_INSPECT) -> tuple[bool, str | None]:
    """Install the unit's platform-specific startup configuration.

    Returns ``(ok, reason)``: ``reason`` carries the underlying failure (e.g. the
    raw ``systemctl``/``launchctl``/``schtasks`` stderr) when ``ok`` is False, and
    is ``None`` on success. The reason is RETURNED rather than printed so the
    caller can state it in order alongside its own structured output instead of
    leaking a bare error line ahead of "enabled".
    """
    plat = get_platform()

    if plat == "darwin":
        return _install_macos_startup(unit)
    elif plat == "linux":
        return _install_linux_startup(unit)
    elif plat == "windows":
        return _install_windows_startup(unit)
    else:
        raise OSError(f"Unsupported platform: {plat}")


def uninstall_startup(unit: BootUnit = AUTO_INSPECT) -> tuple[bool, str | None]:
    """Remove the unit's platform-specific startup configuration.

    Returns ``(ok, reason)`` - see :func:`install_startup`.
    """
    plat = get_platform()

    if plat == "darwin":
        return _uninstall_macos_startup(unit)
    elif plat == "linux":
        return _uninstall_linux_startup(unit)
    elif plat == "windows":
        return _uninstall_windows_startup(unit)
    else:
        raise OSError(f"Unsupported platform: {plat}")


# =============================================================================
# macOS (LaunchAgent)
# =============================================================================


def _get_macos_plist_path(unit: BootUnit) -> Path:
    """Get the path to the LaunchAgent plist file."""
    return Path.home() / "Library" / "LaunchAgents" / f"{unit.label}.plist"


def _is_macos_startup_installed(unit: BootUnit) -> bool:
    """Check if macOS LaunchAgent is installed."""
    return _get_macos_plist_path(unit).exists()


def _render_macos_plist(unit: BootUnit) -> str:
    # KeepAlive is false, so launchd runs the unit once per login and never retries
    # a failed start: a vanished cwcli logs one failure, not a loop.
    argv_lines = "\n".join(
        f"        <string>{arg}</string>" for arg in ("/usr/bin/env", "cwcli", *unit.start_args)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{unit.label}</string>
    <key>ProgramArguments</key>
    <array>
{argv_lines}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{unit_search_path()}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/{unit.log_stem}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{unit.log_stem}.err</string>
</dict>
</plist>
"""


def _write_macos_plist(unit: BootUnit) -> Path:
    plist_path = _get_macos_plist_path(unit)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_render_macos_plist(unit))
    return plist_path


def _macos_unit_program(unit: BootUnit) -> tuple[str, str | None, str | None] | None:
    plist_path = _get_macos_plist_path(unit)
    if not plist_path.exists():
        return None
    data = plistlib.loads(plist_path.read_bytes())
    path = data.get("EnvironmentVariables", {}).get("PATH")
    named, resolved = _resolve_program(list(data.get("ProgramArguments", [])), path)
    outdated = None
    if data.get("ProgramArguments", [None])[0] != "/usr/bin/env":
        outdated = "it names an absolute path, which a reinstall or a move breaks"
    return named, resolved, outdated


def _install_macos_startup(unit: BootUnit) -> tuple[bool, str | None]:
    """Install macOS LaunchAgent plist file."""
    plist_path = _write_macos_plist(unit)

    # Load the LaunchAgent
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode == 0:
        return True, None
    reason = f"launchctl load failed: {result.stderr.strip()}" if result.stderr else None
    return False, reason


def _uninstall_macos_startup(unit: BootUnit) -> tuple[bool, str | None]:
    """Remove macOS LaunchAgent plist file."""
    plist_path = _get_macos_plist_path(unit)

    if not plist_path.exists():
        return False, "the LaunchAgent plist was not found"

    # Unload the LaunchAgent
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    # Remove the plist file
    plist_path.unlink()

    return True, None


# =============================================================================
# Linux (systemd)
# =============================================================================


def _systemctl_reason(step: str, stderr: str) -> str:
    """A one-line reason for a failed ``systemctl --user`` step (no session, unit
    missing, ...). Trimmed so it renders as a single structured warning line."""
    detail = stderr.strip().replace("\n", " ") if stderr else ""
    return f"systemctl {step} failed: {detail}" if detail else f"systemctl {step} failed"


def _get_linux_service_path(unit: BootUnit) -> Path:
    """Get the path to the systemd user service file."""
    return Path.home() / ".config" / "systemd" / "user" / unit.service_name


def _is_linux_startup_installed(unit: BootUnit) -> bool:
    """Check if Linux systemd service is installed."""
    return _get_linux_service_path(unit).exists()


# Deliberately NO ExecStop. When the daemon is already running (started outside
# systemd by `enable`, the web UI, or `cwcli open`), ExecStart's guarded start
# no-ops and leaves the unit with no process, so systemd stops the unit at once
# and runs ExecStop - which was `cwcli config <daemon> stop`, killing the live
# daemon the unit never owned. systemd's default stop (SIGTERM to the unit's own
# cgroup) already stops a daemon the unit DID start, and both daemons exit
# cleanly on SIGTERM.
def _render_linux_unit(unit: BootUnit) -> str:
    return f"""[Unit]
Description={unit.description}
After=network.target
StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}
StartLimitBurst={START_LIMIT_BURST}

[Service]
Type=forking
Environment="PATH={unit_search_path()}"
ExecStart=/usr/bin/env cwcli {" ".join(unit.start_args)}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _write_linux_unit(unit: BootUnit) -> None:
    service_path = _get_linux_service_path(unit)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(_render_linux_unit(unit))


def _systemd_unit_program(service_path: Path) -> tuple[str, str | None, str | None] | None:
    if not service_path.exists():
        return None
    text = service_path.read_text()
    exec_start = path = None
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            exec_start = line.removeprefix("ExecStart=")
        elif line.startswith("Environment="):
            for assignment in shlex.split(line.removeprefix("Environment=")):
                if assignment.startswith("PATH="):
                    path = assignment.removeprefix("PATH=")
    if not exec_start:
        return None
    named, resolved = _resolve_program(shlex.split(exec_start), path)
    outdated = None
    if "StartLimitBurst=" not in text:
        outdated = "it has no restart limit, so a failing start would retry forever"
    return named, resolved, outdated


def _install_linux_startup(unit: BootUnit) -> tuple[bool, str | None]:
    """Install Linux systemd user service."""
    _write_linux_unit(unit)

    # Reload systemd and enable the service
    result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, _systemctl_reason("daemon-reload", result.stderr)

    result = subprocess.run(
        ["systemctl", "--user", "enable", unit.service_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, _systemctl_reason("enable", result.stderr)

    result = subprocess.run(
        ["systemctl", "--user", "start", unit.service_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, _systemctl_reason("start", result.stderr)

    return True, None


def _uninstall_linux_startup(unit: BootUnit) -> tuple[bool, str | None]:
    """Remove Linux systemd user service."""
    service_path = _get_linux_service_path(unit)

    if not service_path.exists():
        return False, "the systemd user unit was not found"

    # Disable the service
    subprocess.run(
        ["systemctl", "--user", "disable", unit.service_name],
        capture_output=True,
    )

    # Remove the service file
    service_path.unlink()

    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    return True, None


# =============================================================================
# Windows (Task Scheduler)
# =============================================================================


def _is_windows_startup_installed(unit: BootUnit) -> bool:
    """Check if Windows Task Scheduler task exists."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", unit.task_name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _windows_unit_program(unit: BootUnit) -> tuple[str, str | None, str | None] | None:
    """The logon task's program. Task Scheduler runs it once per logon with no
    retry, so a missing program logs one failure, not a loop; it keeps an absolute
    path because a task action does not search PATH."""
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", unit.task_name, "/XML"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    command = ET.fromstring(result.stdout).find(".//{*}Command")
    if command is None or not command.text:
        return None
    program = command.text.strip().strip('"')
    return program, program if os.path.isfile(program) else None, None


def _install_windows_startup(unit: BootUnit) -> tuple[bool, str | None]:
    """Install Windows Task Scheduler task."""
    cwcli_path = get_cwcli_path()

    # Create a scheduled task that runs at logon
    command = [
        "schtasks",
        "/Create",
        "/TN",
        unit.task_name,
        "/TR",
        f'"{cwcli_path}" {" ".join(unit.start_args)}',
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/F",  # Force create (overwrite if exists)
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return True, None
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() if e.stderr else ""
        return False, f"schtasks create failed: {detail}" if detail else "schtasks create failed"


def _uninstall_windows_startup(unit: BootUnit) -> tuple[bool, str | None]:
    """Remove Windows Task Scheduler task."""
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", unit.task_name, "/F"],
            check=True,
            capture_output=True,
        )
        return True, None
    except subprocess.CalledProcessError as e:
        detail = (e.stderr.decode(errors="replace").strip() if e.stderr else "") or ""
        return False, f"schtasks delete failed: {detail}" if detail else "schtasks delete failed"


# =============================================================================
# Linux system unit (opt-in shared mode: a machine-wide daemon as a service account)
# =============================================================================
#
# The per-user systemd unit above (``systemctl --user``) runs only inside a login
# session and always as the invoking user. Shared mode needs the credential-bridge
# daemon to run machine-wide, at boot, as a dedicated ``cwcli`` service account -
# so it serves the ONE machine-owned read-only service-account token rather than
# whichever user last logged in (report 5.5). That is a SYSTEM unit in
# ``/etc/systemd/system`` with ``User=``/``Group=``, installed by
# ``cwcli setup shared`` (which already requires root). These functions are
# Linux-only; setup refuses on Windows and defers macOS (which needs a LaunchDaemon).

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
# World-readable (a unit holds no secret) so a non-root member's status/doctor can
# check it; shared mode's 0007 umask would otherwise leave it 0660 root:root.
SYSTEM_UNIT_MODE = 0o644


def system_unit_path(unit: BootUnit) -> Path:
    """Path to the system-wide systemd unit file."""
    return SYSTEM_UNIT_DIR / unit.service_name


def install_system_unit(unit: BootUnit, *, user: str, group: str, pid_file: str) -> bool:
    """Install + enable a system-wide systemd unit that runs the daemon as ``user``.

    Requires root (writes ``/etc/systemd/system`` and runs ``systemctl``); the
    caller (``cwcli setup shared``) has already checked. Linux-only.
    """
    if get_platform() != "linux":
        raise OSError("system units are only supported on Linux (systemd)")

    cwcli_path = get_cwcli_path()
    service_path = system_unit_path(unit)
    # No ExecStop, for the reason given above _write_linux_unit.
    kind = f"Type=forking\nUser={user}\nGroup={group}\nPIDFile={pid_file}"
    if not unit.forks:
        kind = f"Type=simple\nUser={user}\nGroup={group}"
    service_content = f"""[Unit]
Description={unit.description} (shared)
After=network.target docker.service
StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}
StartLimitBurst={START_LIMIT_BURST}

[Service]
{kind}
ExecStart="{cwcli_path}" {" ".join(unit.start_args)}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    service_path.write_text(service_content)
    os.chmod(service_path, SYSTEM_UNIT_MODE)

    result = subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
    if result.returncode != 0:
        if result.stderr:
            print(f"systemctl daemon-reload failed: {result.stderr}", file=sys.stderr)
        return False
    result = subprocess.run(
        ["systemctl", "enable", "--now", unit.service_name], capture_output=True, text=True
    )
    if result.returncode != 0 and result.stderr:
        print(f"systemctl enable failed: {result.stderr}", file=sys.stderr)
    return result.returncode == 0


def repair_system_unit(unit: BootUnit) -> bool:
    """Fix an installed system unit in place, keeping its ``User=``/``Group=``.

    Three fixes, each for a unit an older cwcli wrote: a foreground daemon (the
    scheduler) written as ``Type=forking`` with a pid file nothing writes, which
    systemd kills at the start timeout and restarts in a loop, becomes
    ``Type=simple``; a unit with no restart limit gets one; and an ``ExecStart``
    whose cwcli no longer exists runs this cwcli instead. The file is also made
    readable to every user (:data:`SYSTEM_UNIT_MODE`). Returns True when the
    file was rewritten (the caller reloads systemd); a missing or already-correct
    file is left alone. Needs root.
    """
    path = system_unit_path(unit)
    try:
        original = path.read_text()
        found = _systemd_unit_program(path)
        mode_ok = path.stat().st_mode & 0o777 == SYSTEM_UNIT_MODE
    except Exception:
        return False
    missing = found is not None and found[1] is None
    lines: list[str] = []
    for line in original.splitlines(keepends=True):
        if not unit.forks and line.startswith("PIDFile="):
            continue
        if not unit.forks and line.strip() == "Type=forking":
            line = "Type=simple\n"
        if missing and line.startswith("ExecStart="):
            line = f'ExecStart="{get_cwcli_path()}" {" ".join(unit.start_args)}\n'
        lines.append(line)
        if line.strip() == "[Unit]" and "StartLimitBurst=" not in original:
            lines.append(f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}\n")
            lines.append(f"StartLimitBurst={START_LIMIT_BURST}\n")
    repaired = "".join(lines)
    if "StartLimitBurst=" not in repaired:
        repaired = (
            f"[Unit]\nStartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}\n"
            f"StartLimitBurst={START_LIMIT_BURST}\n\n{repaired}"
        )
    if repaired == original and mode_ok:
        return False
    path.write_text(repaired)
    os.chmod(path, SYSTEM_UNIT_MODE)
    return True
