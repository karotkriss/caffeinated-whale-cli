"""``cwcli status`` - a thin frontend over ``core.status``.

The primary line on STDOUT is the pre-computed ``overall`` aggregate
(``offline``/``online``/``running``/``degraded``) and NOTHING else, so it stays a
clean machine-parseable token (the same contract the old three-token probe had,
now enriched). It is the INSTANCE fold over every reported bench; with ``--bench``
it is that one bench's aggregate by construction. The per-bench, per-process health
detail and every diagnostic go to STDERR, so a script reading ``cwcli status`` still
gets one word while a human at a terminal still sees the full breakdown. Exit 0
across the lifecycle states (including a real-but-stopped ``offline`` project); a
truly-nonexistent project name exits non-zero with a "no such project" error.

There is NO prompt on any path. The bare form on a multi-bench project used to ask
which bench; it now reports all of them, each headed by its index, path, label, and
own aggregate, with the web line naming the port that was actually probed.

``--watch`` adds a live, continuously-refreshing per-process view (rich ``Live``
on stderr). Crucially it re-polls with ``probe_web=False`` so repeated ticks make
ZERO ``curl`` requests against the bench - the whole point is to
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

from ..core import resolvers
from ..core import status as core_status
from ..core.envelope import Result
from ..core.errors import CwcliError
from ..core.status import BenchStatus, StatusReport
from ..utils.completion_utils import complete_project_names
from ..utils.console import stderr_console
from ..utils.docker_utils import handle_docker_errors

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
    result = _fetch(project_name, bench, probe_web=not watch)
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
    *,
    probe_web: bool,
) -> Result[StatusReport]:
    """One snapshot. NON-PROMPTING on every path.

    The multi-bench prompt-and-retry loop that used to live here is gone: the core
    reports every bench instead of returning ``select_bench``, so there is nothing
    left to ask. That makes ``status`` non-prompting everywhere, which satisfies the
    both-modes standard trivially - ``--bench`` remains the non-interactive selector
    it already was, and a non-TTY no longer has a path that could hang or refuse.
    """
    try:
        return core_status.status(project_name, bench=bench, probe_web=probe_web)
    except CwcliError as e:
        # A truly-nonexistent project (NOT_FOUND) or an unreachable Docker daemon
        # (DOCKER) reaches here; a real-but-stopped project is a returned
        # ``offline``, not a raise. Either way, surface it distinctly from
        # offline with a clear message and a non-zero exit.
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None


def _watch_loop(project_name: str, bench: str | None, verbose: bool, interval: float) -> None:
    """Live per-process view on stderr, re-polling with the web probe suppressed.

    Every tick re-polls with ``probe_web=False``, so the watch loop makes zero HTTP
    requests to the bench. Ctrl-C exits cleanly (Live restores the terminal on
    context exit); exit 0, nothing on stdout.
    """
    try:
        report = _snapshot(project_name, bench, probe_web=False)
        with Live(_render_table(report), console=stderr_console, refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                report = _snapshot(project_name, bench, probe_web=False)
                live.update(_render_table(report))
    except KeyboardInterrupt:
        pass


def _snapshot(project_name: str, bench: str | None, *, probe_web: bool) -> StatusReport:
    """A quiet (``probe_web=False``) watch snapshot."""
    result = _fetch(project_name, bench, probe_web=probe_web)
    assert result.data is not None
    return result.data


_OVERALL_STYLE = {
    "running": "bold green",
    "online": "yellow",
    "degraded": "bold red",
    "offline": "dim",
}


def _title(report: StatusReport) -> str:
    """The styled ``{project}: {overall}`` instance heading (the folded aggregate)."""
    style = _OVERALL_STYLE.get(report.overall, "white")
    suffix = "" if len(report.benches) == 1 else f" ({len(report.benches)} benches)"
    return f"[{style}]{report.project}: {report.overall}[/{style}]{suffix}"


def _bench_heading(bench: BenchStatus) -> str:
    """One bench's sub-heading: which bench, its own aggregate, its supervisor state."""
    style = _OVERALL_STYLE.get(bench.overall, "white")
    # No square brackets around the index: stderr_console renders rich markup, and
    # "[0]" would be parsed as a (bogus) style tag.
    index = "-" if bench.index is None else str(bench.index)
    name = f"bench {index}  {bench.bench_path}"
    if bench.label:
        name = f"{name} ('{bench.label}')"
    if bench.bench_present == resolvers.BENCH_ABSENT:
        # Said before the health, because the health is about a directory that is
        # not there: "online" on a deleted bench reads as "here, just not up".
        return (
            f"{name}: [bold red]GONE[/bold red] (directory no longer exists; "
            f"reported from cache - run `cwcli inspect --update`)"
        )
    presence = (
        " [yellow](cached, not verified)[/yellow]"
        if bench.bench_present == resolvers.BENCH_UNVERIFIED
        else ""
    )
    if bench.not_cwcli_supervised:
        # Running under honcho / bench start: "supervisor down" would be a lie.
        supervisor = "not under cwcli supervision"
    else:
        supervisor = f"supervisor {'up' if bench.supervisor_up else 'down'}"
    return f"{name}: [{style}]{bench.overall}[/{style}] ({supervisor}){presence}"


