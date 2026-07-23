import sys

import typer

from ..core import stop as core_stop
from ..core.errors import CwcliError, ErrorKind
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(help="Stop a Frappe project's containers.")


@app.callback(invoke_without_command=True)
@handle_docker_errors
def stop(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Stop only THIS bench's dev processes (numeric index or label), "
        "leaving sibling benches and every container running. Omit to stop the "
        "whole instance's containers.",
    ),
    project_name: list[str] = typer.Argument(
        None,
        help="The name(s) of the Frappe project(s) to stop. Can be piped from stdin.",
        autocompletion=complete_project_names,
    ),
):
    """
    Stops all containers for a given project or for all projects piped from stdin.

    With --bench, stops just that bench's dev processes (its supervisord) and leaves
    the containers and every sibling bench running - the inverse of
    `cwcli start --bench`.
    """
    project_names_to_process = []

    # A variadic Argument greedily eats options placed AFTER the project name, so
    # recover -v/--verbose and --bench <value> from the name list (the same
    # forgiveness start and restart apply to their trailing flags).
    actual_verbose = verbose
    actual_bench = bench
    missing_bench_value = False
    filtered_project_names = []

    if project_name:
        tokens = list(project_name)
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in ("-v", "--verbose"):
                actual_verbose = True
            elif token == "--bench":
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    actual_bench = tokens[i + 1]
                    i += 1
                else:
                    missing_bench_value = True
            elif token.startswith("--bench="):
                actual_bench = token.split("=", 1)[1]
                if not actual_bench:
                    missing_bench_value = True
            else:
                filtered_project_names.append(token)
            i += 1
        project_names_to_process.extend(filtered_project_names)

    if missing_bench_value:
        stderr_console.print(
            "[bold red]Error:[/bold red] Option '--bench' requires a value."
        )
        raise typer.Exit(code=2)

    if not sys.stdin.isatty():
        piped_input = [line.strip() for line in sys.stdin]
        project_names_to_process.extend([name for name in piped_input if name])

    if not project_names_to_process:
        console.print(
            "[bold red]Error:[/bold red] Please provide at least one project name or pipe a list of names."
        )
        raise typer.Exit(code=1)

    if actual_bench is not None:
        _stop_benches(project_names_to_process, actual_bench, actual_verbose)
        return

    console.print(
        f"Attempting to stop [bold yellow]{len(project_names_to_process)}[/bold yellow] project(s)..."
    )

    had_failure = False

    for name in project_names_to_process:
        try:
            with stderr_console.status(
                f"[bold yellow]Stopping '{name}'...[/bold yellow]", spinner="dots"
            ) as status:
                if actual_verbose:
                    stderr_console.print(f"[dim]VERBOSE: Stopping project '{name}'[/dim]")
                status.update(f"[bold yellow]Stopping '{name}'...[/bold yellow]")
                result = core_stop.stop(name)
        except CwcliError as e:
            # A missing project (or an unreachable daemon) is a per-project failure:
            # keep processing the rest, then exit non-zero, rather than falsely
            # reporting success.
            console.print(f"[bold red]Error: {e.message}[/bold red]")
            had_failure = True
            continue

        outcome = result.data
        assert outcome is not None  # OK always carries a StopOutcome

        if actual_verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Stopped {outcome.stopped} container(s) for '{name}'[/dim]"
            )

        # Print outside the spinner context.
        if outcome.already_stopped:
            console.print(f"Instance '{name}' is already stopped.")
        else:
            console.print(f"Instance '{name}' stopped.")

    console.print("\n[bold yellow]Stop command finished.[/bold yellow]")

    if had_failure:
        raise typer.Exit(code=1)


def _stop_benches(names: list[str], bench: str, verbose: bool) -> None:
    """``--bench``: stop one bench per named project; honest per-project exit code."""
    had_failure = False
    for name in names:
        try:
            outcome = _run_stop_bench(name, bench, verbose)
        except CwcliError as e:
            console.print(f"[bold red]Error: {e.message}[/bold red]")
            if e.hint:
                stderr_console.print(f"[dim]{e.hint}[/dim]")
            had_failure = True
            continue
        if outcome.already_stopped:
            console.print(
                f"Bench '{outcome.bench_path}' of '{name}' is already stopped "
                "(no dev processes running)."
            )
        else:
            processes = ", ".join(outcome.stopped_processes) or "no processes"
            console.print(
                f"Bench '{outcome.bench_path}' of '{name}' stopped "
                f"([bold cyan]{processes}[/bold cyan]); other benches and containers "
                "are untouched."
            )
        console.print(f"[dim]Start it again with: cwcli start {name} --bench {bench}[/dim]")

    if had_failure:
        raise typer.Exit(code=1)


def _run_stop_bench(name: str, bench: str, verbose: bool) -> core_stop.BenchStopOutcome:
    """Call ``core.stop_bench`` for a NAMED bench.

    No prompt, and none is possible: this path is reached only when ``--bench``
    carried a value, so ``core.stop_bench``'s ``select_bench`` fork - which exists
    for a caller that passes no selector at all - is unreachable from here. An
    unresolvable selector is a ``CwcliError`` the caller renders.
    """
    with stderr_console.status(
        f"[bold yellow]Stopping bench '{bench}' of '{name}'...[/bold yellow]", spinner="dots"
    ):
        result = core_stop.stop_bench(name, bench=bench)

    for warning in result.warnings:
        if warning.code == "bench.default_used":
            stderr_console.print(f"[yellow]Warning: {warning.text}[/yellow]")
        elif verbose:
            stderr_console.print(f"[dim]{warning.text}[/dim]")

    assert result.data is not None  # OK always carries a BenchStopOutcome
    return result.data


def stop_project_best_effort(project_name: str, verbose: bool = False) -> int | None:
    """Stop a project for another CLI frontend, returning the count stopped, or None
    if the project does not exist.

    A frontend-side rendering adapter, NOT logic: `core.stop` owns the behavior and
    this only maps its typed `NOT_FOUND` back to the `None` the two remaining
    callers (`restart`'s stop-then-start, `start`'s port-conflict resolution) were
    written against. Unlike the `_stop_project` it replaces, everything it prints
    goes to STDERR, so no caller can corrupt a machine-readable stdout. `axi` and
    `rm` do not use it: `axi` calls `core.stop` directly (its errors are TOON), and
    `rm`'s return-to-stopped already treats any exception as a best-effort warning.
    """
    try:
        result = core_stop.stop(project_name)
    except CwcliError as e:
        if e.kind is ErrorKind.NOT_FOUND:
            stderr_console.print(f"[bold red]Error: {e.message}[/bold red]")
            return None
        raise

    outcome = result.data
    assert outcome is not None
    if verbose:
        stderr_console.print(
            f"[dim]VERBOSE: Stopped {outcome.stopped} container(s) for '{project_name}'[/dim]"
        )
    return outcome.stopped
