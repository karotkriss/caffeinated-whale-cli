import sys
from typing import NoReturn

import questionary
import typer

from ..core import start as core_start
from ..core.docker import get_project_containers
from ..core.envelope import Status
from ..core.errors import CwcliError, ErrorKind
from ..core.start import StartOutcome
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from ..utils.port_utils import (
    check_ports_in_use,
    find_project_using_ports,
    format_port_list,
    get_ports_in_use_with_processes,
    get_project_ports,
)
from .utils import resolve_bench_path, split_trailing_options

app = typer.Typer(help="Start a Frappe project's containers.")


def _check_port_conflicts(
    project_name: str, verbose: bool = False, assume_yes: bool = False
) -> bool:
    """
    Check for port conflicts before starting a project.

    Handles mixed scenarios where ports may be held by both Frappe projects
    and external processes. After stopping conflicting Frappe projects,
    re-checks all ports to ensure no external process conflicts remain.

    Args:
        project_name: The name of the docker-compose project.
        verbose: Enable verbose output.

    Returns:
        True if no conflicts or conflicts were resolved, False otherwise.

    Raises:
        typer.Exit: If port conflicts cannot be resolved.
    """
    project_ports = get_project_ports(project_name)

    if not project_ports:
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: No ports configured for project '{project_name}'[/dim]"
            )
        return True

    if verbose:
        stderr_console.print(
            f"[dim]VERBOSE: Project '{project_name}' uses ports: {project_ports}[/dim]"
        )

    # Check which ports are in use
    ports_status = check_ports_in_use(project_ports, verbose=verbose)
    ports_in_use = [port for port, in_use in ports_status.items() if in_use]

    if not ports_in_use:
        if verbose:
            stderr_console.print("[dim]VERBOSE: All required ports are available[/dim]")
        return True

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Ports in use: {ports_in_use}[/dim]")

    # Find which Frappe projects are using these ports
    frappe_projects_on_ports = find_project_using_ports(ports_in_use, exclude_project=project_name)

    # Split ports into Frappe-owned vs non-Frappe-owned
    frappe_ports = set(frappe_projects_on_ports.keys())
    non_frappe_ports = set(ports_in_use) - frappe_ports

    if verbose:
        if frappe_ports:
            stderr_console.print(
                f"[dim]VERBOSE: Ports owned by Frappe projects: {sorted(frappe_ports)}[/dim]"
            )
        if non_frappe_ports:
            stderr_console.print(
                f"[dim]VERBOSE: Ports owned by non-Frappe processes: {sorted(non_frappe_ports)}[/dim]"
            )

    # Handle Frappe project conflicts first
    if frappe_projects_on_ports:
        conflicting_projects = set(frappe_projects_on_ports.values())

        # Group ports by project for better display
        project_to_ports: dict[str, list] = {}
        for port, proj in frappe_projects_on_ports.items():
            if proj not in project_to_ports:
                project_to_ports[proj] = []
            project_to_ports[proj].append(port)

        stderr_console.print(
            f"\n[yellow]Warning:[/yellow] Some ports needed by '{project_name}' are in use by other Frappe projects:"
        )

        for proj, ports in project_to_ports.items():
            formatted_ports = format_port_list(ports)
            stderr_console.print(f"  • Project '{proj}': {formatted_ports}")

        # A non-TTY without --yes cannot answer the prompt: refuse (Exit 1) rather
        # than hang on questionary or crash on EOF, mirroring confirm_or_exit.
        if not assume_yes and not sys.stdin.isatty():
            stderr_console.print(
                f"[bold red]Error:[/bold red] Cannot start '{project_name}': required ports are "
                "in use by other Frappe projects. Re-run with --yes to stop them non-interactively."
            )
            raise typer.Exit(code=1)

        # Ask user if they want to stop conflicting projects (auto-yes skips the prompt)
        try:
            for conflicting_project in conflicting_projects:
                if assume_yes:
                    console.print(
                        f"[dim]Stopping conflicting project '{conflicting_project}' "
                        "(--yes).[/dim]"
                    )
                    answer = True
                else:
                    answer = questionary.confirm(
                        f"Stop project '{conflicting_project}' to free up its ports?",
                        default=True,
                        auto_enter=False,
                    ).unsafe_ask()

                if answer:
                    # Stop the conflicting project. A project that vanished between
                    # detection and here is fine (its ports are free either way):
                    # the post-stop recheck below is what decides.
                    from .stop import stop_project_best_effort

                    stderr_console.print(
                        f"[yellow]Stopping project '{conflicting_project}'...[/yellow]"
                    )
                    with stderr_console.status(
                        f"[bold yellow]Stopping '{conflicting_project}'...[/bold yellow]",
                        spinner="dots",
                    ):
                        stop_project_best_effort(conflicting_project, verbose=verbose)
                    console.print(
                        f"[bold green]✓[/bold green] Stopped project '{conflicting_project}'"
                    )
                else:
                    stderr_console.print(
                        f"[bold red]Error:[/bold red] Cannot start '{project_name}' while '{conflicting_project}' is using required ports."
                    )
                    raise typer.Exit(code=1)
        except KeyboardInterrupt:
            stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
            raise

        # After stopping Frappe projects, re-check ALL originally required ports
        # to catch any remaining conflicts from non-Frappe processes
        if verbose:
            stderr_console.print(
                "[dim]VERBOSE: Re-checking all ports after stopping Frappe projects...[/dim]"
            )

        ports_status = check_ports_in_use(project_ports, verbose=verbose)
        remaining_ports_in_use = [port for port, in_use in ports_status.items() if in_use]

        if remaining_ports_in_use:
            if verbose:
                stderr_console.print(
                    f"[dim]VERBOSE: Ports still in use: {remaining_ports_in_use}[/dim]"
                )

            # These must be non-Frappe processes since we just stopped all Frappe conflicts
            ports_with_processes = get_ports_in_use_with_processes(
                remaining_ports_in_use, verbose=verbose
            )

            # Group ports by process
            process_to_ports: dict[str | None, list] = {}
            for port in remaining_ports_in_use:
                process = ports_with_processes.get(port, "unknown")
                if process not in process_to_ports:
                    process_to_ports[process] = []
                process_to_ports[process].append(port)

            stderr_console.print(
                f"\n[bold red]Error:[/bold red] Cannot start '{project_name}'. Required ports are still in use by other processes:"
            )

            for process, ports in process_to_ports.items():
                formatted_ports = format_port_list(ports)
                if process == "unknown":
                    stderr_console.print(f"  • Ports {formatted_ports}: process unknown")
                else:
                    stderr_console.print(f"  • Ports {formatted_ports}: {process}")

            stderr_console.print(
                f"\n[dim]Please stop these processes before starting '{project_name}'.[/dim]"
            )
            raise typer.Exit(code=1)
        else:
            if verbose:
                stderr_console.print("[dim]VERBOSE: All ports are now available[/dim]")

    # Handle pure non-Frappe conflicts (no Frappe projects involved)
    elif non_frappe_ports:
        # Ports are in use by non-Frappe processes only
        ports_with_processes = get_ports_in_use_with_processes(
            list(non_frappe_ports), verbose=verbose
        )

        # Group ports by process
        process_to_ports = {}
        for port in non_frappe_ports:
            process = ports_with_processes.get(port, "unknown")
            if process not in process_to_ports:
                process_to_ports[process] = []
            process_to_ports[process].append(port)

        stderr_console.print(
            f"\n[bold red]Error:[/bold red] Cannot start '{project_name}'. Required ports are in use by other processes:"
        )

        for process, ports in process_to_ports.items():
            formatted_ports = format_port_list(sorted(ports))
            if process == "unknown":
                stderr_console.print(f"  • Ports {formatted_ports}: process unknown")
            else:
                stderr_console.print(f"  • Ports {formatted_ports}: {process}")

        stderr_console.print(
            f"\n[dim]Please stop these processes before starting '{project_name}'.[/dim]"
        )
        raise typer.Exit(code=1)

    return True


