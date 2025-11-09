import time

import questionary
import typer
from rich.console import Console
from rich.table import Table

from ..utils import auto_inspect, config_utils, db_utils, startup

app = typer.Typer(help="Manage CLI configuration and cache.")
cache_app = typer.Typer(help="Manage the cache.")
auto_inspect_app = typer.Typer(help="Manage automatic project inspection.")
tips_app = typer.Typer(help="Manage contextual tips display.")
app.add_typer(cache_app, name="cache")
app.add_typer(auto_inspect_app, name="auto-inspect")
app.add_typer(tips_app, name="tips")

console = Console()


@app.command("path")
def config_path():
    """
    Display the path to the configuration file.
    """
    console.print(f"Config file is located at: [green]{config_utils.CONFIG_FILE}[/green]")


@app.command("add-path")
def add_path(
    path: str = typer.Argument(..., help="The absolute path to add to the custom search paths.")
):
    """
    Add a custom bench search path to the configuration.
    """
    if config_utils.add_custom_path(path):
        console.print(f"[green]Added '{path}' to custom search paths.[/green]")
    else:
        console.print(f"[yellow]'{path}' already exists in custom search paths.[/yellow]")


@app.command("remove-path")
def remove_path(
    path: str = typer.Argument(..., help="The path to remove from the custom search paths.")
):
    """
    Remove a custom bench search path from the configuration.
    """
    if config_utils.remove_custom_path(path):
        console.print(f"[green]Removed '{path}' from custom search paths.[/green]")
    else:
        console.print(f"[yellow]'{path}' not found in custom search paths.[/yellow]")


@cache_app.command("clear")
def clear_cache(
    project_name: str = typer.Argument(
        None, help="The name of the project to clear from the cache."
    ),
    all: bool = typer.Option(False, "--all", "-a", help="Clear the entire cache."),
):
    """
    Clear the cache for a specific project or the entire cache.
    """
    if all:
        confirmed = questionary.confirm("Are you sure you want to clear the entire cache?").ask()
        if confirmed:
            db_utils.clear_all_cache()
            console.print("[green]Entire cache has been cleared.[/green]")
        else:
            console.print("Operation cancelled.")
    elif project_name:
        if db_utils.clear_cache_for_project(project_name):
            console.print(f"Cache for project '[bold cyan]{project_name}[/bold cyan]' cleared.")
        else:
            console.print(
                f"[yellow]No cache found for project '[bold cyan]{project_name}[/bold cyan]'.[/yellow]"
            )
    else:
        console.print("Please specify a project name or use the --all flag.")


@cache_app.command("path")
def cache_path():
    """
    Display the path to the cache file.
    """
    console.print(f"Cache file is located at: [green]{db_utils.DB_PATH}[/green]")


@cache_app.command("list")
def list_cached_projects():
    """
    List all projects currently in the cache.
    """
    projects = db_utils.get_all_cached_projects()
    if not projects:
        console.print("[yellow]No projects found in the cache.[/yellow]")
        return

    table = Table(title="Cached Projects")
    table.add_column("Project Name", style="cyan")
    table.add_column("Last Updated", style="magenta")

    for project in projects:
        table.add_row(project.name, str(project.last_updated))

    console.print(table)


