"""``cwcli config`` - the config surface, reworked (openspec ``rework-config-dx``).

A thin renderer over ``core.config`` / ``core.auto_inspect``: this module owns
only the typer signatures, the ``rich`` rendering, the ``--json`` spellings, and
the exit codes. The decisions (path validation/normalization, the fused
enable/disable desired-state orchestration, cache-clear consent) live in the
UI-pure core.

The frozen aliases at the bottom (``add-path``/``remove-path``,
``auto-inspect start/restart/set-interval/install-startup/uninstall-startup``,
``tips status``) are registered ``hidden=True`` with a one-line stderr
deprecation warning and byte-identical behavior. They deliberately BYPASS the
core and keep calling the utils the old monolith called (Decision 4: building
core API for verbs scheduled for deletion is waste) - except
``add-path``/``remove-path``, which share the core's input validation with
their new home (the one disclosed exception: storing garbage was never a
behavior worth preserving). ``start``'s argv AND its refuse-when-disabled guard
are load-bearing: already-installed boot units exec
``cwcli config auto-inspect start`` verbatim, and the guard is what keeps a
stale hook inert after a ``disable``.
"""

import json
import time
from dataclasses import asdict

import click
import typer
from rich.table import Table

from ..core import auto_inspect as core_ai
from ..core import config as core_config
from ..core.auto_inspect import AutoInspectState
from ..core.envelope import Status
from ..core.errors import CwcliError, ErrorKind
from ..utils import auto_inspect, config_utils, db_utils, startup
from ..utils.console import console, stderr_console
from .utils import confirm_or_exit

app = typer.Typer(help="Manage CLI configuration and cache.")
paths_app = typer.Typer(help="Manage the bench search paths the inspect command scans.")
cache_app = typer.Typer(help="Manage the cache.")
auto_inspect_app = typer.Typer(help="Manage automatic project inspection.")
tips_app = typer.Typer(help="Manage contextual tips display.")
app.add_typer(paths_app, name="paths")
app.add_typer(cache_app, name="cache")
app.add_typer(auto_inspect_app, name="auto-inspect")
app.add_typer(tips_app, name="tips")


def _warn_deprecated(old: str, new: str) -> None:
    """The one-line stderr deprecation warning every frozen alias emits.

    stderr ONLY, so no parsed stdout changes shape (Decision 4). Wording mirrors
    the ``cwcli update`` deprecation warning.
    """
    stderr_console.print(
        f"[yellow]Warning:[/yellow] '{old}' is deprecated; use [green]{new}[/green] instead."
    )


def _exit_for(error: CwcliError) -> "typer.Exit":
    """Map a core error to the human exit code: USAGE -> 2, everything else -> 1."""
    stderr_console.print(f"[bold red]Error:[/bold red] {error.message}")
    if error.hint:
        stderr_console.print(f"[dim]{error.hint}[/dim]")
    return typer.Exit(code=2 if error.kind is ErrorKind.USAGE else 1)


def _boot_status_words(state: AutoInspectState) -> str:
    if state.boot_installed:
        return "Enabled"
    if state.startup_enabled:
        return "Enabled (not installed)"
    return "Disabled"


# ------------------------------------------------------------------------- show


