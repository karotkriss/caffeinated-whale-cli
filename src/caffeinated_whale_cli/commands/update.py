import shlex
import sys
import time

import docker
import typer

from ..core.exec_stream import ExecChunk, exec_stream
from ..utils import cache, db_utils
from ..utils.completion_utils import complete_app_names, complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import (
    get_project_containers,
    handle_docker_errors,
)


def _stream_command(
    container: docker.models.containers.Container,
    cmd: str,
    workdir: str,
    verbose: bool = False,
    status_msg: str | None = None,
) -> int:
    """Execute command and optionally stream output in real-time.

    One exec, two consumption modes. `verbose` decides whether to RENDER the
    events, not how to obtain them: the verbose/non-verbose fork used to reach
    all the way down to two different exec mechanisms, but rendering was always a
    presentation concern. Non-verbose still prints nothing, because it runs
    inside a `console.status` spinner that streaming would shred.
    """
    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

        # Show status message if provided
        if status_msg:
            with stderr_console.status(f"[bold green]{status_msg}[/bold green]", spinner="dots"):
                # Give the spinner a moment to render
                time.sleep(0.1)

    exit_code = 1
    for event in exec_stream(container, cmd, workdir=workdir):
        if isinstance(event, ExecChunk):
            if verbose:
                # Write directly to stdout to preserve carriage returns and progress bars
                sys.stdout.write(event.text)
                sys.stdout.flush()
        else:
            exit_code = event.exit_code
    return exit_code


def _run_command_quiet(
    container: docker.models.containers.Container, cmd: str, workdir: str, verbose: bool = False
) -> tuple[int, str]:
    """Execute command and return exit code and output."""
    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

    exit_code, output = container.exec_run(cmd, workdir=workdir)
    stdout = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return exit_code, stdout


def _set_maintenance_mode(
    container: docker.models.containers.Container,
    bench_path: str,
    sites: list[str],
    enable: bool = True,
    verbose: bool = False,
) -> dict[str, bool]:
    """
    Set maintenance mode for specified sites.
    Returns a dict mapping site names to success status.
    """
    mode_str = "on" if enable else "off"
    action = "enabling" if enable else "disabling"
    results = {}

    for site in sites:
        cmd = f"bench --site {shlex.quote(site)} set-maintenance-mode {mode_str}"

        if verbose:
            stderr_console.print(f"[dim]$ {cmd}[/dim]")

        exit_code, _ = container.exec_run(cmd, workdir=bench_path)
        success = exit_code == 0
        results[site] = success

        if verbose:
            if success:
                stderr_console.print(f"[dim]✓ {action} maintenance mode for {site}[/dim]")
            else:
                stderr_console.print(f"[dim]✗ Failed to set maintenance mode for {site}[/dim]")

    return results


def _get_sites_with_app(
    project_name: str,
    bench_path: str,
    app_name: str,
    container: docker.models.containers.Container = None,
    verbose: bool = False,
) -> list[str]:
    """
    Get list of sites that have the specified app installed.
    Uses cached data from db_utils if available, falls back to live query.
    """
    # Try to get cached project data first
    cached_data = db_utils.get_cached_project_data(project_name)
    if cached_data:
        sites_with_app = []
        for bench_data in cached_data.get("bench_instances", []):
            # Only check sites in the relevant bench
            if bench_data.get("path") == bench_path:
                for site_data in bench_data.get("sites", []):
                    if app_name in site_data.get("installed_apps", []):
                        sites_with_app.append(site_data.get("name"))
        if sites_with_app:
            if verbose:
                stderr_console.print(
                    f"[dim]Found {len(sites_with_app)} site(s) with '{app_name}' from cache[/dim]"
                )
            return sites_with_app

    # Fall back to live query if cache is not available
    if not container:
        return []

    if verbose:
        stderr_console.print(f"[dim]Cache miss for {project_name}. Running live query...[/dim]")

    # Get all sites from container
    cmd = f"ls -1 {shlex.quote(bench_path)}/sites"
    exit_code, output = _run_command_quiet(container, cmd, bench_path, verbose)

    if exit_code != 0:
        return []

    excluded = {"apps.txt", "assets", "common_site_config.json", "example.com", "apps.json"}
    all_sites = [
        item.strip() for item in output.split("\n") if item.strip() and item.strip() not in excluded
    ]

    # Check which sites have this app installed
    sites_with_app = []
    for site in all_sites:
        cmd = f"bench --site {shlex.quote(site)} list-apps"
        exit_code, output = _run_command_quiet(container, cmd, bench_path, verbose)
        if exit_code == 0:
            # Parse app names - bench list-apps returns lines like "frappe 15.80.0 version-15"
            # We only need the first word (the app name)
            installed_apps = []
            for line in output.split("\n"):
                if line.strip():
                    # Get the first word from each line
                    app = line.strip().split()[0]
                    installed_apps.append(app)

            if app_name in installed_apps:
                sites_with_app.append(site)

    return sites_with_app


