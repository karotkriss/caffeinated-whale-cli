import shlex

import typer

from ..utils import config_utils, db_utils
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running


@handle_docker_errors
def backup(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(
        None,
        "--site",
        "-s",
        help="Site name to backup. If not provided, uses the default site from common_site_config.",
        autocompletion=complete_site_names,
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to the bench directory inside the container (uses cached path from inspect if not specified).",
    ),
    with_files: bool = typer.Option(
        False,
        "--with-files",
        help="Include public and private files in the backup.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Create a backup of a site's database and optionally files.

    This command runs 'bench backup' for the specified site. By default, it backs up
    only the database. Use --with-files to include public and private files.

    If --site is not provided, the default site from common_site_config.json will be used.

    Examples:
        cwcli backup my-project
        cwcli backup my-project --site example.com
        cwcli backup my-project --with-files
    """
    # Ensure containers are running
    ensure_containers_running(project_name, require_running=True, verbose=verbose)

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

    # Get bench path from cache or use provided path
    if not bench_path:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            bench_path = cached_data["bench_instances"][0]["path"]
            if verbose:
                stderr_console.print(f"[dim]Using cached bench path: {bench_path}[/dim]")
        else:
            # No cache found, use default
            bench_path = "/workspace/frappe-bench"
            stderr_console.print(
                f"[yellow]Warning:[/yellow] No cached bench path found. Using default: {bench_path}"
            )

    # Get default site if not provided
    if not site:
        try:
            default_site = db_utils.get_default_site(project_name, bench_path)
        except typer.Exit:
            raise
        except Exception as e:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to retrieve default site: {e}"
            )
            stderr_console.print(
                f"[dim]Tip: Specify --site explicitly or run 'cwcli inspect {project_name}' first.[/dim]"
            )
            raise typer.Exit(code=1) from e

        if default_site:
            site = default_site
            console.print(f"[dim]Using default site: {site}[/dim]")
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No site specified and no default site found in config."
            )
            stderr_console.print(
                f"[dim]Tip: Run 'cwcli inspect {project_name}' first, or specify --site explicitly.[/dim]"
            )
            raise typer.Exit(code=1)

    # Validate site name to prevent command injection
    if not site or not site.strip():
        stderr_console.print("[bold red]Error:[/bold red] Site name cannot be empty.")
        raise typer.Exit(code=1)

    invalid_chars = [";", "&", "|", "$", "`", "(", ")", "<", ">", "\n", "\r", "\\"]
    if any(char in site for char in invalid_chars):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Invalid site name '{site}'. "
            "Site names cannot contain special shell characters."
        )
        raise typer.Exit(code=1)

    # Validate bench_path
    if any(char in bench_path for char in invalid_chars):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Invalid bench path '{bench_path}'. "
            "Paths cannot contain special shell characters."
        )
        raise typer.Exit(code=1)

    # Verify bench path exists
    bench_sites_path = f"{bench_path}/sites"
    quoted_bench_sites_path = shlex.quote(bench_sites_path)
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {quoted_bench_sites_path}"')
    if exit_code != 0:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Bench directory not found at {bench_path}"
        )
        raise typer.Exit(code=1)

    # Check if site exists
    site_path = f"{bench_path}/sites/{site}"
    quoted_site_path = shlex.quote(site_path)
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {quoted_site_path}"')
    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Site '{site}' not found at {site_path}")
        raise typer.Exit(code=1)

    # Verify backup directory exists (create if it doesn't)
    backup_dir = f"{site_path}/private/backups"
    quoted_backup_dir = shlex.quote(backup_dir)
    test_cmd = f"test -d {quoted_backup_dir}"
    exit_code, _ = frappe_container.exec_run(f"sh -c '{test_cmd}'")
    if exit_code != 0:
        if verbose:
            stderr_console.print(f"[dim]Creating backup directory at {backup_dir}[/dim]")
        # Create backup directory
        mkdir_cmd = f"mkdir -p {quoted_backup_dir}"
        exit_code, output = frappe_container.exec_run(f"sh -c '{mkdir_cmd}'")
        if exit_code != 0:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to create backup directory at {backup_dir}"
            )
            if verbose and output:
                stderr_console.print(output.decode("utf-8", errors="replace"))
            raise typer.Exit(code=1)

    # Build backup command
    cmd = f"bench --site {site} backup"

    # Add --with-files flag if requested
    if with_files:
        cmd += " --with-files"

    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

    # Execute backup with spinner
    show_tips = config_utils.get_show_tips()
    console.print()
    with TipSpinner(
        f"Creating backup for site '{site}'", console=stderr_console, enabled=show_tips
    ):
        exit_code, output = frappe_container.exec_run(cmd, workdir=bench_path)

    # Show output if verbose or on failure
    if verbose or exit_code != 0:
        if output:
            console.print()
            console.print("[dim]Backup output:[/dim]")
            try:
                console.print(output.decode("utf-8"))
            except UnicodeDecodeError:
                # Fallback to replace errors if UTF-8 decoding fails
                console.print(output.decode("utf-8", errors="replace"))

    console.print()
    if exit_code == 0:
        console.print(f"[bold green]✓[/bold green] Successfully created backup for site '{site}'")
        if with_files:
            console.print("[dim]Backup includes database and files[/dim]")
        else:
            console.print("[dim]Backup includes database only[/dim]")
        console.print(f"[dim]Backup location: {bench_path}/sites/{site}/private/backups/[/dim]")
    else:
        stderr_console.print(f"[bold red]✗[/bold red] Failed to create backup for site '{site}'")
        stderr_console.print()
        stderr_console.print("[bold]Common causes:[/bold]")
        stderr_console.print("  • Site not running or database connection issues")
        stderr_console.print("  • Insufficient disk space")
        stderr_console.print("  • Permission issues with backup directory")
        stderr_console.print("  • MariaDB/database service not accessible")
        stderr_console.print()
        stderr_console.print("[dim]Tip: Run with -v flag for detailed error output[/dim]")
        raise typer.Exit(code=1)
