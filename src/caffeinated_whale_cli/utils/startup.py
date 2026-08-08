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

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


class BootUnit(NamedTuple):
    """One daemon's boot-unit identity across the three platform mechanisms."""

    label: str  # macOS LaunchAgent label (com.cwcli.<name>)
    service_name: str  # Linux systemd user unit file name (<name>.service)
    task_name: str  # Windows Task Scheduler task name
    description: str  # human description (systemd Description=)
    start_args: tuple[str, ...]  # cwcli argv that starts the daemon
    stop_args: tuple[str, ...]  # cwcli argv that stops it

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
    stop_args=("config", "auto-inspect", "stop"),
)

CRED_BRIDGE = BootUnit(
    label="com.cwcli.cred-bridge",
    service_name="cwcli-cred-bridge.service",
    task_name="CaffeinatedWhaleCliCredBridge",
    description="Caffeinated Whale CLI Credential Bridge",
    start_args=("config", "cred-bridge", "start"),
    stop_args=("config", "cred-bridge", "stop"),
)


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


def install_startup(unit: BootUnit = AUTO_INSPECT) -> bool:
    """Install the unit's platform-specific startup configuration."""
    plat = get_platform()

    if plat == "darwin":
        return _install_macos_startup(unit)
    elif plat == "linux":
        return _install_linux_startup(unit)
    elif plat == "windows":
        return _install_windows_startup(unit)
    else:
        raise OSError(f"Unsupported platform: {plat}")


def uninstall_startup(unit: BootUnit = AUTO_INSPECT) -> bool:
    """Remove the unit's platform-specific startup configuration."""
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


def _install_macos_startup(unit: BootUnit) -> bool:
    """Install macOS LaunchAgent plist file."""
    plist_path = _get_macos_plist_path(unit)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    argv_lines = "\n".join(
        f"        <string>{arg}</string>" for arg in (get_cwcli_path(), *unit.start_args)
    )
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{unit.label}</string>
    <key>ProgramArguments</key>
    <array>
{argv_lines}
    </array>
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

    with open(plist_path, "w") as f:
        f.write(plist_content)

    # Load the LaunchAgent
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0 and result.stderr:
        # Log error for debugging but still return the result
        print(f"launchctl load failed: {result.stderr}", file=sys.stderr)
    return result.returncode == 0


def _uninstall_macos_startup(unit: BootUnit) -> bool:
    """Remove macOS LaunchAgent plist file."""
    plist_path = _get_macos_plist_path(unit)

    if not plist_path.exists():
        return False

    # Unload the LaunchAgent
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    # Remove the plist file
    plist_path.unlink()

    return True


# =============================================================================
# Linux (systemd)
# =============================================================================


def _get_linux_service_path(unit: BootUnit) -> Path:
    """Get the path to the systemd user service file."""
    return Path.home() / ".config" / "systemd" / "user" / unit.service_name


def _is_linux_startup_installed(unit: BootUnit) -> bool:
    """Check if Linux systemd service is installed."""
    return _get_linux_service_path(unit).exists()


def _install_linux_startup(unit: BootUnit) -> bool:
    """Install Linux systemd user service."""
    service_path = _get_linux_service_path(unit)
    service_path.parent.mkdir(parents=True, exist_ok=True)

    cwcli_path = get_cwcli_path()

    service_content = f"""[Unit]
Description={unit.description}
After=network.target

[Service]
Type=forking
ExecStart="{cwcli_path}" {" ".join(unit.start_args)}
ExecStop="{cwcli_path}" {" ".join(unit.stop_args)}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

    with open(service_path, "w") as f:
        f.write(service_content)

    # Reload systemd and enable the service
    result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
    )
    if result.returncode != 0:
        if result.stderr:
            print(f"systemctl daemon-reload failed: {result.stderr}", file=sys.stderr)
        return False

    result = subprocess.run(
        ["systemctl", "--user", "enable", unit.service_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            print(f"systemctl enable failed: {result.stderr}", file=sys.stderr)
        return False

    result = subprocess.run(
        ["systemctl", "--user", "start", unit.service_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and result.stderr:
        print(f"systemctl start failed: {result.stderr}", file=sys.stderr)

    return result.returncode == 0


def _uninstall_linux_startup(unit: BootUnit) -> bool:
    """Remove Linux systemd user service."""
    service_path = _get_linux_service_path(unit)

    if not service_path.exists():
        return False

    # Stop and disable the service
    subprocess.run(
        ["systemctl", "--user", "stop", unit.service_name],
        capture_output=True,
    )
    subprocess.run(
        ["systemctl", "--user", "disable", unit.service_name],
        capture_output=True,
    )

    # Remove the service file
    service_path.unlink()

    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    return True


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


def _install_windows_startup(unit: BootUnit) -> bool:
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
        return True
    except subprocess.CalledProcessError as e:
        if e.stderr:
            print(f"schtasks create failed: {e.stderr}", file=sys.stderr)
        return False


def _uninstall_windows_startup(unit: BootUnit) -> bool:
    """Remove Windows Task Scheduler task."""
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", unit.task_name, "/F"],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