def _apply_site_filter(sites, sites_filter: list[str] | None):
    """Narrow a set/list of sites to those named in ``sites_filter`` (``--site``).

    ``--site`` narrows which of the affected sites get migrated; ``None``/empty
    means "no narrowing" (the historical all-affected-sites behavior).
    """
    if not sites_filter:
        return set(sites)
    allowed = set(sites_filter)
    return {s for s in sites if s in allowed}


def _fail_if_site_filter_matched_nothing(
    unfiltered_sites, filtered_sites, sites_filter: list[str] | None
) -> None:
    """Refuse when ``--site`` narrows an actually-affected set down to empty.

    Distinguishes "genuinely nothing to migrate" (no site has the app
    installed, where exiting 0 is correct) from "--site named a site the
    app isn't actually on" (a typo/mismatch, which must not silently
    succeed as if the update ran).
    """
    if sites_filter and unfiltered_sites and not filtered_sites:
        stderr_console.print(
            "[bold red]Error:[/bold red] --site matched no affected site(s). "
            f"Requested: {', '.join(sorted(sites_filter))}; "
            f"affected: {', '.join(sorted(unfiltered_sites))}"
        )
        raise typer.Exit(code=1)


def _run_frappe_update_reset(
    frappe_container: docker.models.containers.Container,
    bench_path: str,
    project_name: str,
    no_recache: bool,
    verbose: bool,
) -> None:
    """Update the frappe framework via ``bench update --reset`` (whole-bench).

    The framework app is not updated with a per-app ``git pull``; the correct path
    is bench's own ``bench update --reset``, which resets every app's repo, pulls,
    migrates every site, and rebuilds. Named for the ``frappe`` app specifically.
    """
    console.print(
        "[bold cyan]Updating the frappe framework with 'bench update --reset'[/bold cyan]\n"
    )
    exit_code = _stream_command(
        frappe_container,
        "bench update --reset",
        bench_path,
        verbose=True,
        status_msg="Running bench update --reset...",
    )
    if not no_recache:
        if not cache.recache_project(project_name, verbose=verbose) and verbose:
            stderr_console.print(
                "[yellow]Warning:[/yellow] Failed to recache project after frappe update."
            )
    if exit_code != 0:
        stderr_console.print("[bold red]✗[/bold red] 'bench update --reset' failed")
        raise typer.Exit(code=1)
    console.print("[bold green]✓[/bold green] Frappe framework updated")


def _dir_exists(container: docker.models.containers.Container, path: str) -> bool:
    """True if ``path`` is a directory inside the container (shell-safe).

    The command is passed in LIST form (``["sh", "-c", script]``) so docker-py
    execs it directly instead of re-``shlex.split``ting a ``sh -c "..."`` string;
    that keeps ``shlex.quote(path)`` robust even when ``path`` contains a single
    quote (a raw ``--path``/``--app`` would otherwise break the outer quoting).
    """
    exit_code, _ = container.exec_run(["sh", "-c", f"test -d {shlex.quote(path)}"])
    return bool(exit_code == 0)


