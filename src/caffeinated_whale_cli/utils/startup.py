"""
Platform-specific startup configuration for auto-inspect service.

Manages automatic startup of the auto-inspect background process on system boot/login
using platform-specific mechanisms:
- macOS: LaunchAgent plist files
- Linux: systemd user service
- Windows: Task Scheduler
"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path


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
    cwcli_path = python_dir / "cwcli"
    if cwcli_path.exists():
        return str(cwcli_path)

    # Last resort: just return "cwcli" and hope it's in PATH
    return "cwcli"


def is_startup_installed() -> bool:
    """Check if startup is currently installed for the current platform."""
    plat = get_platform()

    if plat == "darwin":
        return _is_macos_startup_installed()
    elif plat == "linux":
        return _is_linux_startup_installed()
    elif plat == "windows":
        return _is_windows_startup_installed()
    else:
        return False


def install_startup() -> bool:
    """Install platform-specific startup configuration."""
    plat = get_platform()

    if plat == "darwin":
        return _install_macos_startup()
    elif plat == "linux":
        return _install_linux_startup()
    elif plat == "windows":
        return _install_windows_startup()
    else:
        raise OSError(f"Unsupported platform: {plat}")


def uninstall_startup() -> bool:
    """Remove platform-specific startup configuration."""
    plat = get_platform()

    if plat == "darwin":
        return _uninstall_macos_startup()
    elif plat == "linux":
        return _uninstall_linux_startup()
    elif plat == "windows":
        return _uninstall_windows_startup()
    else:
        raise OSError(f"Unsupported platform: {plat}")


# =============================================================================
# macOS (LaunchAgent)
# =============================================================================


def _get_macos_plist_path() -> Path:
    """Get the path to the LaunchAgent plist file."""
    return Path.home() / "Library" / "LaunchAgents" / "com.cwcli.auto-inspect.plist"


def _is_macos_startup_installed() -> bool:
    """Check if macOS LaunchAgent is installed."""
    return _get_macos_plist_path().exists()


def _install_macos_startup() -> bool:
    """Install macOS LaunchAgent plist file."""
    plist_path = _get_macos_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    cwcli_path = get_cwcli_path()

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cwcli.auto-inspect</string>
    <key>ProgramArguments</key>
    <array>
        <string>{cwcli_path}</string>
        <string>config</string>
        <string>auto-inspect</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/cwcli-auto-inspect.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cwcli-auto-inspect.err</string>
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


def _uninstall_macos_startup() -> bool:
    """Remove macOS LaunchAgent plist file."""
    plist_path = _get_macos_plist_path()

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


def _get_linux_service_path() -> Path:
    """Get the path to the systemd user service file."""
    return Path.home() / ".config" / "systemd" / "user" / "cwcli-auto-inspect.service"


def _is_linux_startup_installed() -> bool:
    """Check if Linux systemd service is installed."""
    return _get_linux_service_path().exists()


def _install_linux_startup() -> bool:
    """Install Linux systemd user service."""
    service_path = _get_linux_service_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)

    cwcli_path = get_cwcli_path()

    service_content = f"""[Unit]
Description=Caffeinated Whale CLI Auto-Inspect Service
After=network.target

[Service]
Type=forking
ExecStart="{cwcli_path}" config auto-inspect start
ExecStop="{cwcli_path}" config auto-inspect stop
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
        ["systemctl", "--user", "enable", "cwcli-auto-inspect.service"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            print(f"systemctl enable failed: {result.stderr}", file=sys.stderr)
        return False

    result = subprocess.run(
        ["systemctl", "--user", "start", "cwcli-auto-inspect.service"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and result.stderr:
        print(f"systemctl start failed: {result.stderr}", file=sys.stderr)

    return result.returncode == 0


def _uninstall_linux_startup() -> bool:
    """Remove Linux systemd user service."""
    service_path = _get_linux_service_path()

    if not service_path.exists():
        return False

    # Stop and disable the service
    subprocess.run(
        ["systemctl", "--user", "stop", "cwcli-auto-inspect.service"],
        capture_output=True,
    )
    subprocess.run(
        ["systemctl", "--user", "disable", "cwcli-auto-inspect.service"],
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


def _is_windows_startup_installed() -> bool:
    """Check if Windows Task Scheduler task exists."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "CaffeinatedWhaleCliAutoInspect"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _install_windows_startup() -> bool:
    """Install Windows Task Scheduler task."""
    cwcli_path = get_cwcli_path()

    # Create a scheduled task that runs at logon
    command = [
        "schtasks",
        "/Create",
        "/TN",
        "CaffeinatedWhaleCliAutoInspect",
        "/TR",
        f'"{cwcli_path}" config auto-inspect start',
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


def _uninstall_windows_startup() -> bool:
    """Remove Windows Task Scheduler task."""
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", "CaffeinatedWhaleCliAutoInspect", "/F"],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
