"""``cwcli status`` - a thin frontend over ``core.status``.

The primary line on STDOUT is the pre-computed ``overall`` aggregate
(``offline``/``online``/``running``/``degraded``) and NOTHING else, so it stays a
clean machine-parseable token (the same contract the old three-token probe had,
now enriched). The per-process health detail and every diagnostic go to STDERR,
so a script reading ``cwcli status`` still gets one word while a human at a
terminal still sees the full breakdown. Exit 0 across the lifecycle states
(including a real-but-stopped ``offline`` project); a truly-nonexistent project
name exits non-zero with a "no such project" error instead.

``--watch`` adds a live, continuously-refreshing per-process view (rich ``Live``
on stderr). Crucially it re-polls with ``probe_web=False`` so repeated ticks make
ZERO ``curl localhost:8000`` requests against the bench - the whole point is to
watch health WITHOUT spamming the Frappe access logs. Per-process health still
comes from the single ``ps`` read each tick, which leaves no log trace. When
stdout or stderr is not a TTY (piped/redirected - the live view renders to
stderr, so both streams must be interactive), ``--watch`` degrades to one quiet
snapshot instead of starting the live loop.
"""

import sys
import time

import typer
from rich.live import Live
from rich.table import Table

from ..core import status as core_status
from ..core.envelope import Result, Status
from ..core.errors import CwcliError
from ..core.status import StatusReport
from ..utils.completion_utils import complete_project_names
from ..utils.console import stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import resolve_bench_path

_MIN_INTERVAL = 1.0


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
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="Live, continuously-refreshing per-process view (no web probe - leaves "
        "no HTTP requests in the bench logs). Ctrl-C to exit.",
    ),
    interval: float = typer.Option(
        2.0,
        "--interval",
        help="Seconds between --watch refreshes (floored at 1s).",
    ),
):
    """
    Check the health status of a Frappe project instance.

    Prints one aggregate token on stdout - offline / online / running / degraded -
    with the per-process breakdown (up/uptime/CPU/RSS) and the web HTTP code on
    stderr. Exits 0 across all lifecycle states (a stopped instance is ``offline``);
    a truly-nonexistent project name exits non-zero with a "no such project" error.

    ``--watch`` shows a live refreshing view without probing the web server (so
    repeated ticks leave no HTTP requests in the bench's access logs); it degrades
    to a single snapshot when stdout or stderr is not a TTY.
    """
    if watch and sys.stdout.isatty() and sys.stderr.isatty():
        # Live view: quiet (probe_web=False) so repeated ticks never hit :8000.
        _watch_loop(project_name, bench, verbose, max(_MIN_INTERVAL, interval))
        raise typer.Exit(code=0)

    # One-shot. The plain command keeps its web probe; a non-TTY --watch degrades
    # to a single quiet snapshot (probe_web=False) to preserve watch semantics.
    result, _ = _fetch(project_name, bench, None, verbose, probe_web=not watch)
    report = result.data
    assert report is not None  # OK/WARNING always carries a StatusReport
    if verbose:
        for warning in result.warnings:
            # The not-cwcli-supervised hint is rendered by _render_detail (in BOTH
            # modes), so don't also echo it here as a warning - it would double up.
            if warning.code == "supervisor.not_cwcli":
                continue
            stderr_console.print(f"[dim]{warning.text}[/dim]")
    _render_detail(report, verbose)

    # The one machine-readable token on stdout (nothing else).
    typer.echo(report.overall)
    raise typer.Exit(code=0)


def _fetch(
    project_name: str,
    bench: str | None,
    override: str | None,
    verbose: bool,
    *,
    probe_web: bool,
) -> tuple[Result[StatusReport], str | None]:
    """One snapshot, resolving a multi-bench ``NEEDS_CHOICE`` via a prompt.

    Returns ``(result, resolved_bench_path)`` so a watch loop can resolve the bench
    once up front and re-poll with the settled path (no per-tick re-prompt).
    """
    while True:
        try:
            result = core_status.status(
                project_name, bench=bench, bench_path=override, probe_web=probe_web
            )
        except CwcliError as e:
            # A truly-nonexistent project (NOT_FOUND) or an unreachable Docker daemon
            # (DOCKER) reaches here; a real-but-stopped project is a returned
            # ``offline``, not a raise. Either way, surface it distinctly from
            # offline with a clear message and a non-zero exit.
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
    assert result.data is not None
    return result, override