def _pull_apps(container, bench_path, apps, failed_apps, verbose):
    """git pull each app exactly once, recording failures.

    Streams output in verbose mode; otherwise shows a per-app status spinner.
    Shared by both presentations so the pull happens only once per run.
    """
    for i, app in enumerate(apps, 1):
        app_path = f"{bench_path}/apps/{app}"

        if not _dir_exists(container, app_path):
            stderr_console.print(f"[bold red]✗[/bold red] App '{app}' not found at {app_path}")
            failed_apps.append(app)
            continue

        if verbose:
            console.print(f"[bold green]→[/bold green] Updating app: [cyan]{app}[/cyan]")
            exit_code = _stream_command(
                container,
                "git pull",
                app_path,
                verbose=True,
                status_msg=f"Pulling latest changes for '{app}'...",
            )
        else:
            with console.status(
                f"[bold green]Pulling app: {app} ({i}/{len(apps)})[/bold green]", spinner="dots"
            ):
                exit_code = _stream_command(container, "git pull", app_path, verbose=False)

        if exit_code != 0:
            stderr_console.print(f"[bold red]✗[/bold red] Failed to update app '{app}'")
            failed_apps.append(app)
        elif verbose:
            console.print(f"[bold green]✓[/bold green] Successfully updated '{app}'")


def _recache_after_pull(project_name, apps, failed_apps, no_recache, verbose):
    """Re-cache the project after pulls so site/app detection is accurate."""
    if not no_recache and apps and len(failed_apps) < len(apps):
        console.print("\n[dim]Re-caching project to ensure accurate site data...[/dim]")
        if verbose:
            stderr_console.print("[dim]Running inspect to refresh cache...[/dim]")
        if not cache.recache_project(project_name, verbose=verbose):
            if verbose:
                stderr_console.print(
                    "[yellow]Warning:[/yellow] Failed to recache project. "
                    "Site detection may be inaccurate."
                )
    elif no_recache and verbose:
        console.print("\n[dim]Skipping recache (--no-recache flag set)...[/dim]")


def _discover_affected_sites(project_name, bench_path, apps, failed_apps, container, verbose):
    """Find every site that has an updated app installed - exactly one pass."""
    affected: set[str] = set()
    for app in apps:
        if app in failed_apps:
            continue
        if verbose:
            console.print(f"\n[dim]Finding sites with '{app}' installed...[/dim]")
        sites = _get_sites_with_app(project_name, bench_path, app, container, verbose)
        if sites:
            if verbose:
                console.print(f"  [dim]Found {len(sites)} site(s) with '{app}' installed[/dim]")
            affected.update(sites)
        elif verbose:
            console.print(f"  [dim]No sites found with '{app}' installed[/dim]")
    return affected


def _report_failed_apps(failed_apps):
    """Warn about apps that could not be pulled."""
    if failed_apps:
        console.print(
            f"\n[bold yellow]Warning:[/bold yellow] Failed to update {len(failed_apps)} app(s):"
        )
        for app in failed_apps:
            console.print(f"  [red]✗ {app}[/red]")


def _enable_maintenance(container, bench_path, sites, maintenance_sites, verbose):
    """Turn maintenance mode ON per site, recording each success in
    ``maintenance_sites`` immediately.

    Recording as each site is enabled (rather than from a bulk return value) means
    that if an exec raises mid-loop, the finally-block still disables exactly the
    sites that were actually put into maintenance - none is left silently stuck.
    """
    for site in sites:
        results = _set_maintenance_mode(container, bench_path, [site], enable=True, verbose=verbose)
        if results.get(site):
            maintenance_sites.add(site)


def _disable_maintenance(container, bench_path, maintenance_sites, failed_disable, verbose):
    """Turn maintenance mode OFF for every site we enabled.

    Any site that cannot be taken back out of maintenance is recorded in
    ``failed_disable`` (warned here and reported by the caller, which exits
    non-zero) so a stuck site is never left silent.
    """
    if verbose:
        console.print("\n[bold cyan]Disabling maintenance mode...[/bold cyan]")
    for site in sorted(maintenance_sites):
        try:
            results = _set_maintenance_mode(
                container, bench_path, [site], enable=False, verbose=verbose
            )
            ok = bool(results.get(site))
        except Exception as e:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to disable maintenance mode for '{site}': {e}"
            )
            ok = False
        if ok:
            if verbose:
                console.print(f"[bold green]✓[/bold green] Maintenance mode disabled for '{site}'")
        else:
            failed_disable.append(site)
            stderr_console.print(f"[bold red]✗[/bold red] Site '{site}' left in maintenance mode")


