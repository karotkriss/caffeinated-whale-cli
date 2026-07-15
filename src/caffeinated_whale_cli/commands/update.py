"""``cwcli update`` - the DEPRECATED alias for ``cwcli apps update``, plus the shared
frontend both spellings render through.

The state machine itself lives in :mod:`..core.update`; everything here is
presentation: a renderer that turns the core's typed events into this CLI's own
`rich` output, the seven-way summary printed from the returned
:class:`~..core.update.UpdateReport`, and the exit code.

The two spellings are NOT interface-identical and both must keep working:
``cwcli apps update <proj> erpnext`` takes apps POSITIONALLY, while the deprecated
``cwcli update <proj> --app erpnext`` takes them as a repeatable ``--app``/``-a``
OPTION. Unifying the signatures would break every existing ``cwcli update ... --app
x`` invocation. They share ONE implementation (``run_app_update`` -> ``core.update``)
so they cannot drift, and the alias stays a frontend rather than a second core path.
"""

import dataclasses
import json
import sys

import typer
from rich.status import Status as RichStatus

from ..core import update as core_update
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.update import UpdateAborted, UpdateOutput, UpdateReport, UpdateStepEnd, UpdateStepStart
from ..utils.completion_utils import complete_app_names, complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors

# Per-phase presentation. Each entry is (verbose header, non-verbose spinner label,
# success line, failure line) with {item} / {index} / {total} placeholders; None
# means "say nothing at this point", which is what the quiet phases want.
_PHASES = {
    "pull": {
        "header": "[bold green]→[/bold green] Updating app: [cyan]{item}[/cyan]",
        "spinner": "Pulling app: {item} ({index}/{total})",
        "ok": "[bold green]✓[/bold green] Successfully updated '{item}'",
        "failed": "[bold red]✗[/bold red] Failed to update app '{item}'",
    },
    "frappe_reset": {
        "banner": "[bold cyan]Updating the frappe framework with 'bench update --reset'[/bold cyan]\n",
        "spinner": "Running bench update --reset...",
        "ok": "[bold green]✓[/bold green] Frappe framework updated",
        "failed": "[bold red]✗[/bold red] 'bench update --reset' failed",
    },
    "migrate": {
        "header": "\n[bold]Migrating site {index}/{total}: {item}[/bold]",
        "spinner": "Migrating site: {item} ({index}/{total})",
        "ok": "[bold green]✓[/bold green] Migration completed for '{item}'",
        "failed": "[bold red]✗[/bold red] Migration failed for site '{item}'",
    },
    "build": {
        "header": "[bold green]→[/bold green] Building app: [cyan]{item}[/cyan]",
        "spinner": "Building app: {item} ({index}/{total})",
        "ok": "[bold green]✓[/bold green] Assets built successfully for '{item}'",
        "failed": "[bold red]✗[/bold red] Failed to build assets for '{item}'",
    },
    "clear_cache": {
        "spinner": "Clearing cache for '{item}'...",
        "ok": "[bold green]✓[/bold green] Cache cleared for '{item}'",
        "failed": "[bold red]✗[/bold red] Failed to clear cache for '{item}'",
    },
    "clear_website_cache": {
        "spinner": "Clearing website cache for '{item}'...",
        "ok": "[bold green]✓[/bold green] Website cache cleared for '{item}'",
        "failed": "[bold red]✗[/bold red] Failed to clear website cache for '{item}'",
    },
    "clear_locks": {
        "spinner": "Clearing locks for '{item}'...",
        "ok": "[bold green]✓[/bold green] Locks cleared for '{item}'",
    },
    "maintenance_disable": {
        "ok": "[bold green]✓[/bold green] Maintenance mode disabled for '{item}'",
        "failed": "[bold red]✗[/bold red] Site '{item}' left in maintenance mode",
    },
}

