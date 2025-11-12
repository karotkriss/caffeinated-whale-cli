import json
import re
from datetime import datetime

import questionary
import typer
from questionary import Style

from ..utils import config_utils, db_utils
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running


def parse_backup_filename(filename: str) -> dict | None:
    """
    Parse Frappe backup filename into components.

    Args:
        filename: Backup filename (e.g., '20251109_225726-site_name-database.sql.gz')

    Returns:
        Dict with parsed components or None if invalid
    """
    # Remove extensions
    name_without_ext = filename
    extensions = []
    while "." in name_without_ext:
        name_without_ext, ext = name_without_ext.rsplit(".", 1)
        extensions.insert(0, ext)

    # Parse main pattern: {timestamp}-{site_name}-{backup_type}
    pattern = r"^(\d{8}_\d{6})-([^-]+)-(.+)$"
    match = re.match(pattern, name_without_ext)

    if not match:
        return None

    timestamp_str, site_name, backup_type = match.groups()

    # Parse timestamp
    try:
        timestamp = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return None

    return {
        "filename": filename,
        "timestamp": timestamp,
        "timestamp_str": timestamp_str,
        "site_name": site_name,  # With underscores (transformed)
        "backup_type": backup_type,
        "extensions": extensions,
        "full_extension": ".".join(extensions),
        "compressed": "gz" in extensions or "tgz" in extensions,
        "is_database": backup_type == "database",
        "is_files": backup_type in ("files", "private-files"),
        "is_config": backup_type == "site_config_backup",
    }


def transform_site_name_to_backup_format(site_name: str) -> str:
    """
    Transform site name to backup filename format (dots -> underscores).

    Args:
        site_name: Original site name (e.g., 'development.localhost')

    Returns:
        Transformed name (e.g., 'development_localhost')
    """
    return site_name.replace(".", "_")


def scan_backups_for_all_sites(frappe_container, bench_path: str, verbose: bool = False) -> list:
    """
    Scan backup directories for all sites in the bench.

    Args:
        frappe_container: Docker container object
        bench_path: Path to bench directory
        verbose: Enable verbose output

    Returns:
        List of parsed backup file dictionaries (grouped by backup set)
    """
    backups = []

    # Get all sites in the bench
    sites_path = f"{bench_path}/sites"
    cmd = f'find {sites_path} -maxdepth 1 -mindepth 1 -type d -not -name "assets" -not -name "common_site_config.json"'

    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

    exit_code, output = frappe_container.exec_run(cmd, workdir=bench_path)

    if exit_code != 0:
        stderr_console.print(f"[yellow]Warning:[/yellow] Failed to list sites in {sites_path}")
        return backups

    sites = output.decode("utf-8").strip().split("\n")
    sites = [s.strip() for s in sites if s.strip()]

    if verbose:
        stderr_console.print(f"[dim]Found {len(sites)} sites[/dim]")

    # Scan backups for each site
    for site_path in sites:
        backup_dir = f"{site_path}/private/backups"

        # Check if backup directory exists
        test_cmd = f'test -d "{backup_dir}"'
        exit_code, _ = frappe_container.exec_run(f"sh -c '{test_cmd}'")

        if exit_code != 0:
            continue  # No backups for this site

        # List all backup files (database, files, private-files, site_config_backup)
        list_cmd = f'find "{backup_dir}" -maxdepth 1 -type f \\( -name "*-database.sql*" -o -name "*-files.tar*" -o -name "*-files.tgz" -o -name "*-private-files.tar*" -o -name "*-private-files.tgz" -o -name "*-site_config_backup.json" \\) | sort -r'

        if verbose:
            stderr_console.print(f"[dim]$ {list_cmd}[/dim]")

        exit_code, output = frappe_container.exec_run(f"sh -c '{list_cmd}'")

        if exit_code != 0:
            continue

        files = output.decode("utf-8").strip().split("\n")
        files = [f.strip() for f in files if f.strip()]

        # Extract site name from path
        site_name = site_path.split("/")[-1]

        # Parse each backup file
        for file_path in files:
            filename = file_path.split("/")[-1]
            parsed = parse_backup_filename(filename)

            if parsed:
                parsed["full_path"] = file_path
                parsed["site_dir"] = site_name  # Original site name
                backups.append(parsed)

    return backups


