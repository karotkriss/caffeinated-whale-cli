"""
Shared utility functions for command implementations.

This module provides general utilities for ensuring containers are running
before executing commands, resolving a ``--bench`` selector to a bench path, and
handling ``--yes`` confirmations consistently.
"""

import sys

import questionary
import typer

from ..utils import bench_labels, db_utils
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_frappe_container


def ensure_containers_running(
    project_name: str,
    require_running: bool = False,
    verbose: bool = False,
    auto_start: bool = False,
    prompt: bool = True,
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
        prompt: If True (default), interactively ask the user to start stopped
            containers. If False, never prompt: when the containers are not running
            (and ``auto_start`` is False) the function returns False instead of
            asking a question. This non-interactive mode is required for callers that
            run inside a Rich ``console.status`` spinner (e.g. the ``rm`` recache
            path), where an interactive prompt would be painted over and could never
            receive input, deadlocking the command.

    Returns:
        True if containers are running (or were started), False otherwise.

    Raises:
        typer.Exit: If containers are not running and the user is prompted but chooses
                   not to start them, or if starting containers fails.
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
    elif not prompt:
        # Non-interactive caller (e.g. running under a spinner). Do NOT prompt;
        # report that the containers are not running and let the caller decide
        # how to degrade gracefully.
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Containers for '{project_name}' are not running "
                "(non-interactive mode; not prompting to start)[/dim]"
            )
        return False
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


def resolve_bench_path(
    project_name: str,
    bench_selector: str | None,
    path_override: str | None,
    *,
    verbose: bool = False,
    on_ambiguous: str = "error",
) -> str | None:
    """Resolve which bench a command should operate on, honoring ``--bench``/``--path``.

    This is the single, shared replacement for the ad-hoc "use ``--path`` if given,
    else cached ``bench_instances[0]``, else a hardcoded default" logic that used to
    be copy-pasted across the bench-operating commands (and which silently picked
    an arbitrary bench in a multi-bench project).

    Precedence:
      1. ``path_override`` (``--path``) - the explicit low-level escape hatch. Using
         it together with ``--bench`` is an error (they specify the same thing two
         ways).
      2. ``bench_selector`` (``--bench <number|label>``) - resolved against the
         cached benches via :func:`bench_labels.resolve_bench`. No match -> a clear
         error listing every bench.
      3. Neither given -> the *default* bench:
         - single-bench project: that bench's path,
         - multi-bench project: with ``on_ambiguous="error"`` (the data-op default)
           this errors and lists the benches so the user picks one with ``--bench``;
           with ``on_ambiguous="first"`` (used by ``start``) it returns the first
           bench and prints a note, so ``cwcli start`` keeps working,
         - no cached benches at all: returns ``None`` so the caller can fall back to
           its own behavior (run inspect / use a hardcoded default).

    Returns the resolved bench path, or ``None`` only in the no-cache case. Raises
    ``typer.Exit(1)`` on a conflict, an unresolved selector, or an ambiguous
    multi-bench default under ``on_ambiguous="error"``.
    """
    if path_override and bench_selector:
        stderr_console.print("[bold red]Error:[/bold red] Use either --bench or --path, not both.")
        raise typer.Exit(code=1)

    if path_override:
        return path_override

    cached_data = db_utils.get_cached_project_data(project_name)
    benches = (cached_data or {}).get("bench_instances") or []

    if bench_selector is not None:
        chosen = bench_labels.resolve_bench(benches, bench_selector)
        if chosen is None:
            stderr_console.print(
                f"[bold red]Error:[/bold red] No bench '{bench_selector}' in project "
                f"'{project_name}'."
            )
            if benches:
                stderr_console.print("Available benches (address with --bench <index|label>):")
                stderr_console.print(bench_labels.format_bench_list(benches))
            else:
                stderr_console.print(
                    f"[dim]No benches are cached. Run 'cwcli inspect {project_name}' first.[/dim]"
                )
            raise typer.Exit(code=1)
        chosen_path: str = chosen["path"]
        return chosen_path

    # No explicit selector: derive the default bench.
    if len(benches) == 1:
        path: str = benches[0]["path"]
        if verbose:
            stderr_console.print(f"[dim]Using the only bench: {path}[/dim]")
        return path

    if len(benches) == 0:
        # No cache to resolve against; let the caller fall back (inspect/default).
        return None

    # Multiple benches, no selector.
    if on_ambiguous == "first":
        first_path: str = benches[0]["path"]
        stderr_console.print(
            f"[yellow]Note:[/yellow] project '{project_name}' has multiple benches; "
            f"using [green]{first_path}[/green]. Select another with --bench <index|label>:"
        )
        stderr_console.print(bench_labels.format_bench_list(benches))
        return first_path

    stderr_console.print(
        f"[bold red]Error:[/bold red] project '{project_name}' has multiple benches; "
        "specify one with --bench <index|label>:"
    )
    stderr_console.print(bench_labels.format_bench_list(benches))
    raise typer.Exit(code=1)


def confirm_or_exit(
    prompt: str,
    *,
    assume_yes: bool,
    refuse_message: str,
    default: bool = False,
    cancel_message: str = "Operation cancelled.",
) -> None:
    """Gate a destructive action behind a confirmation, honoring ``--yes``.

    Mirrors the established ``restore``/``rm`` contract so every ``--yes`` behaves
    the same way:
      - ``assume_yes`` -> proceed without prompting;
      - no TTY and not ``assume_yes`` -> refuse and ``exit(1)`` (never silently
        proceed on a destructive op driven non-interactively);
      - interactive TTY -> ask; a declined confirm or Ctrl-C/EOF exits non-zero.

    Returns normally only when the action is approved.
    """
    if assume_yes:
        console.print("[dim]Proceeding without confirmation (--yes).[/dim]")
        return

    if not sys.stdin.isatty():
        stderr_console.print(f"[bold red]Error:[/bold red] {refuse_message}")
        raise typer.Exit(code=1)

    try:
        answer = questionary.confirm(prompt, default=default).ask()
    except (KeyboardInterrupt, EOFError):
        stderr_console.print(f"\n[yellow]{cancel_message}[/yellow]")
        raise typer.Exit(code=1) from None

    if not answer:
        stderr_console.print(f"[yellow]{cancel_message}[/yellow]")
        raise typer.Exit(code=1)


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