# Printed once, before the phase's first step.
_PHASE_INTROS = {
    "maintenance_enable": "[bold cyan]Enabling maintenance mode...[/bold cyan]",
    "clear_cache": "\n[bold cyan]Clearing cache for {total} site(s)[/bold cyan]",
    "clear_website_cache": "\n[bold cyan]Clearing website cache for {total} site(s)[/bold cyan]",
    "clear_locks": "\n[bold cyan]Clearing locks for {total} site(s)[/bold cyan]",
    "recache": "\n[dim]Re-caching project to ensure accurate site data...[/dim]",
    "maintenance_disable": "\n[bold cyan]Disabling maintenance mode...[/bold cyan]",
}

# Phases whose intro only makes sense when the user asked to see the detail.
_VERBOSE_ONLY_INTROS = {"maintenance_disable"}


class _Renderer:
    """Renders ``core.update``'s typed events in this CLI's style.

    The core decides WHAT happened; this decides whether and how to show it - the
    same division the exec-stream contract draws, where ``verbose`` picks a
    consumption mode rather than an exec mechanism. Non-verbose runs each step under
    a spinner that streaming would shred, so it renders step boundaries only.
    """

    def __init__(self, verbose: bool):
        self.verbose = verbose
        self._spinner: RichStatus | None = None
        self._intro_done: set[str] = set()

    def __call__(self, event) -> None:
        if isinstance(event, UpdateStepStart):
            self._on_start(event)
        elif isinstance(event, UpdateOutput):
            self._on_output(event)
        elif isinstance(event, UpdateStepEnd):
            self._on_end(event)
        elif isinstance(event, UpdateAborted):
            # The run is unwinding, so this report will never be returned. Print it
            # here or lose a stuck site's remediation entirely.
            self._stop_spinner()
            _report_summary(event.report)

    # -- spinner ---------------------------------------------------------------

    def _start_spinner(self, label: str) -> None:
        self._stop_spinner()
        self._spinner = console.status(f"[bold green]{label}[/bold green]", spinner="dots")
        self._spinner.start()

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    # -- events ----------------------------------------------------------------

    def _on_start(self, event) -> None:
        self._stop_spinner()
        spec = _PHASES.get(event.phase, {})
        fields = {"item": event.item, "index": event.index, "total": event.total}

        if event.message:
            stderr_console.print(f"[yellow]Note:[/yellow] {event.message}")

        intro = _PHASE_INTROS.get(event.phase)
        if intro and event.phase not in self._intro_done:
            self._intro_done.add(event.phase)
            if event.phase not in _VERBOSE_ONLY_INTROS or self.verbose:
                console.print(intro.format(**fields))

        if banner := spec.get("banner"):
            console.print(banner)

        if self.verbose:
            if header := spec.get("header"):
                console.print(header.format(**fields))
        elif label := spec.get("spinner"):
            self._start_spinner(label.format(**fields))

    def _on_output(self, event) -> None:
        if not self.verbose:
            return
        # Raw stdout, not console.print: this is bench's own output, and rich would
        # re-wrap it and eat the carriage returns its progress bars depend on. Both
        # tags go to stdout in yield order, reproducing the combined stream.
        sys.stdout.write(event.text)
        sys.stdout.flush()

    def _on_end(self, event) -> None:
        self._stop_spinner()
        spec = _PHASES.get(event.phase, {})

        if event.status == "unknown":
            stderr_console.print(f"[bold red]✗[/bold red] {event.message}")
            return
        if event.status == "failed":
            # A message means the core had a more specific reason than "it failed"
            # (an app with no directory), and it replaces the generic line.
            line = event.message or spec.get("failed", "").format(item=event.item)
            if line:
                stderr_console.print(
                    line if event.message is None else f"[bold red]✗[/bold red] {line}"
                )
            elif event.phase == "recache":
                stderr_console.print(
                    "[yellow]Warning:[/yellow] Failed to recache project. "
                    "Site detection may be inaccurate."
                )
            return
        if line := spec.get("ok"):
            # The noisy per-item success lines are verbose-only, exactly as before;
            # the quiet run says nothing and lets the summary speak.
            if self.verbose or event.phase in (
                "frappe_reset",
                "clear_cache",
                "clear_website_cache",
                "clear_locks",
            ):
                console.print(line.format(item=event.item))