def group_backup_sets(backups: list) -> dict:
    """
    Group backup files by timestamp into backup sets.
    A backup set includes database, files, and private-files for the same timestamp.

    Args:
        backups: List of parsed backup dictionaries

    Returns:
        Dict mapping timestamp_str to backup set dict with:
        - database: database backup dict
        - files: files backup dict (optional)
        - private_files: private files backup dict (optional)
    """
    backup_sets = {}

    for backup in backups:
        ts = backup["timestamp_str"]

        if ts not in backup_sets:
            backup_sets[ts] = {
                "timestamp": backup["timestamp"],
                "timestamp_str": ts,
                "site_name": backup["site_name"],
                "site_dir": backup["site_dir"],
                "database": None,
                "files": None,
                "private_files": None,
                "site_config_backup": None,
            }

        # Categorize the backup file
        if backup["is_database"]:
            backup_sets[ts]["database"] = backup
        elif backup["backup_type"] == "files":
            backup_sets[ts]["files"] = backup
        elif backup["backup_type"] == "private-files":
            backup_sets[ts]["private_files"] = backup
        elif backup["backup_type"] == "site_config_backup":
            backup_sets[ts]["site_config_backup"] = backup

    # Filter to only sets that have a database backup (required for restore)
    valid_sets = {ts: bset for ts, bset in backup_sets.items() if bset["database"] is not None}

    return valid_sets


def group_and_sort_backups(backups: list, target_site: str) -> tuple:
    """
    Group backups by whether they belong to the target site, then sort by timestamp.

    Args:
        backups: List of parsed backup dictionaries
        target_site: Site name to restore to

    Returns:
        Tuple of (target_site_backups, other_site_backups), both sorted by timestamp (newest first)
        Each element is a backup set dict with database, files, and private_files
    """
    target_site_backup_name = transform_site_name_to_backup_format(target_site)

    # Group all backups into sets
    all_backup_sets = group_backup_sets(backups)

    target_backups = []
    other_backups = []

    for backup_set in all_backup_sets.values():
        # Check if backup is from target site by site name in filename OR by directory
        if (
            backup_set["site_name"] == target_site_backup_name
            or backup_set["site_dir"] == target_site
        ):
            target_backups.append(backup_set)
        else:
            other_backups.append(backup_set)

    # Sort both groups by timestamp (newest first)
    target_backups.sort(key=lambda x: x["timestamp"], reverse=True)
    other_backups.sort(key=lambda x: x["timestamp"], reverse=True)

    return target_backups, other_backups


