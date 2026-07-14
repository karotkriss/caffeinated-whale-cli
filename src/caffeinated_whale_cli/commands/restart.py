import sys
from typing import NoReturn

import questionary
import typer

from ..core import restart as core_restart
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.restart import ProcessRestartOutcome
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from .start import _start_project
from .stop import _stop_project
from .utils import resolve_bench_path

app = typer.Typer(help="Restart a Frappe project's containers.")


@handle_docker_errors
def _restart_project(project_name: str, verbose: bool = False, status=None):
    """The core logic for restarting a single project's containers.

    Returns ``(log_file, stopped)``. ``stopped`` is ``None`` only when the project
    does not exist, so the caller can tell a genuine not-found failure apart from a
    legitimate zero count (found, but nothing was running) and exit non-zero.
    """
    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        return None, None

    # Check if any containers are running
    running_containers = [c for c in containers if c.status == "running"]

    if running_containers:
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Found {len(running_containers)} running container(s) for '{project_name}'[/dim]"
            )
    else:
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: No running containers found for '{project_name}'[/dim]"
            )

    # Stop then start
    stopped = _stop_project(project_name, verbose=verbose, status=status)
    log_file = _start_project(project_name, verbose=verbose, status=status)

    return log_file, stopped


@app.callback(invoke_without_command=True)
def restart(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    process: str = typer.Option(
        None,
        "--process",
        "-p",
        help="Restart ONE Procfile process (e.g. web, worker, socketio) via "
        "supervisord, leaving its siblings running. Omit to restart the whole stack.",
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench the --process belongs to: its numeric index or label "
        "(multi-bench projects).",
    ),
    project_name: list[str] = typer.Argument(
        None,
        help="The name(s) of the Frappe project(s) to restart. Can be piped from stdin.",
        autocompletion=complete_project_names,
    ),
):
    """
    Restart a project. With --process, restart just that one supervisord program
    (siblings keep running); without it, restart all containers and relaunch the
    bench under supervisord (the whole-stack restart).
    """
    project_names_to_process = []

    # A variadic Argument greedily eats options placed AFTER the project name, so
    # recover -v/--verbose, --process/-p <value>, and --bench <value> from the name
    # list (the same forgiveness start applies to its trailing flags).
    actual_verbose = verbose
    actual_process = process
    actual_bench = bench
    filtered_project_names = []

    if project_name:
        tokens = list(project_name)
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in ("-v", "--verbose"):
                actual_verbose = True
            elif token in ("--process", "-p"):
                if i + 1 < len(tokens):
                    actual_process = tokens[i + 1]
                    i += 1
            elif token.startswith("--process="):
                actual_process = token.split("=", 1)[1]
            elif token == "--bench":
                if i + 1 < len(tokens):
                    actual_bench = tokens[i + 1]
                    i += 1
            elif token.startswith("--bench="):
                actual_bench = token.split("=", 1)[1]
            else:
                filtered_project_names.append(token)
            i += 1
        project_names_to_process.extend(filtered_project_names)

    if not sys.stdin.isatty():
        piped_input = [line.strip() for line in sys.stdin]
        project_names_to_process.extend([name for name in piped_input if name])

    if not project_names_to_process:
        console.print(
            "[bold red]Error:[/bold red] Please provide at least one project name or pipe a list of names."
        )
        raise typer.Exit(code=1)

    if actual_process:
        _restart_processes(project_names_to_process, actual_process, actual_bench, actual_verbose)
        return

    console.print(
        f"Attempting to restart [bold cyan]{len(project_names_to_process)}[/bold cyan] project(s)..."
    )

    had_failure = False

    for name in project_names_to_process:
        with stderr_console.status(
            f"[bold cyan]Restarting '{name}'...[/bold cyan]", spinner="dots"
        ) as status:
            log_file, stopped = _restart_project(name, verbose=actual_verbose, status=status)

        # Print outside spinner context. ``stopped is None`` means the project did
        # not exist (error already printed): record the failure and skip the
        # "started" line rather than falsely reporting a restart.
        if stopped is None:
            had_failure = True
            continue

        if stopped > 0:
            console.print(f"Instance '{name}' stopped.")
        console.print(f"Instance '{name}' started.")
        if log_file:
            console.print(f"[bold green]✓ Started bench (logs: {log_file})[/bold green]")
            console.print(f"[dim]View logs with: cwcli logs {name}[/dim]")

    console.print("\n[bold green]Restart command finished.[/bold green]")

    if had_failure:
        raise typer.Exit(code=1)


def _restart_processes(names: list[str], process: str, bench: str | None, verbose: bool) -> None:
    """Single-process restart across the named projects; honest per-project exit code."""
    had_failure = False
    for name in names:
        try:
            outcome = _run_restart_process(name, process, bench, verbose)
        except typer.Exit as e:
            if e.exit_code == 0:
                raise
            had_failure = True
            continue
        _render_process_restart(name, outcome)

    if had_failure:
        raise typer.Exit(code=1)


def _run_restart_process(
    name: str, process: str, bench: str | None, verbose: bool
) -> ProcessRestartOutcome:
    """Call ``core.restart_process``, resolving a multi-bench / unknown-process choice."""
    override: str | None = None
    chosen_process = process
    while True:
        try:
            result = core_restart.restart_process(
                name, chosen_process, bench=bench, bench_path=override
            )
        except CwcliError as e:
            _handle_restart_error(e, name)

        if result.status is Status.NEEDS_CHOICE and result.choice is not None:
            if result.choice.kind == "select_bench":
                override = resolve_bench_path(
                    name, None, None, verbose=verbose, on_ambiguous="prompt"
                )
                bench = None  # the resolved path takes over
                continue
            if result.choice.kind == "select_process":
                chosen_process = _resolve_process_choice(name, result.choice)
                continue
        break

    if verbose:
        for warning in result.warnings:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
    assert result.data is not None
    return result.data


def _resolve_process_choice(name: str, choice) -> str:
    """Pick a process (TTY), or refuse listing the valid labels (non-TTY)."""
    options = [o["value"] for o in choice.options or []]
    if not sys.stdin.isatty():
        stderr_console.print(f"[bold red]Error:[/bold red] {choice.prompt}")
        if options:
            stderr_console.print(f"[dim]Valid processes: {', '.join(options)}[/dim]")
        raise typer.Exit(code=1)
    if not options:
        stderr_console.print(f"[bold red]Error:[/bold red] {choice.prompt}")
        raise typer.Exit(code=1)
    try:
        answer = questionary.select(choice.prompt, choices=options).unsafe_ask()
    except KeyboardInterrupt:
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1) from None
    if not answer:
        raise typer.Exit(code=1)
    return str(answer)


def _handle_restart_error(e: CwcliError, name: str) -> NoReturn:
    """Render a core.restart_process failure and Exit (nonzero -> loop skips project)."""
    if e.code == "project.not_found":
        console.print(f"[bold red]Error: Project '{name}' not found.[/bold red]")
    else:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        if e.hint:
            stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1)


def _render_process_restart(name: str, outcome: ProcessRestartOutcome) -> None:
    """Print the single-process restart result."""
    if outcome.old_pid is not None:
        transition = f"pid {outcome.old_pid} -> {outcome.new_pid or '?'}"
    else:
        transition = f"was down -> pid {outcome.new_pid or '?'}"
    console.print(
        f"Instance '{name}': restarted process "
        f"[bold cyan]{outcome.label}[/bold cyan] ({transition}, {outcome.supervisor_state})"
    )
