import sys
import io
import time
import typer
import docker
from typing import List
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.live import Live
from rich.console import Group
from rich.text import Text

from .utils import get_project_containers
from ..utils.docker_utils import handle_docker_errors
from ..utils import db_utils
from ..utils.console import console, stderr_console


def _stream_command(
    container: docker.models.containers.Container,
    cmd: str,
    workdir: str,
    verbose: bool = False,
    status_msg: str = None
) -> int:
    """Execute command and optionally stream output in real-time."""
    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

        # Show status message if provided
        if status_msg:
            with stderr_console.status(f"[bold green]{status_msg}[/bold green]", spinner="dots"):
                # Give the spinner a moment to render
                import time
                time.sleep(0.1)

        # Stream output in verbose mode
        api = container.client.api
        exec_id = api.exec_create(container.id, cmd, workdir=workdir, tty=False)["Id"]

        for chunk in api.exec_start(exec_id, stream=True, demux=False):
            if isinstance(chunk, (bytes, bytearray)):
                # Write directly to stdout to preserve carriage returns and progress bars
                sys.stdout.write(chunk.decode("utf-8"))
                sys.stdout.flush()
            else:
                sys.stdout.write(str(chunk))
                sys.stdout.flush()

        # Wait for the command to fully complete
        result = api.exec_inspect(exec_id)
        exit_code = result.get("ExitCode")

        # If ExitCode is None, the command is still running - wait for it
        while exit_code is None:
            time.sleep(0.1)
            result = api.exec_inspect(exec_id)
            exit_code = result.get("ExitCode")

        return exit_code if exit_code is not None else 1
    else:
        # Non-verbose mode: just run the command without streaming
        # demux=False ensures we wait for the command to fully complete
        exit_code, _ = container.exec_run(cmd, workdir=workdir, demux=False)
        return exit_code


def _run_command_quiet(
    container: docker.models.containers.Container,
    cmd: str,
    workdir: str,
    verbose: bool = False
) -> tuple[int, str]:
    """Execute command and return exit code and output."""
    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

    exit_code, output = container.exec_run(cmd, workdir=workdir)
    stdout = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return exit_code, stdout


def _get_sites_with_app(
    container: docker.models.containers.Container,
    bench_path: str,
    app_name: str,
    verbose: bool = False
) -> List[str]:
    """Get list of sites that have the specified app installed."""
    # Get all sites from inspect cache or live query
    cmd = f"ls -1 {bench_path}/sites"
    exit_code, output = _run_command_quiet(container, cmd, bench_path, verbose)

    if exit_code != 0:
        return []

    excluded = {"apps.txt", "assets", "common_site_config.json", "example.com", "apps.json"}
    all_sites = [item.strip() for item in output.split("\n") if item.strip() and item.strip() not in excluded]

    # Check which sites have this app installed
    sites_with_app = []
    for site in all_sites:
        cmd = f"bench --site {site} list-apps"
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