def detect_port_conflicts(project_name: str) -> tuple[list[str], list[int]]:
    """Detect host-port conflicts WITHOUT printing or prompting (the axi/frontend split).

    Returns ``(conflicting_frappe_projects, blocking_non_frappe_ports)``: the other
    Frappe projects holding this project's ports (auto-resolvable by stopping them)
    and the ports held by non-Frappe processes (NOT auto-resolvable). ``([], [])``
    means the ports are clear. This is the pure detection half of
    :func:`_check_port_conflicts`; the interactive prompting/printing stays there.
    """
    project_ports = get_project_ports(project_name)
    if not project_ports:
        return ([], [])
    ports_status = check_ports_in_use(project_ports)
    ports_in_use = [port for port, in_use in ports_status.items() if in_use]
    if not ports_in_use:
        return ([], [])
    frappe_projects_on_ports = find_project_using_ports(ports_in_use, exclude_project=project_name)
    frappe_ports = set(frappe_projects_on_ports.keys())
    non_frappe_ports = sorted(set(ports_in_use) - frappe_ports)
    conflicting_projects = sorted(set(frappe_projects_on_ports.values()))
    return (conflicting_projects, non_frappe_ports)


def _frappe_running(project_name: str) -> bool:
    """True iff the project's frappe container is already up (used to gate the port check)."""
    containers = get_project_containers(project_name)
    if not containers:
        return False
    frappe = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    return bool(frappe and frappe.status == "running")


