"""``cwcli apps`` - first-class Frappe app management (the renderer).

A cohesive command group for listing, installing, uninstalling, updating, and
checking out a ref into Frappe apps per bench and (multi-site by default) per
site. It replaces the raw
``cwcli run <project> bench ...`` escape hatch with per-bench/per-site addressing,
``--json`` output, honest aggregated exit codes, and the project's non-interactive
contract (non-TTY-without-flag refuses; auto-start gated by ``--yes``; destructive
uninstall gated by ``confirm_or_exit``/``--yes``).

Since batch 5 (openspec `migrate-apps-core`) this module is a THIN RENDERER: the
resolution, container I/O and fan-out live in :mod:`core.apps`, which returns a
typed report. What stays here is exactly what a renderer owns - the ``rich``/JSON
output, the interactive prompt, the exit codes, and two epilogues that are
frontend concerns by design:

- **the ``ensure_containers_running`` prologue**, which performs the real start
  the core only ever REPORTS as ``start_requested``, and
- **the post-mutation ``cache.recache_project``**, which is deliberately NOT in the
  core: ``core/update.py`` reaches back into the ``inspect`` command only because
  its recache runs mid-fan-out; these recache as an epilogue gated on a condition
  already in the returned report, so hoisting it here keeps that reach the ONE
  place it is.

Output discipline for ``--json``: stdout carries ONLY the final JSON document. All
progress/errors go to stderr, and bench command output is buffered (not streamed)
in JSON mode so it can never corrupt the JSON on stdout. The core emits both as
events and this module picks the consumption mode - that choice is rendering.
"""

import json
import sys

import typer

from ..core import apps as core_apps
from ..core.apps import AppsAnnounce, AppsCommand, AppsOutput, AppsStepEnd
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils import cache
from ..utils.completion_utils import complete_app_names, complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .update import run_app_update
from .utils import confirm_or_exit, ensure_containers_running, resolve_bench_path

app = typer.Typer(help="Manage Frappe apps: list, install, uninstall, update, checkout.")

_DEFAULT_BENCH = "/workspace/frappe-bench"

_ANNOUNCE = {
    "get-app": lambda app_name, site: f"[bold cyan]Fetching[/bold cyan] {app_name}...",
    "install-app": lambda app_name, site: (
        f"[bold cyan]Installing[/bold cyan] {app_name} on [magenta]{site}[/magenta]..."
    ),
    "uninstall-app": lambda app_name, site: (
        f"[bold cyan]Uninstalling[/bold cyan] {app_name} from [magenta]{site}[/magenta]..."
    ),
}


# --------------------------------------------------------------------------- helpers


def _resolve_bench(project_name, bench, bench_path, verbose):
    """Resolve the target bench path (``--bench``/``--path``, else the default).

    Falls back to the historical default only when there is no cache to resolve
    against (matching ``run``). A multi-bench project with no selector raises
    ``Exit(1)`` inside ``resolve_bench_path`` (``on_ambiguous="error"``).

    Kept as a frontend PRE-RESOLVE (the `backup`/`update` pattern) rather than
    letting the core resolve it: the CLI wrapper renders the bench list and today's
    exact exit codes for the ambiguous and not-found forks. The core still resolves
    on its own for `axi`, which renders those forks its own way.
    """
    return resolve_bench_path(project_name, bench, bench_path, verbose=verbose) or _DEFAULT_BENCH


def _exit_on_exec_error(e: CwcliError):
    """Render a core failure the way ``run.py`` does, then exit non-zero.

    Always to stderr, so ``--json``'s stdout-purity contract holds even on this
    path.
    """
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.hint:
        stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1) from e


def _make_renderer(*, json_output, verbose):
    """Build the ``on_event`` callback: the core's events, rendered.

    This is the exec-stream contract's consumption-mode choice, and it belongs
    here: human mode renders each chunk to stdout live, ``--json`` mode buffers so
    stdout stays the document alone and echoes only a FAILED step's output to
    stderr for debuggability.
    """
    buffered: list[str] = []

    def on_event(event):
        if isinstance(event, AppsAnnounce):
            stderr_console.print(_ANNOUNCE[event.phase](event.app, event.site))
        elif isinstance(event, AppsCommand):
            if verbose:
                stderr_console.print(f"[dim]$ {event.command}[/dim]")
            buffered.clear()
        elif isinstance(event, AppsOutput):
            if json_output:
                buffered.append(event.text)
            else:
                # Both tags go to stdout, reproducing the combined stream this used
                # to get from a non-demuxed exec.
                sys.stdout.write(event.text)
                sys.stdout.flush()
        elif isinstance(event, AppsStepEnd):
            if json_output and not event.ok and "".join(buffered).strip():
                stderr_console.print("".join(buffered).rstrip())

    return on_event


def _refresh_cache(project_name, verbose):
    """Refresh the cache through the existing writer so where/open/inspect stop lying.

    Uses ``cache.recache_project`` (a full inspect that persists via
    ``cache_project_data`` -> ``_redact_config_for_cache``), never a direct write.
    Degrades to a warning; the mutation itself already succeeded.
    """
    if not cache.recache_project(project_name, verbose=verbose):
        stderr_console.print(
            "[yellow]Warning:[/yellow] app mutation succeeded but refreshing the cache failed; "
            "run 'cwcli inspect --update' to refresh."
        )