def _update_project(
    project_name: str,
    apps: List[str],
    bench_path: str = None,
    verbose: bool = False,
    clear_cache: bool = False,
    clear_website_cache: bool = False,
    build: bool = False
):
    """Core logic for updating a single project."""

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

    if frappe_container.status != "running":
        stderr_console.print(
            f"[bold red]Error:[/bold red] Frappe container for project '{project_name}' is not running."
        )
        raise typer.Exit(code=1)

    # Get bench path from cache or use provided path
    if not bench_path:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            bench_path = cached_data["bench_instances"][0]["path"]
            if verbose:
                stderr_console.print(f"[dim]Using cached bench path: {bench_path}[/dim]")
        else:
            # No cache found, run inspect automatically
            stderr_console.print(f"[yellow]No cached bench path found. Running inspect...[/yellow]")

            try:
                # Import and run inspect to populate cache
                from .inspect import inspect as inspect_cmd_func

                inspect_cmd_func(
                    project_name=project_name,
                    verbose=verbose,
                    json_output=False,
                    update=False,
                    show_apps=False,
                    interactive=False
                )

                # Try to get cached data again
                cached_data = db_utils.get_cached_project_data(project_name)
                if cached_data and cached_data.get("bench_instances"):
                    bench_path = cached_data["bench_instances"][0]["path"]
                    if verbose:
                        stderr_console.print(f"[dim]Using cached bench path from inspect: {bench_path}[/dim]")
                else:
                    # Still no cache, use default
                    bench_path = "/workspace/frappe-bench"
                    stderr_console.print(
                        f"[yellow]Warning:[/yellow] Could not detect bench path. Using default: {bench_path}"
                    )
            except Exception as e:
                # Inspect failed, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning:[/yellow] Inspect failed. Using default bench path: {bench_path}"
                )
                if verbose:
                    stderr_console.print(f"[dim]Inspect error: {e}[/dim]")

    # Verify bench path exists
    exit_code, output = frappe_container.exec_run(
        f'sh -c "test -d {bench_path}/apps && test -d {bench_path}/sites"'
    )
    if verbose:
        stderr_console.print(f'[dim]$ sh -c "test -d {bench_path}/apps && test -d {bench_path}/sites"[/dim]')
        stderr_console.print(f"[dim]Exit code: {exit_code}[/dim]")

    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Bench directory not found at {bench_path}")
        stderr_console.print(f"[dim]Make sure the bench path is correct. Current path: {bench_path}[/dim]")
        raise typer.Exit(code=1)

    # Track all sites that need migration
    all_affected_sites = set()
    failed_apps = []
    failed_migrations = []
    failed_builds = []
    failed_cache_clears = []
    failed_website_cache_clears = []

    if verbose:
        # VERBOSE MODE - Show output directly
        console.print(f"[bold cyan]Updating {len(apps)} app(s) for project '{project_name}'[/bold cyan]\n")

        # Update each app
        for app in apps:
            app_path = f"{bench_path}/apps/{app}"

            # Check if app exists
            exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {app_path}"')
            if verbose:
                stderr_console.print(f'[dim]$ sh -c "test -d {app_path}"[/dim]')
                stderr_console.print(f"[dim]Exit code: {exit_code}[/dim]")
            if exit_code != 0:
                stderr_console.print(f"[bold red]✗[/bold red] App '{app}' not found at {app_path}")
                failed_apps.append(app)
                continue

            # Git pull
            console.print(f"[bold green]→[/bold green] Updating app: [cyan]{app}[/cyan]")
            exit_code = _stream_command(
                frappe_container,
                "git pull",
                app_path,
                verbose=True,
                status_msg=f"Pulling latest changes for '{app}'..."
            )

            if exit_code != 0:
                stderr_console.print(f"[bold red]✗[/bold red] Failed to update app '{app}'")
                failed_apps.append(app)
                continue

            console.print(f"[bold green]✓[/bold green] Successfully updated '{app}'")

            # Find sites with this app installed
            console.print(f"[dim]Finding sites with '{app}' installed...[/dim]")
            sites = _get_sites_with_app(frappe_container, bench_path, app, verbose)
            if sites:
                console.print(f"  [dim]Found {len(sites)} site(s) with '{app}' installed[/dim]")
                all_affected_sites.update(sites)
            else:
                console.print(f"  [dim]No sites found with '{app}' installed[/dim]")

        # Report failed apps
        if failed_apps:
            console.print(f"\n[bold yellow]Warning:[/bold yellow] Failed to update {len(failed_apps)} app(s):")
            for app in failed_apps:
                console.print(f"  [red]✗ {app}[/red]")

        # Migrate affected sites
        if all_affected_sites:
            console.print(f"[bold cyan]Migrating {len(all_affected_sites)} affected site(s)[/bold cyan]\n")

            for i, site in enumerate(sorted(all_affected_sites), 1):
                console.print(f"\n[bold]Migrating site {i}/{len(all_affected_sites)}: {site}[/bold]")
                cmd = f"bench --site {site} migrate"
                exit_code = _stream_command(
                    frappe_container,
                    cmd,
                    bench_path,
                    verbose=True,
                    status_msg=f"Migrating {site}..."
                )

                if exit_code != 0:
                    failed_migrations.append(site)
                    stderr_console.print(f"[bold red]✗[/bold red] Migration failed for site '{site}'")
                else:
                    console.print(f"[bold green]✓[/bold green] Migration completed for '{site}'")

            console.print(f"[bold green]✓[/bold green] Migration complete for all affected sites\n")
        else:
            console.print(f"[dim]No sites require migration[/dim]\n")

        # Build assets if requested (before clearing cache)
        if build:
            successful_updated_apps = [app for app in apps if app not in failed_apps]

            if successful_updated_apps:
                console.print(f"[bold cyan]Building assets for {len(successful_updated_apps)} app(s)[/bold cyan]")

                for app in successful_updated_apps:
                    cmd = f"bench build --app {app}"
                    console.print(f"[bold green]→[/bold green] Building app: [cyan]{app}[/cyan]")
                    exit_code = _stream_command(
                        frappe_container,
                        cmd,
                        bench_path,
                        verbose=True,
                        status_msg=f"Building {app}..."
                    )

                    if exit_code == 0:
                        console.print(f"[bold green]✓[/bold green] Assets built successfully for '{app}'")
                    else:
                        failed_builds.append(app)
                        stderr_console.print(f"[bold red]✗[/bold red] Failed to build assets for '{app}'")

        # Clear cache if requested (after build)
        if clear_cache and all_affected_sites:
            console.print(f"\n[bold cyan]Clearing cache for {len(all_affected_sites)} site(s)[/bold cyan]")

            for site in sorted(all_affected_sites):
                cmd = f"bench --site {site} clear-cache"
                stderr_console.print(f"[dim]$ {cmd}[/dim]")

                with console.status(f"[bold green]Clearing cache for '{site}'...[/bold green]", spinner="dots"):
                    exit_code, _ = frappe_container.exec_run(cmd, workdir=bench_path)

                if exit_code == 0:
                    console.print(f"[bold green]✓[/bold green] Cache cleared for '{site}'")
                else:
                    failed_cache_clears.append(site)
                    stderr_console.print(f"[bold red]✗[/bold red] Failed to clear cache for '{site}'")

        # Clear website cache if requested (after build)
        if clear_website_cache and all_affected_sites:
            console.print(f"\n[bold cyan]Clearing website cache for {len(all_affected_sites)} site(s)[/bold cyan]")

            for site in sorted(all_affected_sites):
                cmd = f"bench --site {site} clear-website-cache"
                stderr_console.print(f"[dim]$ {cmd}[/dim]")

                with console.status(f"[bold green]Clearing website cache for '{site}'...[/bold green]", spinner="dots"):
                    exit_code, _ = frappe_container.exec_run(cmd, workdir=bench_path)

                if exit_code == 0:
                    console.print(f"[bold green]✓[/bold green] Website cache cleared for '{site}'")
                else:
                    failed_website_cache_clears.append(site)
                    stderr_console.print(f"[bold red]✗[/bold red] Failed to clear website cache for '{site}'")

        # Clear locks for all affected sites
        if all_affected_sites:
            console.print(f"\n[bold cyan]Clearing locks for {len(all_affected_sites)} site(s)[/bold cyan]")

            for site in sorted(all_affected_sites):
                locks_path = f"{bench_path}/sites/{site}/locks"
                cmd = f"rm -rf {locks_path}"
                stderr_console.print(f"[dim]$ {cmd}[/dim]")

                with console.status(f"[bold green]Clearing locks for '{site}'...[/bold green]", spinner="dots"):
                    exit_code, _ = frappe_container.exec_run(cmd, workdir=bench_path)

                if exit_code == 0:
                    console.print(f"[bold green]✓[/bold green] Locks cleared for '{site}'")

    else:
        # NON-VERBOSE MODE - Use stacked progress bars with spinner at bottom
        from rich.spinner import Spinner
        from rich.table import Table

        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        )

        # First, quickly determine affected sites count
        temp_affected_sites = set()
        for app in apps:
            app_path = f"{bench_path}/apps/{app}"
            # Check if app exists
            exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {app_path}"')
            if exit_code == 0:
                # Find sites with this app installed
                sites = _get_sites_with_app(frappe_container, bench_path, app, verbose=False)
                if sites:
                    temp_affected_sites.update(sites)

        total_sites = len(temp_affected_sites)

        # Create only the overall progress bar
        overall_task = progress.add_task("[bold white]Overall", total=100)

        # Create spinner that will stay at the bottom
        spinner = Spinner("dots", text="[bold green]Working...[/bold green]")

        # Combine progress bars and spinner using Group
        progress_group = Group(progress, spinner)

        with Live(progress_group, console=console, refresh_per_second=20, transient=True) as live:

            # Calculate total steps for overall progress
            total_steps = len(apps)  # Git pulls
            total_steps += total_sites  # Migrations
            if build:
                total_steps += len(apps)  # Builds
            if clear_cache:
                total_steps += total_sites  # Cache clears
            if clear_website_cache:
                total_steps += total_sites  # Website cache clears
            completed_steps = 0

            # Step 1: Pull apps
            for i, app in enumerate(apps, 1):
                app_path = f"{bench_path}/apps/{app}"
                spinner.update(text=f"[bold green]Pulling app: {app} ({i}/{len(apps)})[/bold green]")
                live.refresh()

                # Check if app exists
                exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {app_path}"')
                if exit_code != 0:
                    failed_apps.append(app)
                    completed_steps += 1
                    progress.update(overall_task, completed=int((completed_steps / total_steps) * 100))
                    live.refresh()
                    continue

                # Git pull
                exit_code = _stream_command(frappe_container, "git pull", app_path, verbose=False)

                if exit_code != 0:
                    failed_apps.append(app)
                else:
                    # Find sites with this app installed
                    sites = _get_sites_with_app(frappe_container, bench_path, app, verbose=False)
                    if sites:
                        all_affected_sites.update(sites)

                completed_steps += 1
                progress.update(overall_task, completed=int((completed_steps / total_steps) * 100))
                live.refresh()

            # Step 2: Migrate sites
            if all_affected_sites:
                sorted_sites = sorted(all_affected_sites)
                for i, site in enumerate(sorted_sites, 1):
                    spinner.update(text=f"[bold green]Migrating site: {site} ({i}/{len(sorted_sites)})[/bold green]")
                    live.refresh()
                    cmd = f"bench --site {site} migrate"
                    exit_code = _stream_command(frappe_container, cmd, bench_path, verbose=False)

                    if exit_code != 0:
                        failed_migrations.append(site)

                    # Small delay to ensure migration fully completes and releases locks
                    time.sleep(0.5)

                    completed_steps += 1
                    progress.update(overall_task, completed=int((completed_steps / total_steps) * 100))
                    live.refresh()

            # Step 3: Build assets (if requested)
            if build:
                successful_updated_apps = [app for app in apps if app not in failed_apps]

                for i, app in enumerate(successful_updated_apps, 1):
                    spinner.update(text=f"[bold green]Building app: {app} ({i}/{len(successful_updated_apps)})[/bold green]")
                    live.refresh()
                    cmd = f"bench build --app {app}"
                    exit_code = _stream_command(frappe_container, cmd, bench_path, verbose=False)

                    if exit_code != 0:
                        failed_builds.append(app)

                    completed_steps += 1
                    progress.update(overall_task, completed=int((completed_steps / total_steps) * 100))
                    live.refresh()

            # Step 4: Clear cache (if requested)
            if clear_cache and all_affected_sites:
                sorted_sites = sorted(all_affected_sites)
                for i, site in enumerate(sorted_sites, 1):
                    spinner.update(text=f"[bold green]Clearing cache: {site} ({i}/{len(sorted_sites)})[/bold green]")
                    live.refresh()
                    cmd = f"bench --site {site} clear-cache"
                    exit_code, _ = frappe_container.exec_run(cmd, workdir=bench_path)

                    if exit_code != 0:
                        failed_cache_clears.append(site)

                    completed_steps += 1
                    progress.update(overall_task, completed=int((completed_steps / total_steps) * 100))
                    live.refresh()

            # Step 5: Clear website cache (if requested)
            if clear_website_cache and all_affected_sites:
                sorted_sites = sorted(all_affected_sites)
                for i, site in enumerate(sorted_sites, 1):
                    spinner.update(text=f"[bold green]Clearing website cache: {site} ({i}/{len(sorted_sites)})[/bold green]")
                    live.refresh()
                    cmd = f"bench --site {site} clear-website-cache"
                    exit_code, _ = frappe_container.exec_run(cmd, workdir=bench_path)

                    if exit_code != 0:
                        failed_website_cache_clears.append(site)

                    completed_steps += 1
                    progress.update(overall_task, completed=int((completed_steps / total_steps) * 100))
                    live.refresh()

            # Step 6: Clear locks for all affected sites
            if all_affected_sites:
                sorted_sites = sorted(all_affected_sites)
                for i, site in enumerate(sorted_sites, 1):
                    spinner.update(text=f"[bold green]Clearing locks: {site} ({i}/{len(sorted_sites)})[/bold green]")
                    live.refresh()
                    locks_path = f"{bench_path}/sites/{site}/locks"
                    cmd = f"rm -rf {locks_path}"
                    frappe_container.exec_run(cmd, workdir=bench_path)

            # Finalize overall progress
            progress.update(overall_task, completed=100)
            spinner.update(text="[bold green]Complete![/bold green]")
            live.refresh()

        # Show errors after progress bars
        if failed_apps:
            console.print(f"\n[bold yellow]Warning:[/bold yellow] Failed to update {len(failed_apps)} app(s):")
            for app in failed_apps:
                stderr_console.print(f"  [red]✗ {app}[/red]")

        if failed_migrations:
            for site in failed_migrations:
                stderr_console.print(f"[bold red]✗[/bold red] Migration failed for site '{site}'")

        if failed_builds:
            for app in failed_builds:
                stderr_console.print(f"[bold red]✗[/bold red] Failed to build assets for '{app}'")

        if failed_cache_clears:
            for site in failed_cache_clears:
                stderr_console.print(f"[bold red]✗[/bold red] Failed to clear cache for '{site}'")

        if failed_website_cache_clears:
            for site in failed_website_cache_clears:
                stderr_console.print(f"[bold red]✗[/bold red] Failed to clear website cache for '{site}'")

    # Summary and error reporting
    successful_apps = len(apps) - len(failed_apps)
    has_errors = bool(failed_apps or failed_migrations or failed_builds or failed_cache_clears or failed_website_cache_clears)

    if successful_apps > 0:
        console.print(f"\n[bold green]✓ Successfully updated {successful_apps} app(s)[/bold green]")

    # Detailed error reporting
    if has_errors:
        console.print(f"\n[bold red]Update completed with errors:[/bold red]")

        if failed_apps:
            console.print(f"[bold red]✗ Failed to update {len(failed_apps)} app(s):[/bold red]")
            for app in failed_apps:
                console.print(f"  • {app}: Git pull failed")

        if failed_migrations:
            console.print(f"[bold red]✗ Failed to migrate {len(failed_migrations)} site(s):[/bold red]")
            for site in failed_migrations:
                console.print(f"  • {site}: Migration failed")

        if failed_builds:
            console.print(f"[bold red]✗ Failed to build assets for {len(failed_builds)} app(s):[/bold red]")
            for app in failed_builds:
                console.print(f"  • {app}: Build failed")

        if failed_cache_clears:
            console.print(f"[bold red]✗ Failed to clear cache for {len(failed_cache_clears)} site(s):[/bold red]")
            for site in failed_cache_clears:
                console.print(f"  • {site}: Cache clearing failed")

        if failed_website_cache_clears:
            console.print(f"[bold red]✗ Failed to clear website cache for {len(failed_website_cache_clears)} site(s):[/bold red]")
            for site in failed_website_cache_clears:
                console.print(f"  • {site}: Website cache clearing failed")

        # Return failure status
        raise typer.Exit(code=1)