@auto_inspect_app.command("enable")
def enable_auto_inspect(
    interval: int = typer.Option(
        None,
        "--interval",
        "-i",
        help="Inspection interval in seconds (minimum 60, default 3600)",
    ),
    enable_startup: bool = typer.Option(
        False,
        "--startup",
        help="Also enable automatic startup on system boot/login",
    ),
):
    """
    Enable automatic project inspection.
    """
    try:
        config_utils.set_auto_inspect_enabled(True)
        console.print("[green]Auto-inspect enabled.[/green]")

        if interval:
            if interval < 60:
                console.print("[red]Error: Interval must be at least 60 seconds.[/red]")
                return
            config_utils.set_auto_inspect_interval(interval)
            console.print(f"[green]Inspection interval set to {interval} seconds.[/green]")

        if enable_startup:
            if startup.install_startup():
                config_utils.set_auto_inspect_startup(True)
                console.print(
                    "[green]Startup enabled. Auto-inspect will start automatically on system boot.[/green]"
                )
            else:
                console.print("[yellow]Warning: Could not install startup configuration.[/yellow]")

        config = config_utils.get_auto_inspect_config()
        console.print(
            f"[cyan]Projects will be inspected every {config['interval']} seconds.[/cyan]"
        )
        console.print(
            "[yellow]Run 'cwcli config auto-inspect start' to start the background process now.[/yellow]"
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@auto_inspect_app.command("disable")
def disable_auto_inspect():
    """
    Disable automatic project inspection.
    """
    try:
        # Stop the background process if running
        if auto_inspect.is_running():
            console.print("[yellow]Stopping auto-inspect background process...[/yellow]")
            auto_inspect.stop_daemon()

        config_utils.set_auto_inspect_enabled(False)
        console.print("[green]Auto-inspect disabled.[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@auto_inspect_app.command("start")
def start_auto_inspect(
    enable_startup: bool = typer.Option(
        False,
        "--startup",
        help="Also enable automatic startup on system boot/login",
    ),
):
    """
    Start the auto-inspect background process.

    This starts a daemon process that runs in the background and automatically
    inspects all running Frappe projects at the configured interval.
    """
    try:
        if auto_inspect.is_running():
            console.print("[yellow]Auto-inspect background process is already running.[/yellow]")
            pid = auto_inspect.get_pid()
            console.print(f"[cyan]Process ID: {pid}[/cyan]")
            return

        config = config_utils.get_auto_inspect_config()
        if not config.get("enabled"):
            console.print(
                "[red]Auto-inspect is not enabled. Run 'cwcli config auto-inspect enable' first.[/red]"
            )
            return

        auto_inspect.start_daemon()
        console.print("[green]Auto-inspect background process started.[/green]")
        console.print(
            f"[cyan]Running projects will be inspected every {config['interval']} seconds.[/cyan]"
        )
        console.print(f"[dim]Log file: {auto_inspect.LOG_FILE}[/dim]")

        if enable_startup:
            if startup.install_startup():
                config_utils.set_auto_inspect_startup(True)
                console.print(
                    "\n[green]Startup enabled. Auto-inspect will start automatically on system boot.[/green]"
                )
            else:
                console.print(
                    "\n[yellow]Warning: Could not install startup configuration.[/yellow]"
                )
    except Exception as e:
        console.print(f"[red]Error starting background process: {e}[/red]")


@auto_inspect_app.command("stop")
def stop_auto_inspect():
    """
    Stop the auto-inspect background process.
    """
    try:
        if not auto_inspect.is_running():
            console.print("[yellow]Auto-inspect background process is not running.[/yellow]")
            return

        auto_inspect.stop_daemon()
        console.print("[green]Auto-inspect background process stopped.[/green]")
    except Exception as e:
        console.print(f"[red]Error stopping background process: {e}[/red]")


@auto_inspect_app.command("status")
def status_auto_inspect():
    """
    Show the status of the auto-inspect background process.
    """
    config = config_utils.get_auto_inspect_config()

    table = Table(title="Auto-Inspect Status")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Enabled", "Yes" if config.get("enabled") else "No")
    table.add_row("Interval", f"{config.get('interval', 3600)} seconds")

    running = auto_inspect.is_running()
    table.add_row(
        "Background Process", "[green]Running[/green]" if running else "[red]Stopped[/red]"
    )

    if running:
        pid = auto_inspect.get_pid()
        table.add_row("Process ID (PID)", str(pid))

    # Show startup status
    startup_config = config.get("startup_enabled", False)
    startup_installed = startup.is_startup_installed()
    if startup_installed:
        startup_status = "[green]Enabled[/green]"
    elif startup_config:
        startup_status = "[yellow]Enabled (not installed)[/yellow]"
    else:
        startup_status = "[dim]Disabled[/dim]"
    table.add_row("Start on Boot", startup_status)

    console.print(table)

    if running:
        console.print(f"\n[dim]Log file: {auto_inspect.LOG_FILE}[/dim]")
        console.print("[dim]Use 'cwcli config auto-inspect logs' to view recent logs.[/dim]")


@auto_inspect_app.command("logs")
def show_auto_inspect_logs(
    lines: int = typer.Option(20, "--lines", "-n", help="Number of log lines to show")
):
    """
    Show recent auto-inspect background process logs.
    """
    try:
        log_content = auto_inspect.get_log_tail(lines)
        console.print(f"[bold]Last {lines} log lines:[/bold]\n")
        console.print(log_content)
    except Exception as e:
        console.print(f"[red]Error reading logs: {e}[/red]")


@auto_inspect_app.command("set-interval")
def set_interval(
    interval: int = typer.Argument(..., help="Inspection interval in seconds (minimum 60)")
):
    """
    Set the auto-inspect interval.
    """
    try:
        if interval < 60:
            console.print("[red]Error: Interval must be at least 60 seconds.[/red]")
            return

        config_utils.set_auto_inspect_interval(interval)
        console.print(f"[green]Inspection interval set to {interval} seconds.[/green]")

        if auto_inspect.is_running():
            console.print(
                "[yellow]Note: Restart the background process for the new interval to take effect.[/yellow]"
            )
            console.print("[dim]Run: cwcli config auto-inspect restart[/dim]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@auto_inspect_app.command("restart")
def restart_auto_inspect():
    """
    Restart the auto-inspect background process.
    """
    try:
        if auto_inspect.is_running():
            console.print("[yellow]Stopping auto-inspect background process...[/yellow]")
            auto_inspect.stop_daemon()
            console.print("[green]Background process stopped.[/green]")

        time.sleep(1)  # Give it a moment to fully stop

        config = config_utils.get_auto_inspect_config()
        if not config.get("enabled"):
            console.print(
                "[red]Auto-inspect is not enabled. Run 'cwcli config auto-inspect enable' first.[/red]"
            )
            return

        auto_inspect.start_daemon()
        console.print("[green]Auto-inspect background process restarted.[/green]")
        console.print(
            f"[cyan]Running projects will be inspected every {config['interval']} seconds.[/cyan]"
        )
    except Exception as e:
        console.print(f"[red]Error restarting background process: {e}[/red]")


@auto_inspect_app.command("install-startup")
def install_startup_cmd():
    """
    Install platform-specific startup configuration.

    Configures the system to automatically start the auto-inspect background
    process on boot/login. Uses LaunchAgent (macOS), systemd (Linux), or
    Task Scheduler (Windows).
    """
    try:
        # Check if auto-inspect is enabled
        config = config_utils.get_auto_inspect_config()
        if not config.get("enabled"):
            console.print("[yellow]Warning: Auto-inspect is not enabled.[/yellow]")
            console.print(
                "[dim]The startup will be installed, but auto-inspect won't run until you enable it.[/dim]"
            )
            console.print("[dim]Run 'cwcli config auto-inspect enable' first.[/dim]\n")

        # Check if already installed
        if startup.is_startup_installed():
            console.print("[yellow]Startup configuration is already installed.[/yellow]")
            return

        # Install startup
        console.print("[cyan]Installing startup configuration...[/cyan]")
        plat = startup.get_platform()

        if startup.install_startup():
            config_utils.set_auto_inspect_startup(True)
            console.print("[green]Startup configuration installed successfully![/green]")
            console.print(f"[cyan]Platform: {plat.title()}[/cyan]")
            console.print("[dim]Auto-inspect will start automatically on system boot/login.[/dim]")
        else:
            console.print("[red]Failed to install startup configuration.[/red]")

    except Exception as e:
        console.print(f"[red]Error installing startup: {e}[/red]")


@auto_inspect_app.command("uninstall-startup")
def uninstall_startup_cmd():
    """
    Remove platform-specific startup configuration.

    Removes the system configuration that automatically starts auto-inspect on boot.
    The auto-inspect feature itself remains enabled, you can still start it manually.
    """
    try:
        if not startup.is_startup_installed():
            console.print("[yellow]Startup configuration is not installed.[/yellow]")
            return

        console.print("[cyan]Removing startup configuration...[/cyan]")

        if startup.uninstall_startup():
            config_utils.set_auto_inspect_startup(False)
            console.print("[green]Startup configuration removed successfully![/green]")
            console.print("[dim]Auto-inspect will no longer start automatically on boot.[/dim]")
            console.print(
                "[dim]You can still start it manually with 'cwcli config auto-inspect start'.[/dim]"
            )
        else:
            console.print("[red]Failed to remove startup configuration.[/red]")

    except Exception as e:
        console.print(f"[red]Error removing startup: {e}[/red]")


@tips_app.command("enable")
def enable_tips():
    """
    Enable contextual tips during long-running operations.

    When enabled, cwcli will display rotating helpful tips alongside spinners
    during operations like inspect, update, and open. Tips help you discover
    features and best practices while waiting.
    """
    try:
        config_utils.set_show_tips(True)
        console.print("[green]Contextual tips enabled.[/green]")
        console.print(
            "[dim]Tips will be shown during long-running operations like inspect and update.[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Error enabling tips: {e}[/red]")


@tips_app.command("disable")
def disable_tips():
    """
    Disable contextual tips during long-running operations.

    When disabled, cwcli will show simpler status messages without tips.
    """
    try:
        config_utils.set_show_tips(False)
        console.print("[green]Contextual tips disabled.[/green]")
        console.print("[dim]Only basic status messages will be shown during operations.[/dim]")
    except Exception as e:
        console.print(f"[red]Error disabling tips: {e}[/red]")


@tips_app.command("status")
def tips_status():
    """
    Show the current tips display setting.
    """
    show_tips = config_utils.get_show_tips()
    status_text = "[green]Enabled[/green]" if show_tips else "[red]Disabled[/red]"

    table = Table(title="Tips Display Status")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Tips Display", status_text)

    console.print(table)

    if show_tips:
        console.print(
            "\n[dim]Tips are shown during long-running operations to help you discover features.[/dim]"
        )
        console.print("[dim]Use 'cwcli config tips disable' to turn them off.[/dim]")
    else:
        console.print("\n[dim]Tips are currently disabled.[/dim]")
        console.print("[dim]Use 'cwcli config tips enable' to turn them back on.[/dim]")