def _report_and_exit(report, json_output, *, success_msg):
    """Emit per-(app, site) results and exit non-zero if ANY step failed.

    The exit code reads ``report.ok``, NOT the envelope's status: a partial fan-out
    failure is a ``WARNING``-shaped envelope, and every other verb maps ``WARNING``
    to exit 0. Copying that pattern here would report success for an uninstall that
    half failed - on the exact surface this command exists to make honest.
    """
    results = [
        {"app": r.app, "site": r.site, "action": r.action, "ok": r.ok} for r in report.results
    ]
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "project": report.project,
                    "bench": report.bench_path,
                    "results": results,
                    "ok": report.ok,
                },
                indent=2,
            )
        )
    else:
        for r in results:
            mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
            where = f" on [magenta]{r['site']}[/magenta]" if r.get("site") else ""
            stderr_console.print(f"  {mark} {r['action']} [cyan]{r['app']}[/cyan]{where}")
        if not report.ok:
            stderr_console.print("[bold red]Completed with errors.[/bold red]")
        else:
            console.print(f"[bold green]✓[/bold green] {success_msg}")
    if not report.ok:
        raise typer.Exit(code=1)


# ----------------------------------------------------------------------------- list


@app.command("list")
@handle_docker_errors
def list_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="List installed apps for the named site(s). Repeatable."
    ),
    installed: bool = typer.Option(
        False, "--installed", "-i", help="Also show apps installed per site (all sites by default)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """List apps available in a bench, and (with --site/--installed) installed per site."""
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)

    try:
        result = core_apps.list_apps(
            project_name,
            bench_path=resolved,
            sites=sites,
            installed=installed,
            on_event=_make_renderer(json_output=json_output, verbose=verbose),
        )
    except CwcliError as e:
        _exit_on_exec_error(e)

    listing = result.data
    assert listing is not None  # OK/WARNING always carries a listing

    if json_output:
        # The historical shape: `installed` appears ONLY when it was asked for, so
        # its absence stays distinguishable from an empty result.
        doc: dict[str, object] = {
            "project": listing.project,
            "bench": listing.bench_path,
            "available_apps": listing.available,
        }
        if installed or sites:
            doc["installed"] = listing.installed
        typer.echo(json.dumps(doc, indent=2))
        if not listing.ok:
            raise typer.Exit(code=1)
        return

    console.print(f"[bold]Available apps[/bold] ([dim]{listing.bench_path}[/dim]):")
    for a in listing.available:
        console.print(f"  • [cyan]{a}[/cyan]")
    if not listing.available:
        console.print("  [dim](none)[/dim]")
    if installed or sites:
        for site, site_apps in listing.installed.items():
            console.print(f"\n[bold]Installed on[/bold] [magenta]{site}[/magenta]:")
            if site_apps is None:
                stderr_console.print("  [red]could not read installed apps[/red]")
            else:
                for a in site_apps:
                    console.print(f"  • [cyan]{a}[/cyan]")
    if not listing.ok:
        raise typer.Exit(code=1)


# -------------------------------------------------------------------------- install


@app.command("install")
@handle_docker_errors
def install_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Argument(..., help="App name(s) or git URL(s) to fetch and install."),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="Install on the named site(s). Repeatable. Omit to install on all."
    ),
    branch: str = typer.Option(None, "--branch", help="Git branch to fetch (passed to get-app)."),
    fetch_only: bool = typer.Option(
        False,
        "--fetch-only",
        help="Fetch the app(s) into the bench without installing on any site.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Fetch (bench get-app) and install app(s) on the target site(s).

    Each app is a known app name OR a git URL (passed straight to bench get-app).
    Multi-site by default: with no --site the app is installed on every site.
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)

    try:
        result = core_apps.install_apps(
            project_name,
            apps,
            bench_path=resolved,
            sites=sites,
            branch=branch,
            fetch_only=fetch_only,
            on_event=_make_renderer(json_output=json_output, verbose=verbose),
        )
    except CwcliError as e:
        _exit_on_exec_error(e)

    report = result.data
    assert report is not None  # OK/WARNING always carries a report
    for warning in result.warnings:
        if warning.code == "sites.none":
            stderr_console.print(f"[yellow]Note:[/yellow] {warning.text}")

    # Refresh the cache if any step succeeded: a fetch changes available apps, an
    # install changes a site's installed apps - both make the cache stale.
    if any(r.ok for r in report.results):
        _refresh_cache(project_name, verbose)

    # The banner must match what actually happened: only claim "installed" when an
    # install-app step ran (not for --fetch-only or a bench with no sites).
    installed = any(r.action == "install-app" for r in report.results)
    _report_and_exit(
        report,
        json_output,
        success_msg="App(s) installed." if installed else "App(s) fetched.",
    )


# ------------------------------------------------------------------------ uninstall


@app.command("uninstall")
@handle_docker_errors
def uninstall_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Argument(
        ..., help="App name(s) to uninstall.", autocompletion=complete_app_names
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="Uninstall from the named site(s). Repeatable. Omit for all sites."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the destructive confirmation and auto-start containers."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Uninstall app(s) from the target site(s) (destructive).

    Multi-site by default: with no --site the app is removed from every site. This
    destroys site data, so it is gated by --yes / an interactive confirmation.
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)
    on_event = _make_renderer(json_output=json_output, verbose=verbose)

    def _uninstall(consent):
        try:
            return core_apps.uninstall_apps(
                project_name,
                apps,
                bench_path=resolved,
                sites=sites,
                consent=consent,
                on_event=on_event,
            )
        except CwcliError as e:
            _exit_on_exec_error(e)

    # `--yes` is two consents fused into one flag ("skip the destructive
    # confirmation AND auto-start containers"); the core models them separately, so
    # the destructive half is passed here and the auto-start half rode the
    # ensure_containers_running prologue above. The CLI flag keeps its exact
    # historical meaning - a migration does not change the contract.
    result = _uninstall(yes)

    # Nothing to uninstall from is a clean no-op, decided before the gate.
    for warning in result.warnings:
        if warning.code == "sites.none":
            stderr_console.print(f"[yellow]Note:[/yellow] {warning.text}")
            raise typer.Exit(code=0)

    if result.status is Status.NEEDS_CHOICE:
        # Destructive gate. --json implies non-interactive: without --yes it refuses,
        # never prompts (a prompt would fight the JSON-only-on-stdout contract).
        if json_output:
            stderr_console.print(
                "[bold red]Error:[/bold red] 'apps uninstall --json' is destructive; pass --yes."
            )
            raise typer.Exit(code=1)
        confirm_or_exit(
            result.choice.prompt,
            assume_yes=yes,
            refuse_message=(
                "Uninstall is destructive and no confirmation was given. Pass --yes to proceed."
            ),
        )
        result = _uninstall(True)

    report = result.data
    assert report is not None  # OK/WARNING always carries a report
    if any(r.ok for r in report.results):
        _refresh_cache(project_name, verbose)

    _report_and_exit(report, json_output, success_msg="App(s) uninstalled.")


# --------------------------------------------------------------------------- checkout


@app.command("checkout")
@handle_docker_errors
def checkout_app(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    app: str = typer.Argument(
        ..., help="The app whose in-instance checkout to update.", autocompletion=complete_app_names
    ),
    ref: str = typer.Argument(..., help="Branch, tag, or commit to fetch and check out."),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    reset: bool = typer.Option(
        False,
        "--reset",
        help="Discard uncommitted changes and hard-reset the working tree to the fetched ref (required to check out a dirty app).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Fetch and check out a branch/ref into an app already installed in the instance.

    Unlike 'apps install' (a fresh get-app clone) and 'apps update' (the tracked
    upstream on every app), this puts a specific feature branch, tag, or commit
    under test in the EXISTING apps/<app> checkout, authenticated for private repos
    through the same credential bridge as install/update. Use --reset to force a
    clean working tree at the fetched ref.
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)

    if not json_output:
        stderr_console.print(
            f"[bold cyan]Checking out[/bold cyan] [cyan]{ref}[/cyan] into [cyan]{app}[/cyan]..."
        )

    try:
        result = core_apps.checkout_app(
            project_name,
            app,
            ref,
            bench_path=resolved,
            reset=reset,
            on_event=_make_renderer(json_output=json_output, verbose=verbose),
        )
    except CwcliError as e:
        _exit_on_exec_error(e)

    report = result.data
    assert report is not None  # OK/WARNING always carries a report

    # A checkout changes the app's git state (and possibly its reported version), so
    # refresh the cache whenever any git step ran, matching install/update.
    if any(r.ok for r in report.results):
        _refresh_cache(project_name, verbose)

    _report_and_exit(report, json_output, success_msg=f"Checked out {ref} into {app}.")


# ----------------------------------------------------------------------------- update


@app.command("update")
@handle_docker_errors
def update_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Argument(
        None,
        help="App name(s) to update. Use 'frappe' to update the framework.",
        autocompletion=complete_app_names,
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="Narrow migration to the named site(s). Repeatable."
    ),
    clear_cache: bool = typer.Option(
        False, "--clear-cache", "-c", help="Clear cache for affected sites after migration."
    ),
    clear_website_cache: bool = typer.Option(
        False, "--clear-website-cache", "-w", help="Clear website cache for affected sites."
    ),
    build: bool = typer.Option(False, "--build", "-b", help="Build assets after updating apps."),
    skip_maintenance: bool = typer.Option(
        False, "--skip-maintenance", help="Skip maintenance mode during update."
    ),
    no_recache: bool = typer.Option(
        False, "--no-recache", help="Skip re-caching after app updates."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Update app(s) and migrate affected sites (the canonical update path).

    Updating 'frappe' runs 'bench update --reset'. This is what the deprecated
    'cwcli update' now delegates to.
    """
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
        json_output=json_output,
    )