def _watch_loop(project_name: str, bench: str | None, verbose: bool, interval: float) -> None:
    """Live per-process view on stderr, re-polling with the web probe suppressed.

    The bench is resolved once (prompting if ambiguous) BEFORE the live view takes
    over the terminal; every subsequent tick re-polls with the settled path and
    ``probe_web=False``, so the watch loop makes zero HTTP requests to the bench.
    Ctrl-C exits cleanly (Live restores the terminal on context exit); exit 0,
    nothing on stdout.
    """
    try:
        report, override = _snapshot(project_name, bench, None, verbose)
        with Live(_render_table(report), console=stderr_console, refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                report, override = _snapshot(project_name, bench, override, verbose)
                live.update(_render_table(report))
    except KeyboardInterrupt:
        pass


def _snapshot(
    project_name: str, bench: str | None, override: str | None, verbose: bool
) -> tuple[StatusReport, str | None]:
    """A quiet (``probe_web=False``) watch snapshot: the report plus settled path."""
    result, override = _fetch(project_name, bench, override, verbose, probe_web=False)
    assert result.data is not None
    return result.data, override


_OVERALL_STYLE = {
    "running": "bold green",
    "online": "yellow",
    "degraded": "bold red",
    "offline": "dim",
}


def _title(report: StatusReport) -> str:
    """The shared styled ``{project}: {overall} (supervisor state)`` heading."""
    style = _OVERALL_STYLE.get(report.overall, "white")
    if report.not_cwcli_supervised:
        # Running under honcho / bench start: "supervisor down" would be a lie.
        supervisor = "not under cwcli supervision"
    else:
        supervisor = f"supervisor {'up' if report.supervisor_up else 'down'}"
    return f"[{style}]{report.project}: {report.overall}[/{style}] ({supervisor})"


def _up_mark(up: bool) -> str:
    """The shared per-process up/down mark."""
    return "[green]up[/green]" if up else "[red]down[/red]"


def _render_detail(report: StatusReport, verbose: bool) -> None:
    """Render the per-process health + web probe to stderr (never stdout)."""
    stderr_console.print(_title(report))
    if report.not_cwcli_supervised:
        stderr_console.print(f"[yellow]{core_status.NOT_CWCLI_SUPERVISED_HINT}[/yellow]")
    if report.web_http_code is not None:
        stderr_console.print(f"[dim]web http: {report.web_http_code}[/dim]")
    elif verbose and report.container_running:
        stderr_console.print("[dim]web http: no response[/dim]")

    for p in report.processes:
        mark = _up_mark(p.up)
        bits = []
        if p.state is not None:
            bits.append(f"state={p.state}")
        if p.up:
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


def _render_table(report: StatusReport) -> Table:
    """The live ``--watch`` frame: one row per process (no web probe, so no web row)."""
    table = Table(
        title=_title(report),
        title_justify="left",
        expand=False,
    )
    table.add_column("process")
    table.add_column("status")
    table.add_column("state")
    table.add_column("pid", justify="right")
    table.add_column("uptime", justify="right")
    table.add_column("cpu%", justify="right")
    table.add_column("rss", justify="right")
    for p in report.processes:
        mark = _up_mark(p.up)
        table.add_row(
            p.label,
            mark,
            p.state if p.state is not None else "-",
            str(p.pid) if p.pid is not None else "-",
            f"{p.uptime_s}s" if p.uptime_s is not None else "-",
            f"{p.cpu_pct}" if p.cpu_pct is not None else "-",
            f"{p.rss_kb}KB" if p.rss_kb is not None else "-",
        )
    return table