@handle_docker_errors
def _start_project(
    project_name: str,
    verbose: bool = False,
    status=None,
    bench_selector=None,
    bench_path_override: str | None = None,
    restart: bool = False,
):
    """Start a single project's containers + bench, over ``core.start``.

    The thin frontend used by ``restart`` and the auto-start path
    (``ensure_containers_running`` -> ``_start_containers_for_command``). Returns
    the captured bench-start log path (or ``None`` on a soft skip), the return
    contract those callers rely on.

    Note: Port conflict checks are the caller's job (they are a host-side
    pre-step). This resolves the bench the historical lenient way for these
    callers (they have no ``--bench`` and run under a spinner, so they cannot
    prompt): an explicit ``bench_path_override`` is used VERBATIM (the post-restore
    restart must restart the SAME bench it migrated); otherwise the cached bench,
    falling back to the FIRST bench with a note on a multi-bench project
    (``on_ambiguous="first"``), and to ``core.start``'s default when nothing is
    cached. The actual container start + ``bench start`` launch + idempotency live
    in ``core.start``.

    ``restart=True`` (the post-restore restart) forces a genuine relaunch rather
    than the idempotent no-op, so the app reconnects to the restored/migrated DB.
    """
    if status:
        status.update(f"[bold green]Starting '{project_name}'...[/bold green]")

    if bench_path_override:
        resolved_path: str | None = bench_path_override
    else:
        resolved_path = resolve_bench_path(
            project_name, bench_selector, None, verbose=verbose, on_ambiguous="first"
        )

    try:
        result = core_start.start(project_name, bench_path=resolved_path, restart=restart)
    except CwcliError as e:
        return _handle_start_project_error(e, project_name)

    # Same unconditional warning list as _run_start: a web-readiness timeout must
    # be visible here too, since restart's whole-stack path and the auto-start
    # path (ensure_containers_running) both funnel through this helper.
    for warning in result.warnings:
        if warning.code in ("start.uid_align_failed", "bench.default_used", "start.web_not_ready"):
            stderr_console.print(f"[yellow]Warning: {warning.text}[/yellow]")
        elif verbose:
            stderr_console.print(f"[dim]{warning.text}[/dim]")

    outcome = result.data
    if outcome is None:  # pragma: no cover - a resolved path never yields a choice
        return None
    if verbose:
        stderr_console.print(f"[dim]VERBOSE: bench start logging to {outcome.log_path}[/dim]")
    return outcome.log_path