def _report_summary(report: UpdateReport) -> None:
    """Print the summary of every phase.

    Print-only, and the caller owns the exit (which reads ``report.ok``): this is
    also called from the renderer while an exception is unwinding
    (``UpdateAborted``), where raising would replace the in-flight error and lose it.
    Reporting used to sit after a try/finally, where a raise jumped clean over it and
    a stuck site lost its remediation line; now the report is a returned value and
    cannot be skipped at all.
    """
    successful_apps = len(report.apps) - len(report.failed_apps) - len(report.unknown_apps)

    if successful_apps > 0 and not report.aborted:
        console.print(f"\n[bold green]✓ Successfully updated {successful_apps} app(s)[/bold green]")

    if report.ok:
        return

    console.print("\n[bold red]Update completed with errors:[/bold red]")

    if report.aborted:
        # No per-site record exists for the sites the fan-out never reached, so say
        # honestly that it stopped early rather than implying it finished.
        console.print(
            "[bold red]✗[/bold red] Update stopped before completion - "
            "any remaining site was not migrated"
        )

    if report.failed_apps:
        console.print(f"[bold red]✗ Failed to update {len(report.failed_apps)} app(s):[/bold red]")
        for app in report.failed_apps:
            console.print(f"  • {app}: Git pull failed")

    if report.failed_maintenance_enable:
        console.print(
            f"[bold red]✗[/bold red] Could not enable maintenance mode for "
            f"{len(report.failed_maintenance_enable)} site(s), so they were not migrated:"
        )
        for site in report.failed_maintenance_enable:
            console.print(f"  • {site}: could not enter maintenance mode - not migrated")

    if report.failed_migrations:
        console.print(
            f"[bold red]✗ Failed to migrate {len(report.failed_migrations)} site(s):[/bold red]"
        )
        for site in report.failed_migrations:
            console.print(f"  • {site}: Migration failed")

    if report.failed_builds:
        console.print(
            f"[bold red]✗ Failed to build assets for {len(report.failed_builds)} app(s):[/bold red]"
        )
        for app in report.failed_builds:
            console.print(f"  • {app}: Build failed")

    if report.failed_cache_clears:
        console.print(
            f"[bold red]✗ Failed to clear cache for {len(report.failed_cache_clears)} site(s):[/bold red]"
        )
        for site in report.failed_cache_clears:
            console.print(f"  • {site}: Cache clearing failed")

    if report.failed_website_cache_clears:
        console.print(
            f"[bold red]✗ Failed to clear website cache for "
            f"{len(report.failed_website_cache_clears)} site(s):[/bold red]"
        )
        for site in report.failed_website_cache_clears:
            console.print(f"  • {site}: Website cache clearing failed")

    # Unknown is reported APART from failed, and after it, because the two need
    # different actions: a failure can be retried, while something that may still be
    # running must be checked first. Retrying a live migration is harmful.
    _report_unknown(report)

    if report.failed_maintenance_disable:
        console.print(
            f"[bold red]✗ Could not disable maintenance mode for "
            f"{len(report.failed_maintenance_disable)} site(s):[/bold red]"
        )
        for site in report.failed_maintenance_disable:
            console.print(
                f"  • {site}: still in maintenance mode - run "
                f"'bench --site {site} set-maintenance-mode off'"
            )


def _report_unknown(report: UpdateReport) -> None:
    """Report every step whose outcome could not be established."""
    unknown = (
        ("app(s)", report.unknown_apps, "its update may still be running"),
        ("site(s)", report.unknown_migrations, "its migration may still be running"),
        ("app(s)", report.unknown_builds, "its build may still be running"),
    )
    for noun, items, what in unknown:
        if not items:
            continue
        console.print(
            f"[bold red]✗ Lost track of {len(items)} {noun} - outcome UNKNOWN:[/bold red]"
        )
        for item in items:
            console.print(f"  • {item}: {what}; check before retrying")


