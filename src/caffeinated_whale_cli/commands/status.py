"""``cwcli status`` - a thin frontend over ``core.status``.

The primary line on STDOUT is the pre-computed ``overall`` aggregate
(``offline``/``online``/``running``/``degraded``) and NOTHING else, so it stays a
clean machine-parseable token (the same contract the old three-token probe had,
now enriched). The per-process health detail and every diagnostic go to STDERR,
so a script reading ``cwcli status`` still gets one word while a human at a
terminal still sees the full breakdown. Exit 0 across the lifecycle states.
"""

import typer

from ..core import status as core_status
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.status import StatusReport
from ..utils.completion_utils import complete_project_names
from ..utils.console import stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import resolve_bench_path


@handle_docker_errors
def status(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name to check.", autocompletion=complete_project_names
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to report: its numeric index or label (multi-bench projects).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show the per-process health detail and the web HTTP probe on stderr.",
    ),
):
    """
    Check the health status of a Frappe project instance.

    Prints one aggregate token on stdout - offline / online / running / degraded -
    with the per-process breakdown (up/uptime/CPU/RSS) and the web HTTP code on
    stderr. Exits 0 across all lifecycle states.
    """
    override: str | None = None
    while True:
        try:
            result = core_status.status(project_name, bench=bench, bench_path=override)
        except CwcliError as e:
            # Only an unreachable Docker daemon reaches here (absent/stopped is a
            # returned ``offline``, not a raise); surface it distinctly from offline.
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            raise typer.Exit(code=1) from None
        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "select_bench"
        ):
            override = resolve_bench_path(
                project_name, None, None, verbose=verbose, on_ambiguous="prompt"
            )
            continue
        break

    report = result.data
    assert report is not None  # OK/WARNING always carries a StatusReport
    if verbose:
        for warning in result.warnings:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
    _render_detail(report, verbose)

    # The one machine-readable token on stdout (nothing else).
    typer.echo(report.overall)
    raise typer.Exit(code=0)


_OVERALL_STYLE = {
    "running": "bold green",
    "online": "yellow",
    "degraded": "bold red",
    "offline": "dim",
}


def _render_detail(report: StatusReport, verbose: bool) -> None:
    """Render the per-process health + web probe to stderr (never stdout)."""
    style = _OVERALL_STYLE.get(report.overall, "white")
    stderr_console.print(
        f"[{style}]{report.project}: {report.overall}[/{style}]"
        f" (supervisor {'up' if report.supervisor_up else 'down'})"
    )
    if report.web_http_code is not None:
        stderr_console.print(f"[dim]web http: {report.web_http_code}[/dim]")
    elif verbose and report.container_running:
        stderr_console.print("[dim]web http: no response[/dim]")

    for p in report.processes:
        mark = "[green]up[/green]" if p.up else "[red]down[/red]"
        detail = ""
        if p.up:
            bits = []
            if p.pid is not None:
                bits.append(f"pid={p.pid}")
            if p.uptime_s is not None:
                bits.append(f"uptime={p.uptime_s}s")
            if p.cpu_pct is not None:
                bits.append(f"cpu={p.cpu_pct}%")
            if p.rss_kb is not None:
                bits.append(f"rss={p.rss_kb}KB")
            detail = "  " + " ".join(bits) if bits else ""
        stderr_console.print(f"  {p.label:<16} {mark}{detail}")