def _handle_start_project_error(e: CwcliError, project_name: str):
    """Map a core.start failure to the historical ``_start_project`` behavior."""
    if e.code == "project.not_found":
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        raise typer.Exit(code=1) from None
    if e.code == "frappe.not_found":
        # Historical soft skip: containers came up, but there is no bench to start.
        stderr_console.print(f"[yellow]Warning: {e.message} Skipping bench start.[/yellow]")
        return None
    if e.kind is ErrorKind.DOCKER:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    return None


@app.callback(invoke_without_command=True)
def start(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench runs 'bench start': its numeric index or label. "
        "On a multi-bench project with no --bench, prompts interactively and "
        "refuses (non-zero) on a non-TTY.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Auto-confirm stopping conflicting projects to free their ports.",
    ),
    autorestart: bool = typer.Option(
        True,
        "--autorestart/--no-autorestart",
        help="Self-heal crashed processes: supervisord restarts a program that "
        "crashes (not one that exits cleanly), surfacing a crash-loop as FATAL. "
        "Set at launch; --no-autorestart leaves a crashed program down.",
    ),
    # Accept zero, one, or more project names. Default is None.
    project_name: list[str] = typer.Argument(
        None,
        help="The name(s) of the Frappe project(s) to start. Can be piped from stdin.",
        autocompletion=complete_project_names,
    ),
):
    """
    Start a project's containers and run its bench under supervisord.

    Idempotent: re-running against an already-running bench reports "already
    running" and does nothing (no second supervisor stack). On a multi-bench
    project with no --bench, it prompts which bench interactively and refuses
    (non-zero) on a non-TTY. Crashed processes self-heal by default
    (--no-autorestart to disable).
    """
    # A variadic Argument greedily eats options placed AFTER the project name, so
    # recover -v/--verbose, -y/--yes, --autorestart/--no-autorestart, and
    # --bench <value> from the name list. An option start does NOT define is a
    # usage error there, never an extra project to start.
    filtered_project_names, recovered_flags, recovered_values = split_trailing_options(
        project_name,
        command="start",
        flags={
            "-v": ("verbose", True),
            "--verbose": ("verbose", True),
            "-y": ("yes", True),
            "--yes": ("yes", True),
            "--autorestart": ("autorestart", True),
            "--no-autorestart": ("autorestart", False),
        },
        values={"--bench": "bench"},
    )
    project_names_to_process = list(filtered_project_names)
    actual_verbose = recovered_flags.get("verbose", verbose)
    actual_yes = recovered_flags.get("yes", yes)
    actual_autorestart = recovered_flags.get("autorestart", autorestart)
    actual_bench = recovered_values.get("bench", bench)

    if not sys.stdin.isatty():
        piped_input = [line.strip() for line in sys.stdin]
        project_names_to_process.extend([name for name in piped_input if name])

    if not project_names_to_process:
        console.print(
            "[bold red]Error:[/bold red] Please provide at least one project name or pipe a list of names."
        )
        raise typer.Exit(code=1)

    console.print(
        f"Attempting to start [bold cyan]{len(project_names_to_process)}[/bold cyan] project(s)..."
    )

    # Collect per-project failures so one bad name (or unresolved conflict) never
    # aborts the good ones, but the command still exits non-zero at the end - the
    # same failures-collector honesty rm uses.
    had_failure = False

    for name in project_names_to_process:
        # Port conflicts only matter when host ports must be BOUND, i.e. when the
        # frappe container is not already up. An already-running instance owns its
        # own ports, so skip the check (this also avoids the self-conflict a naive
        # re-run would hit, letting core.start report the idempotent no-op).
        if not _frappe_running(name):
            try:
                _check_port_conflicts(name, verbose=actual_verbose, assume_yes=actual_yes)
            except KeyboardInterrupt:
                console.print("\n[yellow]Operation cancelled.[/yellow]")
                raise typer.Exit(code=1) from None
            except typer.Exit:
                # Port conflict unresolved or declined: skip this project, record
                # the failure, and continue with the rest.
                console.print(f"[yellow]Skipping project '{name}' due to port conflicts.[/yellow]")
                had_failure = True
                continue

        try:
            outcome = _run_start(name, actual_bench, actual_verbose, actual_autorestart)
        except typer.Exit as e:
            # Exit code 0 = deliberate abort, exit the entire operation. Any
            # nonzero exit = project not found, ambiguous multi-bench on a non-TTY,
            # or its bench could not be started: skip it, record it, continue.
            if e.exit_code == 0:
                raise
            console.print(f"[yellow]Skipping project '{name}': could not start.[/yellow]")
            had_failure = True
            continue

        if outcome is None:
            # Soft skip (frappe.not_found): the containers came up but there was no
            # bench to start. The warning was already printed; this is NOT a failure
            # (preserves the pre-migration behavior), so the command still exits 0.
            console.print(f"Instance '{name}' started.")
            continue

        _render_start_outcome(name, outcome)

    console.print("\n[bold green]Start command finished.[/bold green]")

    if had_failure:
        raise typer.Exit(code=1)