def run_app_update(
    project_name: str,
    apps: list[str] | None,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    verbose: bool = False,
    clear_cache: bool = False,
    clear_website_cache: bool = False,
    build: bool = False,
    skip_maintenance: bool = False,
    no_recache: bool = False,
    yes: bool = False,
    sites: list[str] | None = None,
    json_output: bool = False,
):
    """Shared app-update entry point behind both ``cwcli apps update`` and the
    deprecated ``cwcli update``.

    Kept as one implementation so the two commands never drift. The interactive
    prologue (auto-start, bench resolution, the auto-inspect fallback) runs here,
    BEFORE any spinner, matching ``commands/backup.py``'s shipped pattern: a prompt
    painted over by a spinner can never receive input.

    ``json_output`` emits the report as ONE JSON document and nothing else on
    stdout: no banner, no renderer (the core's events are dropped), no summary.
    """
    from .utils import ensure_containers_running

    if not apps:
        stderr_console.print("[bold red]Error:[/bold red] At least one app must be specified.")
        raise typer.Exit(code=1)

    if not json_output:
        console.print(f"[bold cyan]Updating project: {project_name}[/bold cyan]\n")

    # Ensure containers are running, prompting if not (auto-start with --yes). This
    # prologue is stderr-only, so it is safe in JSON mode.
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    bench_path = _resolve_bench_path(
        project_name, bench, bench_path, verbose, json_output=json_output
    )

    if verbose and not json_output:
        console.print(
            f"[bold cyan]Updating {len(apps)} app(s) for project '{project_name}'[/bold cyan]\n"
        )

    # None is the drain-and-discard consumption mode: in JSON mode the core's
    # events - bench output included - are dropped rather than rendered, so nothing
    # can corrupt the document.
    renderer = None if json_output else _Renderer(verbose)
    try:
        result = core_update.update(
            project_name,
            list(apps),
            bench_path=bench_path,
            sites=list(sites) if sites else None,
            clear_cache=clear_cache,
            clear_website_cache=clear_website_cache,
            build=build,
            skip_maintenance=skip_maintenance,
            no_recache=no_recache,
            auto_start=yes,
            on_event=renderer,
        )
    except CwcliError as e:
        if renderer is not None:
            renderer._stop_spinner()
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        if e.hint:
            stderr_console.print(f"[dim]{e.hint}[/dim]")
        # USAGE keeps exit 1 here (not axi's 2): --site matching nothing has always
        # exited 1 on this surface, and a migration does not get to change that.
        raise typer.Exit(code=1) from e

    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        # The prologue already resolved the bench and started the containers, so a
        # choice here means that resolution raced. Report rather than guess.
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        raise typer.Exit(code=1)

    report = result.data
    assert report is not None  # OK/WARNING always carries a report

    if json_output:
        typer.echo(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        _report_summary(report)

    # The exit code reads report.ok, NOT result.status: a partial failure is a
    # WARNING-shaped envelope, and every other verb maps WARNING to 0.
    if not report.ok:
        raise typer.Exit(code=1)


def _resolve_bench_path(project_name, bench, bench_path, verbose, *, json_output=False) -> str:
    """Resolve the bench, auto-running ``inspect`` once if nothing is cached.

    The auto-inspect stays in the FRONTEND: it drives the ``inspect`` COMMAND, which
    is un-migrated, and hoisting it here keeps that dependency out of the core (which
    already has to reach back for the mid-fan-out recache, and should not do so
    twice). ``core.update`` still falls back to the default path with a warning when
    it is handed nothing, which is what the agent surface gets.

    In JSON mode the auto-inspect is SKIPPED, because ``inspect``'s own rich output
    goes to stdout and would corrupt the one-document contract. The fallback is then
    the same one ``apps``'s other subcommands already take, and the same one `axi`
    gets: the default path, with the core carrying a warning.
    """
    from .utils import resolve_bench_path as cli_resolve_bench_path

    resolved = cli_resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {resolved}[/dim]")
        return resolved

    if json_output:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] No cached bench path found. Using default: "
            f"{core_update.resolvers.DEFAULT_BENCH_PATH}"
        )
        return core_update.resolvers.DEFAULT_BENCH_PATH

    stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")
    try:
        from .inspect import inspect as inspect_cmd_func

        inspect_cmd_func(
            project_name=project_name,
            verbose=verbose,
            json_output=False,
            update=False,
            no_refresh=False,
            show_apps=False,
            interactive=False,
            yes=False,
        )
        resolved = cli_resolve_bench_path(project_name, bench, None, verbose=verbose)
        if resolved:
            if verbose:
                stderr_console.print(f"[dim]Using cached bench path from inspect: {resolved}[/dim]")
            return resolved
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not detect bench path. "
            f"Using default: {core_update.resolvers.DEFAULT_BENCH_PATH}"
        )
    except typer.Exit:
        raise
    except Exception as e:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Inspect failed. Using default bench path: "
            f"{core_update.resolvers.DEFAULT_BENCH_PATH}"
        )
        if verbose:
            stderr_console.print(f"[dim]Inspect error: {e}[/dim]")
    return core_update.resolvers.DEFAULT_BENCH_PATH


