"""
Auto-inspection service for periodically inspecting running Frappe projects.

This module provides a background service that automatically inspects all running
Frappe projects at configurable intervals to keep cached data fresh for tab completion
and other features.
"""

import time
import signal
import sys
import os
from pathlib import Path
from typing import Optional
import docker
from . import config_utils

# PID file location
PID_DIR = Path.home() / "caffeinated-whale-cli" / "run"
PID_FILE = PID_DIR / "auto-inspect.pid"
LOG_FILE = PID_DIR / "auto-inspect.log"


def _ensure_pid_dir():
    """Ensure the PID directory exists."""
    PID_DIR.mkdir(parents=True, exist_ok=True)


def is_running() -> bool:
    """Check if the auto-inspect service is currently running."""
    if not PID_FILE.exists():
        return False

    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())

        # Check if process with this PID exists
        try:
            os.kill(pid, 0)  # Signal 0 doesn't kill, just checks if process exists
            return True
        except OSError:
            # Process doesn't exist, remove stale PID file
            PID_FILE.unlink()
            return False
    except (ValueError, FileNotFoundError):
        return False


def get_pid() -> Optional[int]:
    """Get the PID of the running auto-inspect service."""
    if not PID_FILE.exists():
        return None

    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except (ValueError, FileNotFoundError):
        return None


def _log(message: str):
    """Write a message to the log file."""
    _ensure_pid_dir()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")


def _get_running_projects() -> list[str]:
    """Get list of currently running Frappe projects."""
    try:
        client = docker.from_env()
        containers = client.containers.list(
            filters={"label": "com.docker.compose.service=frappe", "status": "running"}
        )

        projects = set()
        for container in containers:
            project_name = container.labels.get("com.docker.compose.project")
            if project_name:
                projects.add(project_name)

        return sorted(projects)
    except Exception as e:
        _log(f"Error getting running projects: {e}")
        return []


def _inspect_project(project_name: str) -> bool:
    """Inspect a single project and update its cache."""
    try:
        # Import here to avoid circular imports
        from ..commands.inspect import inspect as inspect_cmd

        # Run inspect command for this project
        inspect_cmd(
            project_name=project_name,
            verbose=False,
            json_output=False,
            update=False,
            show_apps=False,
            interactive=False,
        )
        return True
    except Exception as e:
        _log(f"Error inspecting project {project_name}: {e}")
        return False


def _run_inspection_cycle():
    """Run one complete inspection cycle for all running projects."""
    _log("Starting inspection cycle")

    projects = _get_running_projects()
    if not projects:
        _log("No running projects found")
        return

    _log(f"Found {len(projects)} running project(s): {', '.join(projects)}")

    for project in projects:
        _log(f"Inspecting {project}...")
        success = _inspect_project(project)
        if success:
            _log(f"Successfully inspected {project}")
        else:
            _log(f"Failed to inspect {project}")

    _log("Inspection cycle completed")


def start_daemon():
    """Start the auto-inspect daemon process."""
    if is_running():
        raise RuntimeError("Auto-inspect service is already running")

    config = config_utils.get_auto_inspect_config()
    if not config.get("enabled"):
        raise RuntimeError("Auto-inspect is not enabled in configuration")

    interval = config.get("interval", 3600)

    # Try to fork to background (Unix/Linux/macOS)
    try:
        pid = os.fork()

        if pid > 0:
            # Parent process - just return
            return

        # Child process - detach and run service
        os.setsid()

        # Redirect standard file descriptors
        sys.stdin = open("/dev/null", "r")
        sys.stdout = open("/dev/null", "a+")
        sys.stderr = open("/dev/null", "a+")

        # Write PID file
        _write_pid_file()

        # Set up signal handlers
        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

        # Log startup
        _log(f"Auto-inspect service started (interval: {interval}s)")

        # Run service loop
        _run_service_loop(interval)

    except (AttributeError, OSError):
        # Windows or fork failed - use threading instead
        import threading

        def run_service():
            _write_pid_file()
            _log(f"Auto-inspect service started (interval: {interval}s)")
            _run_service_loop(interval)

        thread = threading.Thread(target=run_service, daemon=False)
        thread.start()


def _write_pid_file():
    """Write the current process ID to the PID file."""
    _ensure_pid_dir()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _run_service_loop(interval: int):
    """Run the main service loop."""
    while True:
        try:
            _run_inspection_cycle()
        except Exception as e:
            _log(f"Error in inspection cycle: {e}")

        # Sleep until next inspection
        time.sleep(interval)


def _handle_sigterm(signum, frame):
    """Handle termination signal."""
    _log("Auto-inspect service received termination signal")
    stop_daemon()
    sys.exit(0)


def stop_daemon():
    """Stop the auto-inspect daemon process."""
    if not is_running():
        raise RuntimeError("Auto-inspect service is not running")

    pid = get_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            _log("Auto-inspect service stopped")

            # Wait for process to terminate
            for _ in range(10):
                if not is_running():
                    break
                time.sleep(0.1)

            # Force kill if still running
            if is_running():
                os.kill(pid, signal.SIGKILL)
                _log("Auto-inspect service force killed")
        except OSError as e:
            _log(f"Error stopping service: {e}")
            raise

    # Remove PID file
    if PID_FILE.exists():
        PID_FILE.unlink()


def get_log_tail(lines: int = 20) -> str:
    """Get the last N lines from the log file."""
    if not LOG_FILE.exists():
        return "No log file found"

    with open(LOG_FILE, "r") as f:
        all_lines = f.readlines()
        return "".join(all_lines[-lines:])