def _run_migrations_verbose(container, bench_path, sites, failed_migrations):
    """Migrate each site, streaming output (verbose presentation)."""
    if not sites:
        console.print("[dim]No sites require migration[/dim]\n")
        return
    console.print(f"[bold cyan]Migrating {len(sites)} affected site(s)[/bold cyan]\n")
    for i, site in enumerate(sites, 1):
        console.print(f"\n[bold]Migrating site {i}/{len(sites)}: {site}[/bold]")
        cmd = f"bench --site {shlex.quote(site)} migrate"
        exit_code = _stream_command(
            container, cmd, bench_path, verbose=True, status_msg=f"Migrating {site}..."
        )
        if exit_code != 0:
            failed_migrations.append(site)
            stderr_console.print(f"[bold red]✗[/bold red] Migration failed for site '{site}'")
        else:
            console.print(f"[bold green]✓[/bold green] Migration completed for '{site}'")
        # Small delay to ensure migration fully completes and releases locks
        time.sleep(0.5)
    console.print("[bold green]✓[/bold green] Migration complete for all affected sites\n")


def _run_migrations_quiet(container, bench_path, sites, failed_migrations):
    """Migrate each site under a status spinner (non-verbose presentation)."""
    if not sites:
        return
    for i, site in enumerate(sites, 1):
        cmd = f"bench --site {shlex.quote(site)} migrate"
        with console.status(
            f"[bold green]Migrating site: {site} ({i}/{len(sites)})[/bold green]", spinner="dots"
        ):
            exit_code = _stream_command(container, cmd, bench_path, verbose=False)
        if exit_code != 0:
            failed_migrations.append(site)
            stderr_console.print(f"[bold red]✗[/bold red] Migration failed for site '{site}'")
        # Small delay to ensure migration fully completes and releases locks
        time.sleep(0.5)


def _build_apps(container, bench_path, apps, failed_apps, failed_builds, verbose):
    """Rebuild assets for every successfully-pulled app."""
    successful = [app for app in apps if app not in failed_apps]
    if not successful:
        return
    if verbose:
        console.print(f"[bold cyan]Building assets for {len(successful)} app(s)[/bold cyan]")
    for i, app in enumerate(successful, 1):
        cmd = f"bench build --app {shlex.quote(app)}"
        if verbose:
            console.print(f"[bold green]→[/bold green] Building app: [cyan]{app}[/cyan]")
            exit_code = _stream_command(
                container, cmd, bench_path, verbose=True, status_msg=f"Building {app}..."
            )
        else:
            with console.status(
                f"[bold green]Building app: {app} ({i}/{len(successful)})[/bold green]",
                spinner="dots",
            ):
                exit_code = _stream_command(container, cmd, bench_path, verbose=False)
        if exit_code == 0:
            if verbose:
                console.print(f"[bold green]✓[/bold green] Assets built successfully for '{app}'")
        else:
            failed_builds.append(app)
            stderr_console.print(f"[bold red]✗[/bold red] Failed to build assets for '{app}'")


def _clear_site_cache(container, bench_path, sites, bench_subcmd, label, failures, verbose):
    """Run ``bench --site <s> <bench_subcmd>`` for each site (cache/website cache)."""
    console.print(f"\n[bold cyan]Clearing {label} for {len(sites)} site(s)[/bold cyan]")
    for site in sites:
        cmd = f"bench --site {shlex.quote(site)} {bench_subcmd}"
        if verbose:
            stderr_console.print(f"[dim]$ {cmd}[/dim]")
        with console.status(
            f"[bold green]Clearing {label} for '{site}'...[/bold green]", spinner="dots"
        ):
            exit_code, _ = container.exec_run(cmd, workdir=bench_path)
        if exit_code == 0:
            console.print(f"[bold green]✓[/bold green] {label.capitalize()} cleared for '{site}'")
        else:
            failures.append(site)
            stderr_console.print(f"[bold red]✗[/bold red] Failed to clear {label} for '{site}'")