def _up_mark(up: bool) -> str:
    """The shared per-process up/down mark."""
    return "[green]up[/green]" if up else "[red]down[/red]"


def _web_line(bench: BenchStatus, verbose: bool) -> str | None:
    """The web line, NAMING the port and the site that were probed (or why none was).

    An unattributed "web http: 404" is what let one bench's HTTP code stand in for
    another's; the port is part of the answer, not decoration. The site is part of
    it too, because Frappe answers per Host - the code is that site's code.
    """
    if not bench.web_port_verified:
        return "[yellow]web port unknown - not probed; run `cwcli inspect`[/yellow]"
    target = f":{bench.web_port}"
    if bench.web_site:
        target = f"{bench.web_site}{target}"
    if bench.web_http_code is not None:
        return f"[dim]web {target} -> {bench.web_http_code}[/dim]"
    if verbose:
        return f"[dim]web {target} -> no response[/dim]"
    return None


def _render_detail(report: StatusReport, verbose: bool) -> None:
    """Render each bench's health + its own web probe to stderr (never stdout)."""
    stderr_console.print(_title(report))
    for bench in report.benches:
        stderr_console.print(_bench_heading(bench))
        # Everything below a bench heading is indented under it, so a multi-bench
        # report reads as blocks rather than one flat wall of lines.
        if bench.not_cwcli_supervised:
            stderr_console.print(f"  [yellow]{core_status.NOT_CWCLI_SUPERVISED_HINT}[/yellow]")
        line = _web_line(bench, verbose)
        if line is not None:
            stderr_console.print(f"  {line}")

        for p in bench.processes:
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
    """The live ``--watch`` frame: one row per process (no web probe, so no web row).

    The ``bench`` column appears only when more than one bench is reported, so the
    single-bench live view is byte-identical to what it has always been.
    """
    multi = len(report.benches) > 1
    table = Table(
        title=_title(report),
        title_justify="left",
        expand=False,
    )
    if multi:
        table.add_column("bench")
    table.add_column("process")
    table.add_column("status")
    table.add_column("state")
    table.add_column("pid", justify="right")
    table.add_column("uptime", justify="right")
    table.add_column("cpu%", justify="right")
    table.add_column("rss", justify="right")
    for bench in report.benches:
        for p in bench.processes:
            row = [
                p.label,
                _up_mark(p.up),
                p.state if p.state is not None else "-",
                str(p.pid) if p.pid is not None else "-",
                f"{p.uptime_s}s" if p.uptime_s is not None else "-",
                f"{p.cpu_pct}" if p.cpu_pct is not None else "-",
                f"{p.rss_kb}KB" if p.rss_kb is not None else "-",
            ]
            if multi:
                row.insert(0, bench.label or ("-" if bench.index is None else str(bench.index)))
            table.add_row(*row)
    return table
