"""
Tips utility for displaying contextual tips during long-running operations.

Inspired by Claude Code's tip system, this module provides rotating tips
that help users discover features and best practices while waiting for
operations to complete.
"""

import random
from threading import Event, Thread

from rich.console import Console

# Curated tips aligned with cwcli features
TIPS = [
    # VS Code Integration
    "💡 Add VS Code to PATH via Command Palette: 'Shell Command: Install code command in PATH'",
    "💡 Use 'cwcli open <project> --code' to skip the editor selection prompt",
    "💡 VS Code Dev Containers extension is auto-installed when opening projects",
    "💡 Open specific apps with 'cwcli open <project> --app <app-name>'",
    # Tab Completion
    "💡 Install tab completion with 'cwcli --install-completion' for faster workflows",
    "💡 Tab completion works for project names, app names, and site names",
    "💡 Tab completion supports Bash, Zsh, Fish, and PowerShell",
    # Caching & Performance
    "💡 Use 'cwcli inspect <project>' to cache project structure for faster commands",
    "💡 The cache speeds up tab completion and other operations significantly",
    "💡 Clear project cache with 'cwcli config cache clear <project>'",
    "💡 Enable auto-inspect to keep your cache fresh automatically",
    # Port Management
    "💡 cwcli automatically detects and resolves port conflicts when starting projects",
    "💡 Use 'cwcli start' with multiple projects to batch-start them efficiently",
    "💡 Customize init ports with '--port 10000' to avoid conflicts (creates 10000-10005, 11000-11005)",
    # Update & Migrations
    "💡 Use '--build' flag with update to rebuild assets after pulling changes",
    "💡 Clear cache after migrations with '--clear-cache' for a clean state",
    "💡 Update multiple apps at once: 'cwcli update <project> --app frappe --app erpnext'",
    # Workflow Tips
    "💡 Use 'cwcli ls --json' to pipe project data to other tools like jq",
    "💡 Combine ls with grep to filter projects: 'cwcli ls -q | grep \"frappe-\"'",
    "💡 Use '-v' flag on any command for detailed diagnostic output",
    # Backup & Safety
    "💡 Run 'cwcli backup <project> --with-files' before major changes",
    "💡 Backups are stored in sites/<site>/private/backups/ by default",
    # Site Management
    "💡 Unlock stuck sites with 'cwcli unlock <project> --site <site-name>'",
    "💡 View all installed apps with 'cwcli inspect <project> --json'",
    # Logs & Debugging
    "💡 Follow bench logs in real-time with 'cwcli logs <project>'",
    "💡 Use '--lines' flag to control how many log lines are shown",
    # Auto-Inspect Feature
    "💡 Set up auto-inspect for background cache updates: 'cwcli config auto-inspect enable'",
    "💡 Auto-inspect can start on system boot with the '--startup' flag",
    "💡 Check auto-inspect status with 'cwcli config auto-inspect status'",
    # Configuration
    "💡 View config location with 'cwcli config path'",
    "💡 Add custom bench search paths with 'cwcli config add-path <path>'",
    # General Productivity
    "💡 Use 'cwcli status <project>' to check if your project is healthy",
    "💡 Run bench commands directly with 'cwcli run <project> <bench-args>'",
    "💡 Restart projects quickly with 'cwcli restart <project>'",
]


class TipRotator:
    """
    Manages rotation of tips with configurable interval.

    Tips are shuffled once and cycled through sequentially to avoid
    immediate repetition during long operations.
    """

    def __init__(self, tips: list[str] | None = None, interval: float = 4.0):
        """
        Initialize the tip rotator.

        Args:
            tips: List of tips to rotate through (defaults to TIPS)
            interval: Seconds between tip rotations (default: 4.0)
        """
        self.tips = tips or TIPS.copy()
        self.interval = interval
        self.current_index = 0
        self._shuffled_tips: list[str] | None = None

    def shuffle_tips(self) -> None:
        """Shuffle tips to randomize order while avoiding immediate repeats."""
        self._shuffled_tips = self.tips.copy()
        random.shuffle(self._shuffled_tips)
        self.current_index = 0

    def get_next_tip(self) -> str:
        """
        Get the next tip in rotation.

        Returns:
            The next tip string.
        """
        if self._shuffled_tips is None:
            self.shuffle_tips()

        tip = self._shuffled_tips[self.current_index]
        self.current_index = (self.current_index + 1) % len(self._shuffled_tips)

        # Re-shuffle after completing a full cycle
        if self.current_index == 0:
            self.shuffle_tips()

        return tip


class TipSpinner:
    """
    Context manager for displaying a spinner with rotating tips.

    Example:
        with TipSpinner("Inspecting project", console=console_err):
            # Long-running operation
            time.sleep(10)
    """

    def __init__(
        self,
        status_message: str,
        console: Console | None = None,
        spinner: str = "dots",
        tip_interval: float = 4.0,
        enabled: bool = True,
    ):
        """
        Initialize the tip spinner.

        Args:
            status_message: The main status message to display
            console: Rich Console instance (defaults to stderr Console)
            spinner: Rich spinner style (default: "dots")
            tip_interval: Seconds between tip rotations (default: 4.0)
            enabled: Whether tips are enabled (default: True)
        """
        self.status_message = status_message
        self.console = console or Console(stderr=True)
        self.spinner = spinner
        self.enabled = enabled

        self.rotator = TipRotator(interval=tip_interval)
        self._stop_event = Event()
        self._tip_thread: Thread | None = None
        self._status_context = None
        self._current_tip = ""

    def _format_status(self, tip: str = "") -> str:
        """Format the status message with optional tip."""
        if tip and self.enabled:
            return f"[bold green]{self.status_message}[/bold green]\n[dim]{tip}[/dim]"
        return f"[bold green]{self.status_message}[/bold green]"

    def _rotate_tips(self) -> None:
        """Background thread that rotates tips at intervals."""
        # Initial delay before first tip rotation
        if self._stop_event.wait(self.rotator.interval):
            return

        while not self._stop_event.is_set():
            self._current_tip = self.rotator.get_next_tip()
            # Update the status context with new tip
            if self._status_context is not None:
                self._status_context.update(self._format_status(self._current_tip))

            # Wait for interval or stop event
            if self._stop_event.wait(self.rotator.interval):
                break

    def __enter__(self):
        """Enter context: start spinner and tip rotation."""
        # Reset the stop event to allow reuse of the same TipSpinner instance
        self._stop_event.clear()

        # Initialize first tip
        self._current_tip = self.rotator.get_next_tip() if self.enabled else ""

        # Start the Rich status context
        self._status_context = self.console.status(
            self._format_status(self._current_tip), spinner=self.spinner
        )
        self._status_context.__enter__()

        # Start tip rotation thread if enabled
        if self.enabled:
            self._tip_thread = Thread(target=self._rotate_tips, daemon=True)
            self._tip_thread.start()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context: stop tip rotation and spinner."""
        # Signal tip rotation thread to stop
        if self._tip_thread:
            self._stop_event.set()
            self._tip_thread.join(timeout=1.0)

        # Exit the Rich status context
        if self._status_context:
            self._status_context.__exit__(exc_type, exc_val, exc_tb)

        return False

    def update(self, status_message: str) -> None:
        """
        Update the status message while keeping tip rotation.

        Args:
            status_message: New status message to display
        """
        self.status_message = status_message
        if self._status_context is not None:
            self._status_context.update(self._format_status(self._current_tip))


def get_tip() -> str:
    """
    Get a random tip from the tips collection.

    Returns:
        A random tip string.
    """
    return random.choice(TIPS)
