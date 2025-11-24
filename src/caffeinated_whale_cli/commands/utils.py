"""
Shared utility functions for command implementations.

This module provides general utilities for ensuring containers are running
before executing commands.
"""

import questionary
import typer

from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_frappe_container


def ensure_containers_running(
    project_name: str,
    require_running: bool = False,
    verbose: bool = False,
    auto_start: bool = False,
) -> bool:
    """
    Check if containers for a project are running and optionally prompt to start them.

    Note: This function does NOT perform port conflict checks. Port conflicts are only
    checked by the `start` command. This is intentional - other commands (logs, run, etc.)
    need a quick way to ensure containers are running without the interactive port conflict
    resolution workflow.

    Args:
        project_name: The name of the docker-compose project.
        require_running: If True, containers must be running for the operation to proceed.
        verbose: Enable verbose output.
        auto_start: If True, automatically start containers without prompting.

    Returns:
        True if containers are running (or were started), False otherwise.

    Raises:
        typer.Exit: If containers are not running and user chooses not to start them,
                   or if starting containers fails.
    """
    if not require_running:
        return True

    # Get the frappe container
    frappe_container = get_frappe_container(project_name)

    # Reload container to get current status
    frappe_container.reload()

    if frappe_container.status == "running":
        if verbose:
            stderr_console.print("[dim]VERBOSE: Frappe container is running[/dim]")
        return True

    # Container is not running - decide whether to start
    user_wants_to_start = False

    if auto_start:
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Auto-starting containers for '{project_name}'[/dim]"
            )
        user_wants_to_start = True
    else:
        # Prompt user to start containers
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Frappe container for project '{project_name}' is not running."
        )

        try:
            answer = questionary.confirm(
                f"Would you like to start the containers for '{project_name}'?",
                default=True,
                auto_enter=False,
            ).ask()

            if answer:
                user_wants_to_start = True
            else:
                stderr_console.print("[yellow]Operation cancelled.[/yellow]")
                stderr_console.print(
                    f"[dim]Start containers with: cwcli start {project_name}[/dim]"
                )
                raise typer.Exit(code=0)
        except KeyboardInterrupt:
            stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
            raise typer.Exit(code=0) from None

    # Start the containers (skipping port checks)
    if user_wants_to_start:
        _start_containers_for_command(project_name, verbose)
        return True

    return False


def _start_containers_for_command(project_name: str, verbose: bool = False):
    """
    Start containers for a project. Used by ensure_containers_running.

    This function performs the same port conflict detection as the `start` command
    to prevent Docker errors when ports are already in use. It will:
    - Check if required ports are available
    - Identify and offer to stop conflicting Frappe projects
    - Report non-Frappe processes using the ports
    - Provide helpful error messages

    Args:
        project_name: The name of the docker-compose project.
        verbose: Enable verbose output.

    Raises:
        typer.Exit: If starting containers fails or port conflicts cannot be resolved.
    """
    from .start import _check_port_conflicts, _start_project

    # Check for port conflicts BEFORE attempting to start
    try:
        _check_port_conflicts(project_name, verbose)
    except typer.Exit:
        # Port conflict couldn't be resolved
        stderr_console.print(
            f"[yellow]Cannot start '{project_name}' due to port conflicts.[/yellow]"
        )
        stderr_console.print(
            f"[dim]Resolve conflicts manually or use 'cwcli start {project_name}' for more options.[/dim]"
        )
        raise

    # Port conflicts resolved or no conflicts - safe to start
    try:
        with stderr_console.status(
            f"[bold green]Starting containers for '{project_name}'...[/bold green]",
            spinner="dots",
        ) as status:
            log_file = _start_project(project_name, verbose=verbose, status=status)

        console.print(f"[bold green]✓[/bold green] Containers started for '{project_name}'")
        if log_file:
            console.print(f"[dim]View logs with: cwcli logs {project_name}[/dim]")
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to start containers: {e}")
        raise typer.Exit(code=1) from None