@app.command("show")
def show(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """
    Show the effective configuration in one shot: search paths, auto-inspect
    settings with live daemon and boot-hook state, tips, and file locations.
    """
    report = core_config.show_config().data
    assert report is not None

    if json_output:
        # Plain print, never a width-wrapping console (the ls --json precedent).
        print(json.dumps(asdict(report), indent=2))
        return

    state = report.auto_inspect
    console.print("[bold]Locations[/bold]")
    console.print(f"  Config file: [green]{report.config_file}[/green]")
    console.print(f"  Cache DB:    [green]{report.cache_db}[/green]")
    console.print("\n[bold]Search paths[/bold]")
    if report.search_paths:
        for path in report.search_paths:
            console.print(f"  - {path}")
    else:
        console.print("  [dim](none configured)[/dim]")
    console.print("\n[bold]Auto-inspect[/bold]")
    console.print(f"  Enabled: {'Yes' if state.enabled else 'No'}")
    console.print(f"  Interval: {state.interval} seconds")
    daemon_words = (
        f"[green]Running[/green] (PID {state.daemon_pid})"
        if state.daemon_running
        else "[red]Stopped[/red]"
    )
    console.print(f"  Daemon: {daemon_words}")
    console.print(f"  Start on boot: {_boot_status_words(state)}")
    console.print("\n[bold]UI[/bold]")
    console.print(f"  Tips: {'Enabled' if report.show_tips else 'Disabled'}")


# ------------------------------------------------------------------- path / edit


@app.command("path")
def config_path():
    """
    Print the configuration file's path, bare and substitution-safe.
    """
    # typer.echo, not rich: a console wraps long paths at narrow widths, which
    # broke `$(cwcli config path)` (F8).
    typer.echo(str(config_utils.CONFIG_FILE))


@app.command("edit")
def config_edit():
    """
    Open the configuration file in $EDITOR.
    """
    config_utils.load_config()  # ensures the file exists before the editor opens
    click.edit(filename=str(config_utils.CONFIG_FILE))


# ------------------------------------------------------------------ search paths


def _render_path_change(result, *, added: bool) -> None:
    data = result.data
    assert data is not None
    if added:
        if data.changed:
            console.print(f"[green]Added '{data.path}' to custom search paths.[/green]")
        else:
            console.print(f"[yellow]'{data.path}' already exists in custom search paths.[/yellow]")
    else:
        if data.changed:
            console.print(f"[green]Removed '{data.path}' from custom search paths.[/green]")
        else:
            console.print(f"[yellow]'{data.path}' not found in custom search paths.[/yellow]")


@paths_app.callback(invoke_without_command=True)
def paths_home(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """
    List the configured bench search paths.
    """
    if ctx.invoked_subcommand is not None:
        return
    report = core_config.show_config().data
    assert report is not None
    search_paths = report.search_paths
    if json_output:
        print(json.dumps(search_paths, indent=2))
        return
    if not search_paths:
        console.print("[yellow]No custom search paths configured.[/yellow]")
        return
    for path in search_paths:
        typer.echo(path)


@paths_app.command("add")
def paths_add(
    path: str = typer.Argument(..., help="The absolute path to add to the custom search paths.")
):
    """
    Add a custom bench search path (absolute, normalized before duplicate check).
    """
    try:
        result = core_config.add_search_path(path)
    except CwcliError as e:
        raise _exit_for(e) from None
    _render_path_change(result, added=True)


@paths_app.command("remove")
def paths_remove(
    path: str = typer.Argument(..., help="The path to remove from the custom search paths.")
):
    """
    Remove a custom bench search path (matched on its normalized form).
    """
    try:
        result = core_config.remove_search_path(path)
    except CwcliError as e:
        raise _exit_for(e) from None
    _render_path_change(result, added=False)


# ------------------------------------------------------------------------ cache


@cache_app.command("clear")
def clear_cache(
    project_name: str = typer.Argument(
        None, help="The name of the project to clear from the cache."
    ),
    all: bool = typer.Option(False, "--all", "-a", help="Clear the entire cache."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt (required to clear --all non-interactively).",
    ),
):
    """
    Clear the cache for a specific project or the entire cache.
    """
    try:
        result = core_config.clear_cache(project_name, all_projects=all, consent=yes)
    except CwcliError as e:
        # Contradictory or missing targets are usage errors: exit 2, nothing
        # cleared (F4/F10) - never resolved toward the more destructive reading.
        raise _exit_for(e) from None

    if result.status is Status.NEEDS_CHOICE:
        assert result.choice is not None
        # Destructive: prompt on a TTY; a non-TTY without --yes refuses (exit 1)
        # rather than silently wiping the whole cache.
        confirm_or_exit(
            result.choice.prompt,
            assume_yes=False,
            refuse_message=(
                "Refusing to clear the entire cache without confirmation. "
                "Re-run with --yes to clear it non-interactively."
            ),
        )
        result = core_config.clear_cache(project_name, all_projects=all, consent=True)

    data = result.data
    assert data is not None
    if data.scope == "all":
        console.print("[green]Entire cache has been cleared.[/green]")
    elif data.found:
        console.print(f"Cache for project '[bold cyan]{data.project}[/bold cyan]' cleared.")
    else:
        console.print(
            f"[yellow]No cache found for project '[bold cyan]{data.project}[/bold cyan]'.[/yellow]"
        )


@cache_app.command("path")
def cache_path():
    """
    Print the cache file's path, bare and substitution-safe.
    """
    typer.echo(str(db_utils.DB_PATH))


@cache_app.command("list")
def list_cached_projects(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """
    List all projects currently in the cache.
    """
    projects = core_config.cached_projects().data
    assert projects is not None

    if json_output:
        # JSON before the empty-state message, so an empty cache emits [].
        print(json.dumps([asdict(p) for p in projects], indent=2))
        return

    if not projects:
        console.print("[yellow]No projects found in the cache.[/yellow]")
        return

    table = Table(title="Cached Projects")
    table.add_column("Project Name", style="cyan")
    table.add_column("Last Updated", style="magenta")
    for project in projects:
        table.add_row(project.name, project.last_updated)
    console.print(table)


# ----------------------------------------------------------------- auto-inspect

_ACTION_LINES = {
    "config.enabled": "[green]Auto-inspect enabled.[/green]",
    "interval.set": None,  # rendered with the interval value below
    "daemon.started": "[green]Auto-inspect background process started.[/green]",
    "daemon.restarted": (
        "[green]Auto-inspect background process restarted with the new interval.[/green]"
    ),
    "daemon.already_running": (
        "[yellow]Auto-inspect background process is already running.[/yellow]"
    ),
    "daemon.stopped": "[green]Auto-inspect background process stopped.[/green]",
    "daemon.not_running": "[yellow]Auto-inspect background process is not running.[/yellow]",
    "config.disabled": "[green]Auto-inspect disabled.[/green]",
    "hook.installed": (
        "[green]Startup enabled. Auto-inspect will start automatically on system boot.[/green]"
    ),
    "hook.removed": "[green]Startup configuration removed.[/green]",
}


def _render_outcome(result) -> None:
    data = result.data
    assert data is not None
    for action in data.actions:
        if action == "interval.set":
            console.print(
                f"[green]Inspection interval set to {data.state.interval} seconds.[/green]"
            )
            continue
        line = _ACTION_LINES.get(action)
        if line:
            console.print(line)
    for warning in result.warnings:
        console.print(f"[yellow]Warning: {warning.text}[/yellow]")


@auto_inspect_app.command("enable")
def enable_auto_inspect(
    interval: int = typer.Option(
        None,
        "--interval",
        "-i",
        help="Inspection interval in seconds (minimum 60, default 3600)",
    ),
    startup: bool | None = typer.Option(
        None,
        "--startup/--no-startup",
        help="Also install (or remove) the automatic start on system boot/login. "
        "Omit both to leave the boot hook untouched.",
        show_default=False,
    ),
):
    """
    Enable automatic project inspection and start the background process.

    Idempotent: re-running applies changed settings (restarting the daemon when
    the interval changed) and reports already-satisfied state as a no-op.
    """
    try:
        result = core_ai.enable(interval=interval, at_boot=startup)
    except CwcliError as e:
        # Validation happens before anything persists (the F3 fix); the interval
        # refusal keeps its historical exit 1.
        console.print(f"[red]Error: {e.message}[/red]")
        raise typer.Exit(code=1) from None

    _render_outcome(result)
    assert result.data is not None
    console.print(
        f"[cyan]Projects will be inspected every {result.data.state.interval} seconds.[/cyan]"
    )
    console.print(f"[dim]Log file: {result.data.state.log_file}[/dim]")


@auto_inspect_app.command("disable")
def disable_auto_inspect():
    """
    Disable automatic project inspection: stop the background process, set
    enabled = false, and remove the boot hook.
    """
    try:
        result = core_ai.disable()
    except CwcliError as e:
        console.print(f"[red]Error: {e.message}[/red]")
        raise typer.Exit(code=1) from None
    _render_outcome(result)


@auto_inspect_app.command("stop")
def stop_auto_inspect():
    """
    Stop the auto-inspect background process.

    Leaves the enabled flag and the boot hook untouched, so a boot-hooked
    daemon returns at the next boot. Use 'disable' to tear everything down.
    """
    try:
        result = core_ai.stop()
    except CwcliError as e:
        console.print(f"[red]Error stopping background process: {e.message}[/red]")
        raise typer.Exit(code=1) from None
    _render_outcome(result)


@auto_inspect_app.command("status")
def status_auto_inspect(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """
    Show the status of the auto-inspect background process.
    """
    state = core_ai.status().data
    assert state is not None

    if json_output:
        print(json.dumps(asdict(state), indent=2))
        return

    table = Table(title="Auto-Inspect Status")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Enabled", "Yes" if state.enabled else "No")
    table.add_row("Interval", f"{state.interval} seconds")
    table.add_row(
        "Background Process",
        "[green]Running[/green]" if state.daemon_running else "[red]Stopped[/red]",
    )
    if state.daemon_running:
        table.add_row("Process ID (PID)", str(state.daemon_pid))

    boot = _boot_status_words(state)
    styled_boot = {
        "Enabled": "[green]Enabled[/green]",
        "Enabled (not installed)": "[yellow]Enabled (not installed)[/yellow]",
        "Disabled": "[dim]Disabled[/dim]",
    }[boot]
    table.add_row("Start on Boot", styled_boot)

    console.print(table)

    if state.daemon_running:
        console.print(f"\n[dim]Log file: {state.log_file}[/dim]")
        console.print("[dim]Use 'cwcli config auto-inspect logs' to view recent logs.[/dim]")


@auto_inspect_app.command("logs")
def show_auto_inspect_logs(
    lines: int = typer.Option(20, "--lines", "-n", help="Number of log lines to show")
):
    """
    Show recent auto-inspect background process logs.
    """
    try:
        result = core_ai.log_tail(lines)
    except CwcliError as e:
        # A failed read is an error (F10): exit 1, not a red message with exit 0.
        stderr_console.print(f"[red]{e.message}[/red]")
        raise typer.Exit(code=1) from None
    assert result.data is not None
    console.print(f"[bold]Last {lines} log lines:[/bold]\n")
    console.print(result.data.content)


# ------------------------------------------------------------------------- tips


@tips_app.command("enable")
def enable_tips():
    """
    Enable contextual tips during long-running operations.

    When enabled, cwcli will display rotating helpful tips alongside spinners
    during operations like inspect, update, and open. Tips help you discover
    features and best practices while waiting.
    """
    try:
        core_config.set_tips(True)
        console.print("[green]Contextual tips enabled.[/green]")
        console.print(
            "[dim]Tips will be shown during long-running operations like inspect and update.[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Error enabling tips: {e}[/red]")
        raise typer.Exit(code=1) from e


@tips_app.command("disable")
def disable_tips():
    """
    Disable contextual tips during long-running operations.

    When disabled, cwcli will show simpler status messages without tips.
    """
    try:
        core_config.set_tips(False)
        console.print("[green]Contextual tips disabled.[/green]")
        console.print("[dim]Only basic status messages will be shown during operations.[/dim]")
    except Exception as e:
        console.print(f"[red]Error disabling tips: {e}[/red]")
        raise typer.Exit(code=1) from e


# ==================================================================== aliases
# Frozen deprecated aliases (Decision 4): hidden from --help, one stderr
# warning line, byte-identical stdout and exit codes via the utils the old
# monolith called. New semantics live ONLY under the surviving names above, so
# no script silently changes behavior. Removal horizon: not before 1.0 and no
# earlier than two minors after rework-config-dx ships.


@app.command("add-path", hidden=True)
def add_path(
    path: str = typer.Argument(..., help="The absolute path to add to the custom search paths.")
):
    """
    [Deprecated] Add a custom bench search path. Use 'cwcli config paths add'.
    """
    _warn_deprecated("cwcli config add-path", "cwcli config paths add")
    # The one disclosed alias exception: the same validation as the new home
    # (storing a non-absolute path was never behavior worth preserving - F9).
    try:
        result = core_config.add_search_path(path)
    except CwcliError as e:
        raise _exit_for(e) from None
    _render_path_change(result, added=True)


@app.command("remove-path", hidden=True)
def remove_path(
    path: str = typer.Argument(..., help="The path to remove from the custom search paths.")
):
    """
    [Deprecated] Remove a custom bench search path. Use 'cwcli config paths remove'.
    """
    _warn_deprecated("cwcli config remove-path", "cwcli config paths remove")
    try:
        result = core_config.remove_search_path(path)
    except CwcliError as e:
        raise _exit_for(e) from None
    _render_path_change(result, added=False)


@auto_inspect_app.command("start", hidden=True)
def start_auto_inspect(
    enable_startup: bool = typer.Option(
        False,
        "--startup",
        help="Also enable automatic startup on system boot/login",
    ),
):
    """
    [Deprecated] Start the auto-inspect background process. Use 'enable'.

    The argv AND the refuse-when-disabled guard are load-bearing: installed
    boot units exec this verbatim, and the guard keeps a stale hook inert.
    """
    _warn_deprecated("cwcli config auto-inspect start", "cwcli config auto-inspect enable")
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
            raise typer.Exit(code=1)

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
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error starting background process: {e}[/red]")
        raise typer.Exit(code=1) from e


@auto_inspect_app.command("restart", hidden=True)
def restart_auto_inspect():
    """
    [Deprecated] Restart the auto-inspect background process. Use 'enable'.
    """
    _warn_deprecated("cwcli config auto-inspect restart", "cwcli config auto-inspect enable")
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
            raise typer.Exit(code=1)

        auto_inspect.start_daemon()
        console.print("[green]Auto-inspect background process restarted.[/green]")
        console.print(
            f"[cyan]Running projects will be inspected every {config['interval']} seconds.[/cyan]"
        )
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error restarting background process: {e}[/red]")
        raise typer.Exit(code=1) from e


@auto_inspect_app.command("set-interval", hidden=True)
def set_interval(
    interval: int = typer.Argument(..., help="Inspection interval in seconds (minimum 60)")
):
    """
    [Deprecated] Set the auto-inspect interval. Use 'enable --interval N'.
    """
    _warn_deprecated(
        "cwcli config auto-inspect set-interval", "cwcli config auto-inspect enable --interval"
    )
    try:
        if interval < 60:
            console.print("[red]Error: Interval must be at least 60 seconds.[/red]")
            raise typer.Exit(code=1)

        config_utils.set_auto_inspect_interval(interval)
        console.print(f"[green]Inspection interval set to {interval} seconds.[/green]")

        if auto_inspect.is_running():
            console.print(
                "[yellow]Note: Restart the background process for the new interval to take effect.[/yellow]"
            )
            console.print("[dim]Run: cwcli config auto-inspect restart[/dim]")
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1) from e


@auto_inspect_app.command("install-startup", hidden=True)
def install_startup_cmd():
    """
    [Deprecated] Install the boot startup configuration. Use 'enable --startup'.
    """
    _warn_deprecated(
        "cwcli config auto-inspect install-startup", "cwcli config auto-inspect enable --startup"
    )
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
            raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error installing startup: {e}[/red]")
        raise typer.Exit(code=1) from e


@auto_inspect_app.command("uninstall-startup", hidden=True)
def uninstall_startup_cmd():
    """
    [Deprecated] Remove the boot startup configuration. Use 'enable --no-startup'
    (keep running, drop the hook) or 'disable'.
    """
    _warn_deprecated(
        "cwcli config auto-inspect uninstall-startup",
        "cwcli config auto-inspect enable --no-startup",
    )
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
            raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error removing startup: {e}[/red]")
        raise typer.Exit(code=1) from e


@tips_app.command("status", hidden=True)
def tips_status():
    """
    [Deprecated] Show the current tips display setting. Use 'cwcli config show'.
    """
    _warn_deprecated("cwcli config tips status", "cwcli config show")
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
