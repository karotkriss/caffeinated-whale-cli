"""``cwcli scale`` - the human frontend over ``core.scale``.

Renders the coarse progress to stderr under a status, gates the whole-instance
restart behind a confirmation (honoring ``--yes``, resolving the core's
``confirm_scale`` NEEDS_CHOICE), and prints the new host port map on success.
The logic - reading each bench's port truth, widening the compose range,
recreating only frappe, repairing v13/v14 toolchains, relaunching supervisord -
all lives on the core; this only prompts, spins, and renders.
"""

import sys

import typer

from ..core import scale as core_scale
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.scale import ScaleProgress, ScaleReport
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors


def _render(report: ScaleReport) -> None:
    """Print the outcome: the new host port map, plus what happened."""
    if not report.expanded:
        console.print(
            f"Instance '{report.project}' already publishes enough ports "
            f"({report.published_ports}); nothing to expand."
        )
    else:
        console.print(
            f"[bold green]Expanded '{report.project}'[/bold green] from "
            f"{report.previous_published_ports} to {report.published_ports} published ports "
            f"and restarted {report.benches_restarted} bench(es)."
        )
        if report.toolchain_repaired:
            console.print(
                f"Repaired the toolchain of {len(report.toolchain_repaired)} old-major bench(es)."
            )

    console.print("\n[bold]Host port map:[/bold]")
    for bench in report.port_map:
        label = f" ({bench.label})" if bench.label else ""
        if not bench.ports_verified:
            console.print(
                f"  {bench.bench_path}{label}: [yellow]ports unknown (unreadable)[/yellow]"
            )
            continue
        mark = "[green]reachable[/green]" if bench.reachable else "[red]UNREACHABLE[/red]"
        console.print(
            f"  {bench.bench_path}{label}: "
            f"web localhost:{bench.host_web_port} socketio localhost:{bench.host_socketio_port} "
            f"- {mark}"
        )


@handle_docker_errors
def scale(
    project_name: str = typer.Argument(
        ...,
        help="The Frappe project (instance) to scale.",
        autocompletion=complete_project_names,
    ),
    to: int = typer.Option(
        None,
        "--to",
        help="Ensure at least this many benches are reachable from the host "
        "(publish at least this many ports). Omit to auto-fit every bench.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the whole-instance-restart confirmation.",
    ),
) -> None:
    """Widen the instance's published port range so every bench is reachable.

    A Frappe instance publishes a fixed range of six web / six socketio ports at
    init; a 7th serving bench binds inside the container but is silently
    unreachable from the host. This reconciles the published range to cover every
    bench's assigned port, database-safe (only the frappe service is recreated,
    with ``--no-deps``), repairing the runtime toolchain of any v13/v14 bench that
    recreation wipes, then relaunches every bench.

    Expanding restarts every serving bench in the instance (they share one
    container) - unavoidable, so it is confirmed unless ``--yes``.
    """

    def on_event(event: ScaleProgress) -> None:
        stderr_console.print(f"[dim]{event.message}[/dim]")

    try:
        # First call, without consent: the core tells us whether an expansion (and
        # its whole-instance restart) is actually needed, so the confirmation is
        # only ever shown when something will restart.
        result = core_scale.scale(project_name, to=to, consent=False)

        if result.status is Status.NEEDS_CHOICE:
            assert result.choice is not None
            if not _confirm_restart(result.choice.prompt, assume_yes=yes):
                stderr_console.print("[yellow]Scale cancelled.[/yellow]")
                raise typer.Exit(code=1)
            with stderr_console.status("[bold]Expanding port range...[/bold]", spinner="dots"):
                result = core_scale.scale(project_name, to=to, consent=True, on_event=on_event)
    except CwcliError as error:
        stderr_console.print(f"[bold red]Error:[/bold red] {error.message}")
        if error.hint:
            console.print(error.hint)
        raise typer.Exit(code=1) from None

    for warning in result.warnings:
        stderr_console.print(f"[yellow]Warning:[/yellow] {warning.text}")

    assert result.data is not None  # OK/WARNING always carries a ScaleReport
    _render(result.data)


def _confirm_restart(prompt: str, *, assume_yes: bool) -> bool:
    """Confirm the whole-instance restart, honoring ``--yes`` and refusing a non-TTY.

    Mirrors the ``confirm_or_exit`` contract but returns a bool so the caller frames
    its own cancel message: proceed on ``--yes``; refuse (return False) on a non-TTY
    without ``--yes``; otherwise ask.
    """
    if assume_yes:
        console.print("[dim]Proceeding without confirmation (--yes).[/dim]")
        return True

    if not sys.stdin.isatty():
        stderr_console.print(
            "[bold red]Error:[/bold red] Expanding restarts every serving bench; "
            "re-run with --yes to proceed non-interactively."
        )
        raise typer.Exit(code=1)

    import questionary

    try:
        answer = questionary.confirm(prompt, default=False).ask()
    except (KeyboardInterrupt, EOFError):
        return False
    return bool(answer)
