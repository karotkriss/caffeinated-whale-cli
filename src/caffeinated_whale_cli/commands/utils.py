"""
Shared utility functions for command implementations.

This module provides general utilities for ensuring containers are running
before executing commands, resolving a ``--bench`` selector to a bench path, and
handling ``--yes`` confirmations consistently.
"""

import sys

import questionary
import typer

from ..core import resolvers
from ..core.envelope import Status
from ..core.errors import CwcliError, ErrorKind
from ..utils import bench_labels
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
                   not to start them (decline or Ctrl-C), if the terminal is non-TTY
                   and ``auto_start`` is False (so no prompt is possible), or if
                   starting containers fails. All of these exit with code 1.
    """
    if not require_running:
        return True

    # Resolve the container (this CLI wrapper prints + Exit(1) on not-found) and
    # ask the pure core resolver for its run-state decision. All prompting,
    # printing, and the actual (UI-coupled) container start stay here in the CLI.
    frappe_container = get_frappe_container(project_name)
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )

    if state.status is Status.OK:
        assert state.data is not None  # OK always carries a ContainerState
        if state.data.running:
            if verbose:
                stderr_console.print("[dim]VERBOSE: Frappe container is running[/dim]")
            return True
        # start_requested: auto-start was asked for and the container was down.
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Auto-starting containers for '{project_name}'[/dim]"
            )
        _start_containers_for_command(project_name, verbose)
        return True

    # NEEDS_CHOICE confirm_start: the container is down and no auto-start was asked.
    if not prompt:
        # Non-interactive caller (e.g. running under a spinner). Do NOT prompt;
        # report that the containers are not running and let the caller degrade.
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Containers for '{project_name}' are not running "
                "(non-interactive mode; not prompting to start)[/dim]"
            )
        return False

    # Prompt the user to start. Mirrors confirm_or_exit's three-branch contract:
    # a non-TTY without auto_start refuses (Exit 1) rather than hanging on
    # questionary (open-but-idle stdin) or crashing with an uncaught EOFError
    # (closed stdin - .ask() only catches KeyboardInterrupt); an interactive
    # decline or Ctrl-C is a refusal, so it exits non-zero too.
    if not sys.stdin.isatty():
        stderr_console.print(
            f"[bold red]Error:[/bold red] Frappe container for project "
            f"'{project_name}' is not running. Pass --yes to auto-start it, or "
            f"start it first with 'cwcli start {project_name}'."
        )
        raise typer.Exit(code=1)

    stderr_console.print(
        f"[yellow]Warning:[/yellow] Frappe container for project '{project_name}' is not running."
    )

    try:
        answer = questionary.confirm(
            f"Would you like to start the containers for '{project_name}'?",
            default=True,
            auto_enter=False,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1) from None

    if answer:
        _start_containers_for_command(project_name, verbose)
        return True

    stderr_console.print("[yellow]Operation cancelled.[/yellow]")
    stderr_console.print(f"[dim]Start containers with: cwcli start {project_name}[/dim]")
    raise typer.Exit(code=1)


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
           with ``on_ambiguous="first"`` (used by the internal ``_start_project``
           wrapper - ``restart``/auto-start/post-restore callers) it returns the
           first bench and prints a note; with ``on_ambiguous="prompt"`` (used by
           the interactive ``cwcli start``/``status`` commands) it prompts on a
           TTY and refuses naming ``--bench`` on a non-TTY,
         - no cached benches at all: returns ``None`` so the caller can fall back to
           its own behavior (run inspect / use a hardcoded default).

    Returns the resolved bench path, or ``None`` only in the no-cache case. Raises
    ``typer.Exit(1)`` on a conflict, an unresolved selector, or an ambiguous
    multi-bench default under ``on_ambiguous="error"``.

    This is the CLI wrapper: the pure resolution (precedence, cache read,
    selector match, ambiguity) lives in ``core.resolvers.resolve_bench``; this
    wrapper only renders the bench list and maps the typed outcome to today's
    prints and exit codes.
    """
    try:
        result = resolvers.resolve_bench(project_name, bench_selector, path_override)
    except CwcliError as e:
        if e.kind is ErrorKind.USAGE:
            stderr_console.print(
                "[bold red]Error:[/bold red] Use either --bench or --path, not both."
            )
            raise typer.Exit(code=1) from None
        # NOT_FOUND: an explicit --bench selector matched no cached bench.
        benches = resolvers.cached_benches(project_name)
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
        raise typer.Exit(code=1) from None

    if result is None:
        # No cache to resolve against; let the caller fall back (inspect/default).
        return None

    if result.status is Status.OK:
        assert result.data is not None  # OK always carries the resolved path
        path: str = result.data
        if verbose:
            for warning in result.warnings:
                if warning.code == "bench.sole":
                    stderr_console.print(f"[dim]{warning.text}[/dim]")
        return path

    # NEEDS_CHOICE select_bench: multiple benches, no selector.
    benches = resolvers.cached_benches(project_name)
    if on_ambiguous == "first":
        first_path: str = benches[0]["path"]
        stderr_console.print(
            f"[yellow]Note:[/yellow] project '{project_name}' has multiple benches; "
            f"using [green]{first_path}[/green]. Select another with --bench <index|label>:"
        )
        stderr_console.print(bench_labels.format_bench_list(benches))
        return first_path

    if on_ambiguous == "prompt":
        # start/status: prompt which bench on a TTY, refuse on a non-TTY. Mirrors
        # the "support BOTH interactive and non-interactive" contract - a non-TTY
        # without --bench must refuse (Exit 1), never silently pick one.
        return _prompt_select_bench(project_name, benches)

    stderr_console.print(
        f"[bold red]Error:[/bold red] project '{project_name}' has multiple benches; "
        "specify one with --bench <index|label>:"
    )
    stderr_console.print(bench_labels.format_bench_list(benches))
    raise typer.Exit(code=1)


def _prompt_select_bench(project_name: str, benches: list[dict]) -> str:
    """Interactively pick a bench (TTY), or refuse naming ``--bench`` (non-TTY)."""
    if not sys.stdin.isatty():
        stderr_console.print(
            f"[bold red]Error:[/bold red] project '{project_name}' has multiple benches; "
            "specify one with --bench <index|label>:"
        )
        stderr_console.print(bench_labels.format_bench_list(benches))
        raise typer.Exit(code=1)

    choice_map: dict[str, str] = {}
    choices: list[str] = []
    for i, b in enumerate(benches):
        label = b.get("label")
        prefix = f"'{label}' " if label else ""
        text = f"[{i}] {prefix}{b.get('path', '?')}"
        choices.append(text)
        choice_map[text] = str(b["path"])

    try:
        answer = questionary.select(
            f"Project '{project_name}' has multiple benches; select one:",
            choices=choices,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1) from None

    if answer is None:
        stderr_console.print("[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1)
    return choice_map[answer]


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
