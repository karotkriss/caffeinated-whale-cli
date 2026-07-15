"""``cwcli run`` - the thin frontend over ``core.run_plan`` + ``core.exec_stream``.

Typer signature, the auto-start prompt, rendering, and the exit code live here;
everything else is the core's. See ``core/run.py`` for why the resolve and the
stream are two calls (generators are lazy), and ``core/exec_stream.py`` for why
the exit code is polled rather than read once.
"""

import typer
from rich.console import Console

from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.exec_stream import ExecChunk
from ..core.run import run_plan, run_stream
from ..utils.completion_utils import complete_project_names
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running

stderr_console = Console(stderr=True)


@handle_docker_errors
def run(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench_args: list[str] = typer.Argument(..., help="Bench command and arguments to run."),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to target: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Execute `bench <command>` inside the specified project's frappe container.

    Flags this command does not define are passed through to bench, so
    `cwcli run my-project --site example.com migrate` works. The options below
    stay this command's wherever they appear; to send bench a flag that collides
    with one of them, put it after a `--` separator, as in
    `cwcli run my-project -- build --verbose`.
    """
    # Interactive prologue: prompts happen HERE, before the core call. The core
    # then re-checks and only returns confirm_start on the (rare) race.
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    started = False
    while True:
        try:
            result = run_plan(
                project_name, bench_args, bench=bench, bench_path=bench_path, auto_start=yes
            )
        except CwcliError as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            if e.hint:
                stderr_console.print(f"[dim]{e.hint}[/dim]")
            raise typer.Exit(code=1) from e

        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "confirm_start"
        ):
            # Mirrors backup.py/unlock.py: re-invoke at most ONCE after an attempted
            # start. A second confirm_start after ensure_containers_running already
            # claimed success means the start didn't take - fail closed, don't spin.
            if started:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Frappe container for project "
                    f"'{project_name}' failed to start."
                )
                raise typer.Exit(code=1)
            ensure_containers_running(
                project_name, require_running=True, verbose=verbose, auto_start=yes
            )
            started = True
            continue
        break

    if result.choice is not None:
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        for option in result.choice.options or []:
            stderr_console.print(f"  [dim]{option['value']}[/dim]  {option['label']}")
        stderr_console.print("[dim]Pass --bench <index|label> to choose one.[/dim]")
        raise typer.Exit(code=1)

    plan = result.data
    assert plan is not None  # OK always carries a RunPlan

    if verbose:
        for warning in result.warnings:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
        stderr_console.print(f"[dim]$ {plan.command}  (in {plan.bench_path})[/dim]")

    exit_code = 1
    try:
        for event in run_stream(plan):
            if isinstance(event, ExecChunk):
                # Both tags to stdout, reproducing the combined stream this
                # command has always shown.
                typer.echo(event.text, nl=False)
            else:
                exit_code = event.exit_code
    except CwcliError as e:
        # An unknown exit code lands here rather than being reported as success.
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        if e.hint:
            stderr_console.print(f"[dim]{e.hint}[/dim]")
        raise typer.Exit(code=1) from e

    raise typer.Exit(code=exit_code)
