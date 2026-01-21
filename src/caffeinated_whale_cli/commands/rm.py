"""
Remove (delete) a Frappe project and its containers.

This command stops and removes all containers for a project, and optionally
removes associated volumes. Before removal:
1. Re-caches the project to get accurate site information
2. Creates database backups for all sites
3. Archives project configuration to ~/.cwcli/archive
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import questionary
import typer

from ..utils import cache, db_utils
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors

app = typer.Typer(help="Remove a Frappe project and its containers.")


def _backup_sites(
    project_name: str,
    container,
    bench_path: str,
    archive_dir: Path,
    verbose: bool = False,
) -> bool:
    """
    Backup all sites in the bench before removal.

    Args:
        project_name: Name of the project
        container: Docker container to run backup commands in
        bench_path: Path to bench directory inside container
        archive_dir: Directory to save backups to
        verbose: Enable verbose output

    Returns:
        True if backups successful, False otherwise
    """
    try:
        # Get list of sites
        sites_dir = f"{bench_path}/sites"
        exit_code, output = container.exec_run(f"ls -1 {sites_dir}")

        if exit_code != 0:
            if verbose:
                stderr_console.print(
                    f"[dim]VERBOSE: Could not list sites directory: {sites_dir}[/dim]"
                )
            return False

        sites = [
            s.strip()
            for s in output.decode("utf-8").split("\n")
            if s.strip()
            and s.strip() not in ["apps.txt", "assets", "common_site_config.json", "apps.json"]
        ]

        if not sites:
            if verbose:
                stderr_console.print("[dim]VERBOSE: No sites found to backup[/dim]")
            return True

        # Create backups directory
        backups_dir = archive_dir / "backups"
        backups_dir.mkdir(exist_ok=True)

        backed_up_count = 0
        for site in sites:
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Backing up site '{site}'...[/dim]")

            # Run bench backup command for the site
            backup_cmd = f"cd {bench_path} && bench --site {site} backup --with-files"
            exit_code, output = container.exec_run(f"sh -c '{backup_cmd}'", workdir=bench_path)

            if exit_code == 0:
                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Backup created for '{site}'[/dim]")

                # Get the backup files from the site's backups directory
                site_backup_dir = f"{bench_path}/sites/{site}/private/backups"

                # List all backup files
                exit_code, ls_output = container.exec_run(f"ls -1t {site_backup_dir}")

                if exit_code == 0:
                    backup_files = [
                        f.strip() for f in ls_output.decode("utf-8").split("\n") if f.strip()
                    ]

                    # Copy the most recent backups
                    site_archive_backups = backups_dir / site
                    site_archive_backups.mkdir(exist_ok=True)

                    # Get the 3 most recent files (database, files, site config)
                    for backup_file in backup_files[:5]:  # Get top 5 to ensure we get all parts
                        source_path = f"{site_backup_dir}/{backup_file}"

                        # Use docker cp to copy the file out
                        exit_code, file_data = container.exec_run(f"cat {source_path}")

                        if exit_code == 0:
                            dest_file = site_archive_backups / backup_file
                            dest_file.write_bytes(file_data)

                            if verbose:
                                stderr_console.print(
                                    f"[dim]VERBOSE: Copied {backup_file} to archive[/dim]"
                                )

                backed_up_count += 1
            else:
                stderr_console.print(f"[yellow]Warning:[/yellow] Could not backup site '{site}'")
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Backup command output: {output.decode('utf-8')}[/dim]"
                    )

        if backed_up_count > 0:
            console.print(f"  [dim]Backed up {backed_up_count} site(s) to {backups_dir}[/dim]")
            return True
        else:
            return False

    except Exception as e:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Backup failed: {e}[/dim]")
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not backup sites for '{project_name}'"
        )
        return False


def _archive_project_config(
    project_name: str,
    container,
    bench_path: str,
    verbose: bool = False,
) -> bool:
    """
    Archive project configuration files to ~/.cwcli/archive before removal.

    Archives:
    - docker-compose.yml (from container)
    - site_config.json files (from all sites in the bench)

    Args:
        project_name: Name of the project
        container: Docker container to extract files from
        bench_path: Path to bench directory inside container
        verbose: Enable verbose output

    Returns:
        True if archive successful, False otherwise
    """
    try:
        # Create archive directory
        archive_base = Path.home() / ".cwcli" / "archive"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = archive_base / f"{project_name}_{timestamp}"
        archive_dir.mkdir(parents=True, exist_ok=True)

        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Archiving to {archive_dir}[/dim]")

        # Archive docker-compose.yml from container's working directory
        # Try common locations
        compose_locations = [
            "/workspace/frappe-bench/docker-compose.yml",
            f"{bench_path}/docker-compose.yml",
            "/docker-compose.yml",
        ]

        compose_archived = False
        for compose_path in compose_locations:
            exit_code, output = container.exec_run(f"cat {compose_path}")
            if exit_code == 0:
                compose_file = archive_dir / "docker-compose.yml"
                compose_file.write_bytes(output)
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Archived {compose_path} to {compose_file}[/dim]"
                    )
                compose_archived = True
                break

        if not compose_archived and verbose:
            stderr_console.print("[dim]VERBOSE: Could not find docker-compose.yml to archive[/dim]")

        # Archive site_config.json files from all sites
        sites_dir = f"{bench_path}/sites"
        exit_code, output = container.exec_run(f"ls -1 {sites_dir}")

        if exit_code == 0:
            sites = [
                s.strip()
                for s in output.decode("utf-8").split("\n")
                if s.strip()
                and s.strip() not in ["apps.txt", "assets", "common_site_config.json", "apps.json"]
            ]

            configs_archived = 0
            for site in sites:
                site_config_path = f"{sites_dir}/{site}/site_config.json"
                exit_code, output = container.exec_run(f"cat {site_config_path}")

                if exit_code == 0:
                    # Create site directory in archive
                    site_archive_dir = archive_dir / site
                    site_archive_dir.mkdir(exist_ok=True)

                    # Save site_config.json
                    config_file = site_archive_dir / "site_config.json"
                    config_file.write_bytes(output)
                    configs_archived += 1

                    if verbose:
                        stderr_console.print(
                            f"[dim]VERBOSE: Archived {site_config_path} to {config_file}[/dim]"
                        )

            if configs_archived > 0:
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Archived {configs_archived} site config(s)[/dim]"
                    )

        # Create archive metadata
        metadata = {
            "project_name": project_name,
            "archived_at": datetime.now().isoformat(),
            "bench_path": bench_path,
        }

        metadata_file = archive_dir / "archive_metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2))

        console.print(f"  [dim]Configuration archived to {archive_dir}[/dim]")
        return True

    except Exception as e:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Archive failed: {e}[/dim]")
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not archive configuration for '{project_name}'"
        )
        return False


@handle_docker_errors
def _remove_project(
    project_name: str,
    remove_volumes: bool = False,
    no_backup: bool = False,
    verbose: bool = False,
    status=None,
):
    """
    The core logic for removing a single project's containers.

    Args:
        project_name: Name of the project to remove
        remove_volumes: Whether to remove volumes as well
        no_backup: Skip database backups
        verbose: Enable verbose output
        status: Status context for spinner

    Returns:
        Number of containers removed, or 0 if project not found
    """
    containers = get_project_containers(project_name)

    if not containers:
        stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        return 0

    if verbose:
        stderr_console.print(
            f"[dim]VERBOSE: Found {len(containers)} container(s) for '{project_name}'[/dim]"
        )

    # Find frappe container for archiving and backup
    frappe_container = None
    for container in containers:
        if container.labels.get("com.docker.compose.service") == "frappe":
            frappe_container = container
            break

    # Backup and archive before removal
    if frappe_container:
        # Try to get bench path from cache first
        bench_path = "/workspace/frappe-bench"  # default
        try:
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                bench_path = cached_data["bench_instances"][0]["path"]
        except Exception:
            pass

        # Create archive directory
        archive_base = Path.home() / ".cwcli" / "archive"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = archive_base / f"{project_name}_{timestamp}"
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Backup databases (unless --no-backup)
        if not no_backup:
            if status:
                status.update(
                    f"[bold cyan]Backing up databases for '{project_name}'...[/bold cyan]"
                )
            _backup_sites(project_name, frappe_container, bench_path, archive_dir, verbose=verbose)

        # Archive configuration
        if status:
            status.update(f"[bold cyan]Archiving configuration for '{project_name}'...[/bold cyan]")
        _archive_project_config(project_name, frappe_container, bench_path, verbose=verbose)

    # Stop and remove each container
    removed_count = 0
    for container in containers:
        try:
            container_name = container.name
            container_status = container.status

            if verbose:
                stderr_console.print(
                    f"[dim]VERBOSE: Processing container '{container_name}' (status: {container_status})[/dim]"
                )

            # Stop if running
            if container_status == "running":
                if status:
                    status.update(f"[bold yellow]Stopping '{container_name}'...[/bold yellow]")
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Stopping container '{container_name}'[/dim]"
                    )
                container.stop()

            # Remove container
            if status:
                status.update(f"[bold red]Removing '{container_name}'...[/bold red]")
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Removing container '{container_name}'[/dim]")
            container.remove(v=remove_volumes, force=True)
            removed_count += 1

        except Exception as e:
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to remove container '{container_name}': {e}"
            )
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Exception: {e}[/dim]")

    # Clear cache for removed project
    if removed_count > 0:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Clearing cache for '{project_name}'[/dim]")
        db_utils.clear_cache_for_project(project_name)

    return removed_count


@app.callback(invoke_without_command=True)
def rm(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
    volumes: bool = typer.Option(
        True,
        "--volumes/--no-volumes",
        help="Remove associated volumes (default: True). Use --no-volumes to keep volumes.",
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip database backups before removal (not recommended).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt and proceed with removal.",
    ),
    project_name: list[str] = typer.Argument(
        None,
        help="The name(s) of the Frappe project(s) to remove. Can be piped from stdin.",
        autocompletion=complete_project_names,
    ),
):
    """
    Remove (delete) Frappe project containers and volumes.

    WARNING: This action is destructive and cannot be undone!

    By default, this command:
    - Re-caches project to get accurate site information
    - Creates database backups for all sites
    - Archives docker-compose.yml and site_config.json files
    - Stops all containers for the project
    - Removes all containers for the project
    - Removes all associated Docker volumes (deletes all data!)
    - Clears the project from the cache

    Use --no-volumes to keep volumes:
    - Keeps Docker volumes (preserves data)
    - Only removes containers

    Use --no-backup to skip backups (not recommended):
    - Skips database backups
    - Skips recaching (faster but risky)

    Examples:
        cwcli rm my-project                 # Remove everything (with confirmation)
        cwcli rm my-project --no-volumes    # Keep volumes, remove containers only
        cwcli rm my-project --no-backup     # Skip backups (not recommended)
        cwcli rm my-project --yes           # Skip confirmation
        cwcli ls | cwcli rm                 # Remove multiple projects via pipe
    """
    project_names_to_process = []

    # Handle -v or --verbose in remaining args
    actual_verbose = verbose
    filtered_project_names = []

    if project_name:
        for name in project_name:
            if name in ("-v", "--verbose"):
                actual_verbose = True
            else:
                filtered_project_names.append(name)
        project_names_to_process.extend(filtered_project_names)

    # Handle piped input
    if not sys.stdin.isatty():
        piped_input = [line.strip() for line in sys.stdin]
        project_names_to_process.extend([name for name in piped_input if name])

    if not project_names_to_process:
        stderr_console.print(
            "[bold red]Error:[/bold red] Please provide at least one project name or pipe a list of names."
        )
        raise typer.Exit(code=1)

    # Re-cache projects if not skipping backups
    if not no_backup:
        console.print()
        console.print("[bold cyan]Preparing for removal...[/bold cyan]")
        for project in project_names_to_process:
            with stderr_console.status(
                f"Re-caching project '{project}'...", spinner="dots"
            ) as status:
                if actual_verbose:
                    stderr_console.print(f"[dim]VERBOSE: Re-caching '{project}'...[/dim]")

                if not cache.recache_project(project, verbose=actual_verbose):
                    stderr_console.print(
                        f"[yellow]Warning:[/yellow] Could not recache '{project}'. Backup may be incomplete."
                    )

    # Confirmation prompt (unless --yes flag is used)
    if not yes:
        console.print()
        console.print(
            "[bold red]WARNING:[/bold red] You are about to permanently remove the following project(s):"
        )
        for name in project_names_to_process:
            console.print(f"  • [bold]{name}[/bold]")
        console.print()

        if volumes:
            console.print(
                "[bold red]VOLUMES WILL BE DELETED:[/bold red] [bold yellow]ALL DATA WILL BE PERMANENTLY LOST![/bold yellow]"
            )
            console.print(
                "[dim]This includes databases, sites, files, and all other data in Docker volumes.[/dim]"
            )
        else:
            console.print(
                "[bold yellow]Note:[/bold yellow] Volumes will be preserved (--no-volumes flag set)."
            )
            console.print(
                "[dim]You can remove volumes later if needed or recreate containers from existing data.[/dim]"
            )
        console.print()

        try:
            confirm = questionary.confirm(
                "Are you sure you want to proceed?",
                default=False,
                auto_enter=False,
            ).ask()

            if not confirm:
                console.print("[yellow]Operation cancelled.[/yellow]")
                raise typer.Exit(code=0)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Operation cancelled.[/yellow]")
            raise typer.Exit(code=0) from None

    # Proceed with removal
    console.print()
    console.print(f"Removing [bold red]{len(project_names_to_process)}[/bold red] project(s)...")
    if not volumes:
        console.print("[bold yellow]Note:[/bold yellow] Preserving volumes (--no-volumes flag)")
    else:
        console.print("[bold red]Deleting all volumes and data![/bold red]")

    total_removed = 0
    for name in project_names_to_process:
        with stderr_console.status(
            f"[bold red]Removing '{name}'...[/bold red]", spinner="dots"
        ) as status:
            result = _remove_project(
                name,
                remove_volumes=volumes,
                no_backup=no_backup,
                verbose=actual_verbose,
                status=status,
            )

        # Print results outside spinner context
        if result > 0:
            console.print(
                f"[bold green]✓[/bold green] Project '{name}' removed ({result} container(s))"
            )
            if not volumes:
                console.print(f"  [dim]Volumes preserved for '{name}'[/dim]")
            total_removed += result
        # If result is 0, error message was already printed

    console.print()
    if total_removed > 0:
        console.print(
            f"[bold green]✓[/bold green] Successfully removed {total_removed} container(s)"
        )
    else:
        console.print("[bold yellow]No containers were removed.[/bold yellow]")