@handle_docker_errors
def update(
    project_name: str = typer.Argument(
        ...,
        help="The name of the project to update."
    ),
    apps: List[str] = typer.Option(
        None,
        "--app",
        "-a",
        help="App name(s) to update. Specify multiple app names after --app or use --app multiple times."
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to the bench directory inside the container (uses cached path from inspect if not specified)."
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output with streaming command output."
    ),
    clear_cache: bool = typer.Option(
        False,
        "--clear-cache",
        "-c",
        help="Clear cache for all affected sites after migration."
    ),
    clear_website_cache: bool = typer.Option(
        False,
        "--clear-website-cache",
        "-w",
        help="Clear website cache for all affected sites after migration."
    ),
    build: bool = typer.Option(
        False,
        "--build",
        "-b",
        help="Build assets after updating apps."
    ),
):
    """
    Update specified apps and migrate all sites where they are installed.

    This command will:
    1. Navigate to each app directory and run 'git pull'
    2. Find all sites where the app is installed
    3. Run 'bench --site <site> migrate' for each affected site
    4. Optionally clear cache and/or website cache
    5. Optionally rebuild assets with 'bench build'

    Example:
        cwcli update my-project --app erpnext custom_app
        cwcli update my-project --app erpnext --clear-cache --clear-website-cache --build
    """
    if not apps:
        stderr_console.print("[bold red]Error:[/bold red] At least one --app must be specified.")
        raise typer.Exit(code=1)

    console.print(f"[bold cyan]Updating project: {project_name}[/bold cyan]\n")
    _update_project(project_name, list(apps), bench_path, verbose, clear_cache, clear_website_cache, build)