def display_backup_selection_menu(
    target_backups: list, other_backups: list, target_site: str
) -> dict | None:
    """
    Display interactive menu for backup selection using questionary.

    Args:
        target_backups: Backup sets from the target site
        other_backups: Backup sets from other sites
        target_site: Site name being restored

    Returns:
        Selected backup set dict or None if cancelled
    """
    if not target_backups and not other_backups:
        stderr_console.print("[bold red]Error:[/bold red] No database backups found.")
        return None

    # Build choices with badges
    choices = []
    backup_map = {}

    # Simple text badges
    files_badge = "[FILES]"
    private_badge = "[PRIVATE]"
    db_only_badge = "[DATABASE ONLY]"

    # Add target site backups first
    if target_backups:
        choices.append(questionary.Separator(f"\n=== Backups from site: {target_site} ==="))
        for backup_set in target_backups:
            pretty_time = backup_set["timestamp"].strftime("%Y-%m-%d %H:%M:%S")

            # Build badge string with colored backgrounds
            badges = []
            if backup_set["files"]:
                badges.append(files_badge)
            if backup_set["private_files"]:
                badges.append(private_badge)

            badge_str = " ".join(badges) if badges else db_only_badge
            choice_text = f"{pretty_time}  {badge_str}"

            choices.append(choice_text)
            backup_map[choice_text] = backup_set

    # Add other site backups
    if other_backups:
        choices.append(questionary.Separator("\n=== Backups from other sites ==="))
        for backup_set in other_backups:
            pretty_time = backup_set["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            from_site = backup_set["site_dir"]

            # Build badge string with colored backgrounds
            badges = []
            if backup_set["files"]:
                badges.append(files_badge)
            if backup_set["private_files"]:
                badges.append(private_badge)

            badge_str = " ".join(badges) if badges else db_only_badge
            choice_text = f"{pretty_time} ({from_site})  {badge_str}"

            choices.append(choice_text)
            backup_map[choice_text] = backup_set

    # Create custom style
    custom_style = Style(
        [
            ("qmark", "fg:#00ff00 bold"),  # Bright green question mark
            ("question", "fg:#00ffff bold"),  # Bright cyan question text
            ("answer", "fg:#00ff00 bold"),  # Bright green answer
            ("pointer", "fg:#ffff00 bold"),  # Bright yellow pointer
            ("highlighted", "fg:#ffff00 bold"),  # Bright yellow highlighted option
            ("selected", "fg:#00ff00"),  # Green for selected
            ("separator", "fg:#666666"),  # Gray separator
            ("instruction", "fg:#888888"),  # Gray instructions
            ("text", "fg:#ffffff"),  # White text
        ]
    )

    console.print()
    choice = questionary.select(
        "Select a backup to restore:",
        choices=choices,
        style=custom_style,
        pointer=">",
        instruction="(Use arrow keys to navigate, Enter to select)",
    ).ask()

    if choice is None:
        console.print("[yellow]Restore cancelled.[/yellow]")
        return None

    return backup_map.get(choice)


@handle_docker_errors
def restore(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(
        None,
        "--site",
        "-s",
        help="Site name to restore. If not provided, uses the default site from common_site_config.",
        autocompletion=complete_site_names,
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to the bench directory inside the container (uses cached path from inspect if not specified).",
    ),
    mariadb_root_username: str = typer.Option(
        None,
        "--mariadb-root-username",
        help="MariaDB root username (default: root). If not provided, you will be prompted.",
    ),
    mariadb_root_password: str = typer.Option(
        None,
        "--mariadb-root-password",
        help="MariaDB root password. If not provided, you will be prompted interactively.",
    ),
    admin_password: str = typer.Option(
        None,
        "--admin-password",
        help="Set administrator password after restore.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Restore a site from a backup with interactive selection.

    This command scans all available backups across all sites in the bench,
    presents them in an interactive menu grouped by the target site, and
    executes the restore using 'bench restore'.

    If --site is not provided, the default site from common_site_config.json will be used.

    Examples:
        cwcli restore my-project --site example.com
        cwcli restore my-project  # Uses default site
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
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {bench_path}/sites"')
    if exit_code != 0:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Bench directory not found at {bench_path}"
        )
        raise typer.Exit(code=1)

    # Check if site exists
    site_path = f"{bench_path}/sites/{site}"
    exit_code, _ = frappe_container.exec_run(f'sh -c "test -d {site_path}"')
    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Site '{site}' not found at {site_path}")
        raise typer.Exit(code=1)

    # Scan backups
    show_tips = config_utils.get_show_tips()
    with TipSpinner("Scanning backups for all sites", console=stderr_console, enabled=show_tips):
        backups = scan_backups_for_all_sites(frappe_container, bench_path, verbose)

    if not backups:
        stderr_console.print("[bold red]Error:[/bold red] No database backups found in any site.")
        raise typer.Exit(code=1)

    # Group and sort backups
    target_backups, other_backups = group_and_sort_backups(backups, site)

    # Display selection menu
    selected_backup = display_backup_selection_menu(target_backups, other_backups, site)

    if not selected_backup:
        raise typer.Exit(code=0)

    # Confirm restore
    console.print()
    console.print(
        f"[bold yellow]⚠ Warning:[/bold yellow] This will replace all data in site '{site}'"
    )
    console.print(f"[dim]Backup: {selected_backup['database']['filename']}[/dim]")
    console.print(f"[dim]From: {selected_backup['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}[/dim]")

    # Show what will be restored
    restore_items = ["Database"]
    if selected_backup["files"]:
        restore_items.append("Public files")
    if selected_backup["private_files"]:
        restore_items.append("Private files")
    console.print(f"[dim]Will restore: {', '.join(restore_items)}[/dim]")
    console.print()

    try:
        confirm = questionary.confirm("Are you sure you want to restore?", default=False).ask()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Restore cancelled.[/yellow]")
        raise typer.Exit(code=0) from None

    if not confirm:
        console.print("[yellow]Restore cancelled.[/yellow]")
        raise typer.Exit(code=0)

    # Prompt for MariaDB password if not provided
    if not mariadb_root_password:
        try:
            mariadb_root_password = questionary.password("MariaDB root password:").ask()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Restore cancelled.[/yellow]")
            raise typer.Exit(code=0) from None

        if not mariadb_root_password:
            stderr_console.print("[bold red]Error:[/bold red] Password cannot be empty.")
            raise typer.Exit(code=1)

    # Validate passwords for shell injection (single quotes would break the command)
    if "'" in mariadb_root_password:
        stderr_console.print(
            "[bold red]Error:[/bold red] MariaDB password cannot contain single quotes. "
            "Please use a different password or pass it via environment variable."
        )
        raise typer.Exit(code=1)

    if admin_password and "'" in admin_password:
        stderr_console.print(
            "[bold red]Error:[/bold red] Admin password cannot contain single quotes. "
            "Please use a different password or pass it via environment variable."
        )
        raise typer.Exit(code=1)

    # Validate mariadb_root_username if provided
    if mariadb_root_username:
        if not mariadb_root_username.strip():
            stderr_console.print("[bold red]Error:[/bold red] MariaDB username cannot be empty.")
            raise typer.Exit(code=1)
        if any(char in mariadb_root_username for char in invalid_chars):
            stderr_console.print(
                "[bold red]Error:[/bold red] Invalid MariaDB username. "
                "Username cannot contain special shell characters."
            )
            raise typer.Exit(code=1)

    # Build restore command
    backup_file = selected_backup["database"]["full_path"]

    # Validate backup file path
    if not backup_file or not backup_file.strip():
        stderr_console.print("[bold red]Error:[/bold red] Backup file path is empty.")
        raise typer.Exit(code=1)

    # Verify backup file exists before attempting restore
    test_backup_cmd = f'test -f "{backup_file}"'
    exit_code, _ = frappe_container.exec_run(f"sh -c '{test_backup_cmd}'")
    if exit_code != 0:
        stderr_console.print(f"[bold red]Error:[/bold red] Backup file not found: {backup_file}")
        raise typer.Exit(code=1)

    cmd = f'bench --site {site} restore "{backup_file}"'

    # Add database credentials
    if mariadb_root_username:
        cmd += f" --mariadb-root-username {mariadb_root_username}"
    else:
        # Default to root if not specified
        cmd += " --mariadb-root-username root"

    cmd += f" --mariadb-root-password '{mariadb_root_password}'"

    if admin_password:
        cmd += f" --admin-password '{admin_password}'"

    # Add file restore flags if available
    if selected_backup["files"]:
        files_path = selected_backup["files"]["full_path"]
        cmd += f' --with-public-files "{files_path}"'

    if selected_backup["private_files"]:
        private_files_path = selected_backup["private_files"]["full_path"]
        cmd += f' --with-private-files "{private_files_path}"'

    if verbose:
        # Hide password in verbose output
        display_cmd = cmd
        if mariadb_root_password:
            display_cmd = display_cmd.replace(mariadb_root_password, "***")
        if admin_password:
            display_cmd = display_cmd.replace(admin_password, "***")
        stderr_console.print(f"[dim]$ {display_cmd}[/dim]")

    # Execute restore with spinner for clean output
    console.print()
    with TipSpinner(f"Restoring site '{site}'", console=stderr_console, enabled=show_tips):
        exit_code, output = frappe_container.exec_run(cmd, workdir=bench_path)

    # Show output if verbose or on failure
    if verbose or exit_code != 0:
        if output:
            console.print()
            console.print("[dim]Restore output:[/dim]")
            console.print(output.decode("utf-8"))

    console.print()
    if exit_code == 0:
        console.print(f"[bold green]✓[/bold green] Successfully restored site '{site}'")
        console.print(f"[dim]From backup: {selected_backup['database']['filename']}[/dim]")
        if selected_backup["files"] or selected_backup["private_files"]:
            console.print("[dim]Including file archives[/dim]")

        # Update encryption_key from backup site_config if available
        if selected_backup.get("site_config_backup"):
            try:
                backup_config_path = selected_backup["site_config_backup"]["full_path"]
                site_config_path = f"{bench_path}/sites/{site}/site_config.json"

                # Read encryption_key from backup config
                read_cmd = f'cat "{backup_config_path}"'
                exit_code, output = frappe_container.exec_run(read_cmd, workdir=bench_path)

                if exit_code == 0:
                    try:
                        backup_config = json.loads(output.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        if verbose:
                            stderr_console.print(
                                f"[yellow]Warning:[/yellow] Failed to parse backup site_config JSON: {e}"
                            )
                        raise

                    encryption_key = backup_config.get("encryption_key")

                    if encryption_key:
                        # Read current site_config
                        read_current_cmd = f'cat "{site_config_path}"'
                        exit_code, output = frappe_container.exec_run(
                            read_current_cmd, workdir=bench_path
                        )

                        if exit_code == 0:
                            try:
                                current_config = json.loads(output.decode("utf-8"))
                            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                                if verbose:
                                    stderr_console.print(
                                        f"[yellow]Warning:[/yellow] Failed to parse current site_config JSON: {e}"
                                    )
                                raise

                            current_config["encryption_key"] = encryption_key

                            # Write updated config back
                            updated_config_json = json.dumps(current_config, indent=1)
                            write_cmd = (
                                f"cat > \"{site_config_path}\" << 'EOF'\n{updated_config_json}\nEOF"
                            )

                            exit_code, _ = frappe_container.exec_run(
                                f"sh -c '{write_cmd}'", workdir=bench_path
                            )

                            if exit_code == 0:
                                console.print(
                                    "[dim]Updated encryption_key from backup site_config[/dim]"
                                )
                            else:
                                stderr_console.print(
                                    "[yellow]Warning:[/yellow] Failed to update encryption_key in site_config"
                                )
            except Exception as e:
                if verbose:
                    stderr_console.print(
                        f"[yellow]Warning:[/yellow] Failed to update encryption_key: {e}"
                    )
    else:
        stderr_console.print(f"[bold red]✗[/bold red] Failed to restore site '{site}'")
        stderr_console.print()
        stderr_console.print("[bold]Common causes:[/bold]")
        stderr_console.print("  • Incorrect MariaDB root password")
        stderr_console.print("  • Database connection issues")
        stderr_console.print("  • Corrupted backup file")
        stderr_console.print("  • Insufficient permissions")
        stderr_console.print("  • Incompatible Frappe/ERPNext versions")
        stderr_console.print()
        stderr_console.print("[dim]Tip: Run with -v flag for detailed error output[/dim]")
        raise typer.Exit(code=1)