def _clear_locks(container, bench_path, sites, verbose):
    """Remove each affected site's locks directory."""
    console.print(f"\n[bold cyan]Clearing locks for {len(sites)} site(s)[/bold cyan]")
    for site in sites:
        locks_path = f"{bench_path}/sites/{site}/locks"
        cmd = f"rm -rf {shlex.quote(locks_path)}"
        if verbose:
            stderr_console.print(f"[dim]$ {cmd}[/dim]")
        with console.status(
            f"[bold green]Clearing locks for '{site}'...[/bold green]", spinner="dots"
        ):
            exit_code, _ = container.exec_run(cmd, workdir=bench_path)
        if exit_code == 0:
            console.print(f"[bold green]✓[/bold green] Locks cleared for '{site}'")


def _update_project(
    project_name: str,
    apps: list[str],
    bench_path: str | None = None,
    verbose: bool = False,
    clear_cache: bool = False,
    clear_website_cache: bool = False,
    build: bool = False,
    skip_maintenance: bool = False,
    no_recache: bool = False,
    bench_selector: str | None = None,
    yes: bool = False,
    sites_filter: list[str] | None = None,
):
    """Core logic for updating a single project."""
    from .utils import ensure_containers_running, resolve_bench_path

    # Ensure containers are running, prompt user if not (auto-start with --yes)
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    # Get containers
    containers = get_project_containers(project_name)
    if not containers:
        stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        raise typer.Exit(code=1)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No 'frappe' service found for project '{project_name}'."
        )
        raise typer.Exit(code=1)

    # Resolve which bench to update (--bench/--path, else the single bench, else
    # error on multi-bench ambiguity). Returns None only when nothing is cached.
    resolved = resolve_bench_path(project_name, bench_selector, bench_path, verbose=verbose)
    if resolved:
        bench_path = resolved
        if verbose:
            stderr_console.print(f"[dim]Using bench path: {bench_path}[/dim]")
    else:
        # No cache found: run inspect automatically to populate it, then re-resolve.
        stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")
        try:
            # Import and run inspect to populate cache
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

            # Re-resolve now that the cache is populated (same --bench/single/multi
            # rules, so a multi-bench project still errors instead of guessing).
            bench_path = resolve_bench_path(project_name, bench_selector, None, verbose=verbose)
            if bench_path:
                if verbose:
                    stderr_console.print(
                        f"[dim]Using cached bench path from inspect: {bench_path}[/dim]"
                    )
            else:
                # Still no cache, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning:[/yellow] Could not detect bench path. Using default: {bench_path}"
                )
        except typer.Exit:
            raise
        except Exception as e:
            # Inspect failed, use default
            bench_path = "/workspace/frappe-bench"
            stderr_console.print(
                f"[yellow]Warning:[/yellow] Inspect failed. Using default bench path: {bench_path}"
            )
            if verbose:
                stderr_console.print(f"[dim]Inspect error: {e}[/dim]")

    # Verify bench path exists
    bench_path_ok = _dir_exists(frappe_container, f"{bench_path}/apps") and _dir_exists(
        frappe_container, f"{bench_path}/sites"
    )
    if verbose:
        stderr_console.print(f"[dim]Verifying bench directory at {bench_path}[/dim]")

    if not bench_path_ok:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Bench directory not found at {bench_path}"
        )
        stderr_console.print(
            f"[dim]Make sure the bench path is correct. Current path: {bench_path}[/dim]"
        )
        raise typer.Exit(code=1)

    # Frappe framework app special-case: the framework is updated bench-wide via
    # `bench update --reset`, not a per-app `git pull`. If the user names `frappe`
    # (alone or alongside others), run the whole-bench reset+update+migrate+build,
    # which already covers every app and every site, then return. `--site` narrowing
    # does not apply here (bench update is bench-wide).
    if any(app.lower() == "frappe" for app in apps):
        # `bench update --reset` is bench-wide: it migrates and rebuilds everything,
        # so the per-app/per-site options below do not apply. Announce that they are
        # ignored rather than silently dropping them.
        ignored = [
            name
            for name, active in (
                ("--site", bool(sites_filter)),
                ("--clear-cache", clear_cache),
                ("--clear-website-cache", clear_website_cache),
                ("--build", build),
                ("--skip-maintenance", skip_maintenance),
            )
            if active
        ]
        if ignored:
            stderr_console.print(
                "[yellow]Note:[/yellow] updating 'frappe' runs a bench-wide "
                f"'bench update --reset'; ignoring {', '.join(ignored)} (not applicable)."
            )
        _run_frappe_update_reset(frappe_container, bench_path, project_name, no_recache, verbose)
        return

    # Track sites that need migration and per-phase failures.
    all_affected_sites: set[str] = set()
    failed_apps: list[str] = []
    failed_migrations: list[str] = []
    failed_builds: list[str] = []
    failed_cache_clears: list[str] = []
    failed_website_cache_clears: list[str] = []
    failed_maintenance_disable: list[str] = []  # Sites left stuck in maintenance mode
    failed_maintenance_enable: list[str] = []  # Sites never in maintenance, so not migrated
    maintenance_sites: set[str] = set()  # Sites we actually turned maintenance ON for

    try:
        if verbose:
            console.print(
                f"[bold cyan]Updating {len(apps)} app(s) for project '{project_name}'[/bold cyan]\n"
            )

        # --- Shared pull + discovery: ONE git-pull pass and ONE discovery pass,
        # used by both the verbose and non-verbose paths below (b8 fix: the old
        # non-verbose block was mis-bound to `if all_affected_sites` and re-ran
        # both, double-pulling and re-discovering; an empty affected-set now runs
        # neither a second time).
        _pull_apps(frappe_container, bench_path, apps, failed_apps, verbose)
        _recache_after_pull(project_name, apps, failed_apps, no_recache, verbose)
        all_affected_sites = _discover_affected_sites(
            project_name, bench_path, apps, failed_apps, frappe_container, verbose
        )

        # Narrow to the sites named with --site (if any); no --site keeps them all.
        unfiltered_affected_sites = set(all_affected_sites)
        all_affected_sites = _apply_site_filter(all_affected_sites, sites_filter)
        _fail_if_site_filter_matched_nothing(
            unfiltered_affected_sites, all_affected_sites, sites_filter
        )
        _report_failed_apps(failed_apps)

        # Enable maintenance mode for affected sites (unless explicitly skipped),
        # recording each site as it is turned on so cleanup disables exactly those.
        if not skip_maintenance and all_affected_sites:
            console.print("[bold cyan]Enabling maintenance mode...[/bold cyan]")
            _enable_maintenance(
                frappe_container,
                bench_path,
                sorted(all_affected_sites),
                maintenance_sites,
                verbose,
            )
            console.print(
                f"[bold green]✓[/bold green] Maintenance mode enabled for "
                f"{len(maintenance_sites)} site(s)\n"
            )
            failed_maintenance_enable = sorted(all_affected_sites - maintenance_sites)

        # Migrate only the sites actually in maintenance mode (or every affected
        # site when maintenance is skipped) - never migrate a site we could not
        # put into maintenance.
        if skip_maintenance:
            sites_to_migrate = sorted(all_affected_sites)
        else:
            sites_to_migrate = sorted(maintenance_sites)

        # Top-level presentation split, keyed on --verbose (NOT all_affected_sites).
        if verbose:
            _run_migrations_verbose(
                frappe_container, bench_path, sites_to_migrate, failed_migrations
            )
        else:
            _run_migrations_quiet(frappe_container, bench_path, sites_to_migrate, failed_migrations)

        # Build assets if requested (before clearing cache).
        if build:
            _build_apps(frappe_container, bench_path, apps, failed_apps, failed_builds, verbose)

        # Clear caches / locks only for sites actually eligible for migration (after
        # build) - a site that never entered maintenance was not migrated, so it
        # must not be cache/lock-cleared as if the update had actually run there.
        if clear_cache and sites_to_migrate:
            _clear_site_cache(
                frappe_container,
                bench_path,
                sites_to_migrate,
                "clear-cache",
                "cache",
                failed_cache_clears,
                verbose,
            )
        if clear_website_cache and sites_to_migrate:
            _clear_site_cache(
                frappe_container,
                bench_path,
                sites_to_migrate,
                "clear-website-cache",
                "website cache",
                failed_website_cache_clears,
                verbose,
            )
        if sites_to_migrate:
            _clear_locks(frappe_container, bench_path, sites_to_migrate, verbose)

    finally:
        # CRITICAL: always disable maintenance mode for every site we enabled, even
        # if the update failed - and record any site that cannot be taken back out
        # so it is warned and forces a non-zero exit (never silently left stuck).
        if not skip_maintenance and maintenance_sites:
            _disable_maintenance(
                frappe_container,
                bench_path,
                maintenance_sites,
                failed_maintenance_disable,
                verbose,
            )

    # Summary and error reporting
    successful_apps = len(apps) - len(failed_apps)
    has_errors = bool(
        failed_apps
        or failed_migrations
        or failed_builds
        or failed_cache_clears
        or failed_website_cache_clears
        or failed_maintenance_disable
        or failed_maintenance_enable
    )

    if successful_apps > 0:
        console.print(f"\n[bold green]✓ Successfully updated {successful_apps} app(s)[/bold green]")

    # Detailed error reporting
    if has_errors:
        console.print("\n[bold red]Update completed with errors:[/bold red]")

        if failed_apps:
            console.print(f"[bold red]✗ Failed to update {len(failed_apps)} app(s):[/bold red]")
            for app in failed_apps:
                console.print(f"  • {app}: Git pull failed")

        if failed_maintenance_enable:
            console.print(
                f"[bold red]✗[/bold red] Could not enable maintenance mode for "
                f"{len(failed_maintenance_enable)} site(s), so they were not migrated:"
            )
            for site in failed_maintenance_enable:
                console.print(f"  • {site}: could not enter maintenance mode - not migrated")

        if failed_migrations:
            console.print(
                f"[bold red]✗ Failed to migrate {len(failed_migrations)} site(s):[/bold red]"
            )
            for site in failed_migrations:
                console.print(f"  • {site}: Migration failed")

        if failed_builds:
            console.print(
                f"[bold red]✗ Failed to build assets for {len(failed_builds)} app(s):[/bold red]"
            )
            for app in failed_builds:
                console.print(f"  • {app}: Build failed")

        if failed_cache_clears:
            console.print(
                f"[bold red]✗ Failed to clear cache for {len(failed_cache_clears)} site(s):[/bold red]"
            )
            for site in failed_cache_clears:
                console.print(f"  • {site}: Cache clearing failed")

        if failed_website_cache_clears:
            console.print(
                f"[bold red]✗ Failed to clear website cache for {len(failed_website_cache_clears)} site(s):[/bold red]"
            )
            for site in failed_website_cache_clears:
                console.print(f"  • {site}: Website cache clearing failed")

        if failed_maintenance_disable:
            console.print(
                f"[bold red]✗ Could not disable maintenance mode for "
                f"{len(failed_maintenance_disable)} site(s):[/bold red]"
            )
            for site in failed_maintenance_disable:
                console.print(
                    f"  • {site}: still in maintenance mode - run "
                    f"'bench --site {shlex.quote(site)} set-maintenance-mode off'"
                )

        # Return failure status
        raise typer.Exit(code=1)


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
):
    """Shared app-update entry point behind both ``cwcli apps update`` and the
    deprecated ``cwcli update``.

    Validates that at least one app was given, then delegates to
    ``_update_project`` (which owns the frappe special-case and the ``--site``
    narrowing). Kept as one implementation so the two commands never drift.
    """
    if not apps:
        stderr_console.print("[bold red]Error:[/bold red] At least one app must be specified.")
        raise typer.Exit(code=1)

    console.print(f"[bold cyan]Updating project: {project_name}[/bold cyan]\n")
    _update_project(
        project_name,
        list(apps),
        bench_path,
        verbose,
        clear_cache,
        clear_website_cache,
        build,
        skip_maintenance,
        no_recache,
        bench_selector=bench,
        yes=yes,
        sites_filter=list(sites) if sites else None,
    )


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