@handle_docker_errors
def update(
    project_name: str = typer.Argument(
        ..., help="The name of the project to update.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Option(
        None,
        "--app",
        "-a",
        help="App name(s) to update. Specify multiple app names after --app or use --app multiple times.",
        autocompletion=complete_app_names,
    ),
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
    sites: list[str] = typer.Option(
        None,
        "--site",
        help="Narrow migration to the named site(s). Repeatable. Omit to migrate all affected sites.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose output with streaming command output."
    ),
    clear_cache: bool = typer.Option(
        False, "--clear-cache", "-c", help="Clear cache for all affected sites after migration."
    ),
    clear_website_cache: bool = typer.Option(
        False,
        "--clear-website-cache",
        "-w",
        help="Clear website cache for all affected sites after migration.",
    ),
    build: bool = typer.Option(False, "--build", "-b", help="Build assets after updating apps."),
    skip_maintenance: bool = typer.Option(
        False,
        "--skip-maintenance",
        help="Skip enabling maintenance mode for affected sites during update.",
    ),
    no_recache: bool = typer.Option(
        False,
        "--no-recache",
        help="Skip re-caching project after app updates (uses existing cache for site detection).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
):
    """
    [DEPRECATED] Update apps - use 'cwcli apps update' instead.

    Kept as a working alias for 'cwcli apps update'. This command will:
    1. Navigate to each app directory and run 'git pull' (or 'bench update --reset' for frappe)
    2. Find all sites where the app is installed
    3. Enable maintenance mode for affected sites (unless --skip-maintenance is used)
    4. Run 'bench --site <site> migrate' for each site that entered maintenance mode
       (a site that could not be put into maintenance is skipped and reported, and
       the command exits non-zero)
    5. Optionally clear cache and/or website cache for each migrated site
    6. Optionally rebuild assets with 'bench build'
    7. Disable maintenance mode for affected sites after completion (unless --skip-maintenance is used)

    Example:
        cwcli update my-project --app erpnext custom_app
        cwcli update my-project --app erpnext --clear-cache --clear-website-cache --build
        cwcli update my-project --app erpnext --skip-maintenance
    """
    stderr_console.print(
        "[yellow]Warning:[/yellow] 'cwcli update' is deprecated; use "
        "[green]cwcli apps update[/green] instead."
    )
    run_app_update(
        project_name,
        apps,
        bench=bench,
        bench_path=bench_path,
        verbose=verbose,
        clear_cache=clear_cache,
        clear_website_cache=clear_website_cache,
        build=build,
        skip_maintenance=skip_maintenance,
        no_recache=no_recache,
        yes=yes,
        sites=sites,
    )