def _run_start(
    name: str, bench_selector: str | None, verbose: bool, autorestart: bool = True
) -> StartOutcome | None:
    """Call ``core.start`` under the spinner, resolving a multi-bench choice via a
    prompt OUTSIDE the spinner (the known spinner-over-questionary deadlock)."""
    override: str | None = None
    while True:
        try:
            with stderr_console.status(
                f"[bold green]Starting '{name}'...[/bold green]", spinner="dots"
            ):
                result = core_start.start(
                    name, bench=bench_selector, bench_path=override, autorestart=autorestart
                )
        except CwcliError as e:
            if e.code == "frappe.not_found":
                # Soft skip (matches the internal _handle_start_project_error and the
                # pre-migration behavior): the containers came up, but there is no
                # frappe service / bench to start. Warn and return a soft-skip (None) -
                # NOT a hard failure, so the whole command does not exit 1 over it.
                stderr_console.print(f"[yellow]Warning: {e.message} Skipping bench start.[/yellow]")
                return None
            _handle_start_error(e, name)

        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "select_bench"
        ):
            # Prompt (TTY) / refuse (non-TTY) which bench, then re-invoke with the
            # chosen path verbatim. bench_selector was None to reach here.
            override = resolve_bench_path(name, None, None, verbose=verbose, on_ambiguous="prompt")
            continue
        break

    # start.uid_align_failed and bench.default_used render unconditionally (the
    # open.py/restore.py/unlock.py precedent for bench.default_used, and the
    # init.uid_align_failed precedent for the uid remap failure): both signal the
    # bench workspace may not behave as expected, not just verbose diagnostics.
    for warning in result.warnings:
        if warning.code in ("start.uid_align_failed", "bench.default_used", "start.web_not_ready"):
            stderr_console.print(f"[yellow]Warning: {warning.text}[/yellow]")
        elif verbose:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
    return result.data


def _handle_start_error(e: CwcliError, name: str) -> NoReturn:
    """Render a core.start failure and Exit (nonzero -> the loop skips this project)."""
    if e.code == "project.not_found":
        console.print(f"[bold red]Error: Project '{name}' not found.[/bold red]")
    else:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        if e.hint:
            stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1)


def _render_start_outcome(name: str, outcome: StartOutcome) -> None:
    """Print the start result (idempotent no-op or a fresh launch)."""
    console.print(f"Instance '{name}' started.")
    if outcome.already_running:
        up = sum(1 for p in outcome.processes if p.pid is not None)
        total = len(outcome.processes)
        console.print(f"[bold green]✓ Already running ({up}/{total} processes up)[/bold green]")
    else:
        console.print(f"[bold green]✓ Started bench (logs: {outcome.log_path})[/bold green]")
    console.print(f"[dim]View logs with: cwcli logs {name}[/dim]")
