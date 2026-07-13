import json
import re
import shlex
import sys
from datetime import datetime

import questionary
import typer
from questionary import Style

from ..utils import bench_sites, config_utils, db_utils
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from ..utils.sendme_utils import (
    copy_to_clipboard,
    ensure_sendme_installed,
    get_sendme_command,
)
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running, resolve_bench_path


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
    extensions: list[str] = []
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


def _prompt_mariadb_credentials(
    mariadb_root_username: str | None,
    mariadb_root_password: str | None,
) -> tuple[str, str]:
    """Resolve the MariaDB root credentials for a restore, in BOTH modes.

    Shared by the normal and receive restore paths so credential handling is
    identical everywhere:

    - **Interactive (a TTY):** prompt for the username (defaulting to ``root``)
      and the password, actually collecting the input. A blank username keeps the
      ``root`` default; a blank password is an error (a restore needs the real
      root password). This fixes the normal path silently skipping the username
      prompt and never collecting the password. The preceding restore confirmation
      disables questionary's auto-enter, so it consumes its own trailing Enter and
      can never leave a stray keystroke for the password prompt to swallow as empty
      input - the password prompt always waits for real input regardless of which
      credential flags were passed.
    - **Non-interactive (flags / non-TTY):** use the ``--mariadb-root-*`` flags.
      The username has a safe default (``root``) so it may be omitted, but the
      password is a secret with no default: a non-TTY WITHOUT
      ``--mariadb-root-password`` refuses with a non-zero exit rather than
      hanging on a prompt or proceeding with an empty password.

    Returns ``(username, password)`` with both guaranteed non-empty.
    """
    is_tty = sys.stdin.isatty()

    # Username: defaults to root; only prompted interactively.
    if not mariadb_root_username:
        if is_tty:
            try:
                answer = questionary.text("MariaDB root username:", default="root").ask()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1) from None
            if answer is None:  # Ctrl-C / no input
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1)
            mariadb_root_username = answer.strip() or "root"
        else:
            mariadb_root_username = "root"

    # Password: a required secret with no default.
    if not mariadb_root_password:
        if is_tty:
            try:
                mariadb_root_password = questionary.password("MariaDB root password:").ask()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1) from None
            if not mariadb_root_password:
                stderr_console.print("[bold red]Error:[/bold red] Password cannot be empty.")
                raise typer.Exit(code=1)
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No MariaDB root password provided and not running "
                "interactively.\n[dim]Pass --mariadb-root-password (and, if not 'root', "
                "--mariadb-root-username) to restore non-interactively.[/dim]"
            )
            raise typer.Exit(code=1)

    return mariadb_root_username, mariadb_root_password


def _resolve_default_site(
    project_name: str,
    bench_path: str | None,
    frappe_container,
    verbose: bool = False,
) -> str | None:
    """Resolve a bench's default site from EITHER of the two places it is recorded.

    Frappe records the default site in ``common_site_config.json``'s
    ``default_site`` OR ``sites/currentsite.txt`` (the pointer ``bench use``
    writes). The cache-backed :func:`db_utils.get_default_site` already checks
    both (``default_site`` then the cached ``current_site``); this adds a LIVE
    read of ``currentsite.txt`` as a final fallback so a cold or stale cache still
    resolves the default for this (destructive) restore. Returns None if neither
    source names a site.
    """
    site = db_utils.get_default_site(project_name, bench_path)
    if site:
        return site
    if frappe_container is not None and bench_path:
        site = bench_sites.read_current_site(frappe_container, bench_path, verbose)
        if site and verbose:
            stderr_console.print(f"[dim]Default site from currentsite.txt: {site}[/dim]")
    return site


def _post_restore_migrate_and_restart(
    frappe_container,
    project_name: str,
    bench_path: str,
    site: str,
    verbose: bool = False,
) -> bool:
    """Run ``bench migrate`` then restart the instance after a successful restore.

    A restored database is at the backup's schema, which may lag the bench's app
    code, so ``bench --site <site> migrate`` brings it up to date; the instance is
    then restarted so its running processes reconnect and pick up the migrated DB
    (``_start_project`` kills the old ``bench start`` and relaunches it - the same
    thing ``cwcli restart`` does for the app).

    A failed migrate does NOT undo the (successful) restore; it is surfaced clearly
    and the instance is still restarted, but the function returns False so the
    caller can exit non-zero to flag that the site needs attention.
    """
    migrate_ok = True

    show_tips = config_utils.get_show_tips()
    migrate_cmd = f"bench --site {shlex.quote(site)} migrate"
    if verbose:
        stderr_console.print(f"[dim]$ {migrate_cmd}[/dim]")

    console.print()
    # A Docker/API exception from exec_run must NOT skip the restart: treat it as a
    # failed-but-reported migrate (exit_code=1) so the restart below still runs. A
    # failed migrate is surfaced but never prevents the instance from coming back up.
    try:
        with TipSpinner(f"Migrating site '{site}'", console=stderr_console, enabled=show_tips):
            exit_code, output = frappe_container.exec_run(
                ["sh", "-c", migrate_cmd], workdir=bench_path
            )
    except Exception as exc:
        exit_code = 1
        output = f"Failed to run migrate: {exc}".encode("utf-8", errors="replace")

    if verbose or exit_code != 0:
        if output:
            console.print()
            console.print("[dim]Migrate output:[/dim]")
            console.print(output.decode("utf-8", errors="replace"))

    if exit_code == 0:
        console.print(f"[bold green]✓[/bold green] Migrated site '{site}'")
    else:
        migrate_ok = False
        stderr_console.print(
            f"[bold yellow]⚠ Warning:[/bold yellow] 'bench migrate' failed for site '{site}'. "
            "The database was restored, but the schema may be out of date."
        )
        stderr_console.print(
            f"[dim]Run it manually: cwcli run {project_name} -- bench --site {site} migrate[/dim]"
        )

    # Restart the instance so the app reconnects to the restored/migrated DB.
    # Imported lazily to avoid any import cycle at module load.
    from .start import _start_project

    console.print()
    console.print("[bold cyan]Restarting instance...[/bold cyan]")
    # Restart the SAME bench that was just migrated/restored (bench_path), never the
    # first sorted bench: pass it as an explicit override so a multi-bench restore
    # into a non-first bench does not kill/restart the wrong dev server.
    log_file = _start_project(
        project_name, verbose=verbose, bench_path_override=bench_path, restart=True
    )
    if log_file:
        console.print(f"[bold green]✓[/bold green] Instance restarted (logs: {log_file})")
    else:
        console.print("[bold green]✓[/bold green] Instance restart requested")

    return migrate_ok


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
    backups: list[dict] = []

    # Get all sites in the bench
    sites_path = f"{bench_path}/sites"
    quoted_sites_path = shlex.quote(sites_path)
    cmd = f'find {quoted_sites_path} -maxdepth 1 -mindepth 1 -type d -not -name "assets" -not -name "common_site_config.json"'

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
        quoted_backup_dir = shlex.quote(backup_dir)

        # Check if backup directory exists
        test_cmd = f"test -d {quoted_backup_dir}"
        exit_code, _ = frappe_container.exec_run(f"sh -c '{test_cmd}'")

        if exit_code != 0:
            continue  # No backups for this site

        # List all backup files (database, files, private-files, site_config_backup)
        list_cmd = f'find {quoted_backup_dir} -maxdepth 1 -type f \\( -name "*-database.sql*" -o -name "*-files.tar*" -o -name "*-files.tgz" -o -name "*-private-files.tar*" -o -name "*-private-files.tgz" -o -name "*-site_config_backup.json" \\) | sort -r'

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


def _read_backup_installed_apps(
    frappe_container,
    backup_db_path: str,
    verbose: bool = False,
) -> set[str] | None:
    """Read the list of apps a backup's site had installed, from the DB dump.

    Frappe records a site's installed apps as a JSON list under the ``__global`` /
    ``installed_apps`` default (this is exactly what ``frappe.get_installed_apps()``
    reads), so the dump contains a row like::

        ...,'["frappe", "widgets"]','installed_apps',...

    We ``zcat -f`` the dump (``-f`` streams a plain ``.sql`` too) and grep out that
    one array, then extract the app-name tokens from it. This tells us which apps
    the BACKUP needs - the right question for "will this restore + migrate break
    because an app's code is missing", which the current (about-to-be-overwritten)
    site cannot answer.

    Returns the set of app names, or ``None`` when the list could not be read
    (unreadable dump, or the marker not present) so the caller can fail safe and
    NOT warn on a false negative.
    """
    quoted = shlex.quote(backup_db_path)
    # Grab the installed_apps global's JSON array. ``zcat -f`` handles gzip AND an
    # already-plain .sql; grep is byte-oriented (-a) since the dump is binary-ish.
    extract = (
        f"zcat -f {quoted} 2>/dev/null | " "grep -aoE \"\\[[^]]*\\]','installed_apps'\" | head -1"
    )
    if verbose:
        stderr_console.print(f"[dim]$ {extract}[/dim]")
    exit_code, output = frappe_container.exec_run(["sh", "-c", extract])
    if exit_code != 0:
        return None
    try:
        text = output.decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    if not text:
        return None
    # ``text`` looks like: [\"frappe\", \"widgets\"]','installed_apps'
    # Take everything up to the first ``]`` (the JSON array body) and pull out the
    # app-name tokens, tolerant of mysqldump's escaping of the inner quotes.
    array_body = text.split("]", 1)[0]
    apps = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", array_body))
    return apps or None


def check_missing_apps(
    frappe_container,
    project_name: str,
    bench_path: str,
    backup_db_path: str,
    verbose: bool = False,
    no_recache: bool = False,
) -> list[str]:
    """
    Return the apps the BACKUP needs that the bench does not physically have.

    Warns the user, before a destructive restore, that the backup was taken on a
    site with apps whose code is not in this bench's ``apps/`` - those apps will
    fail ``bench migrate`` after the restore. This is the captain's exact concern
    ("the backup uses apps the bench does NOT have installed").

    The backup's app list is read from its own DB dump
    (:func:`_read_backup_installed_apps`), NOT from the current site: the site is
    about to be overwritten, and reading its apps via ``bench list-apps`` cannot
    even name an app whose code is already missing (that import crashes). Available
    apps are read live from ``apps/`` so the check never depends on cache freshness.

    Because both sides of the comparison are read live, this check no longer
    touches cwcli's cache. ``no_recache`` is therefore a DEPRECATED no-op, kept
    only so existing callers (and the ``--no-recache`` CLI flag) keep working.

    Args:
        frappe_container: Docker container object
        project_name: Name of the project (unused; kept for signature stability)
        bench_path: Path to bench directory
        backup_db_path: Full container path to the backup's database dump
        verbose: Enable verbose output
        no_recache: Deprecated no-op (the check reads everything live; retained for
            backward compatibility with the ``--no-recache`` flag and callers)

    Returns:
        Sorted list of missing app names (empty when none, or when the backup's app
        list could not be determined - fail safe, never a false warning).
    """
    # Apps physically present in the bench (its apps/ directory), read live.
    quoted_apps_dir = shlex.quote(f"{bench_path}/apps")
    exit_code, output = frappe_container.exec_run(["sh", "-c", f"ls -1 {quoted_apps_dir}"])
    if exit_code != 0:
        if verbose:
            stderr_console.print(
                f"[yellow]Warning:[/yellow] Could not list apps in {bench_path}/apps."
            )
        return []
    available_apps = {
        a.strip() for a in output.decode("utf-8", errors="replace").split("\n") if a.strip()
    }

    # Apps the backup needs (from its own dump).
    backup_apps = _read_backup_installed_apps(frappe_container, backup_db_path, verbose)
    if backup_apps is None:
        # Could not read the backup's app list -> fail safe, do not warn.
        if verbose:
            stderr_console.print(
                "[yellow]Warning:[/yellow] Could not read the backup's installed apps; "
                "skipping the missing-apps check."
            )
        return []

    missing_apps = backup_apps - available_apps
    return sorted(missing_apps)


def select_backup_set(
    target_backups: list, other_backups: list, *, latest: bool, backup_file: str | None
) -> dict | None:
    """Resolve a backup set non-interactively. Returns None if no match.

    A pure selector (no TTY, no prompts) so it is unit-testable without a
    container or questionary. Used by the normal ``restore`` path when the
    ``--latest`` or ``--backup-file`` selector is passed so an agent or script
    can pick a backup without driving the interactive menu.

    Args:
        target_backups: Backup sets for the target site (newest-first, as
            ``group_and_sort_backups`` returns).
        other_backups: Backup sets for OTHER sites (newest-first). Deliberately
            NOT consulted by ``--latest``: silently restoring another site's
            newest backup over the target site is exactly the surprise this
            command must not spring. ``--backup-file`` DOES search across both
            lists because the caller names a specific file and may legitimately
            need a file from another site's backup directory.
        latest: When True, return ``target_backups[0]`` (the newest target-site
            backup), or None if ``target_backups`` is empty.
        backup_file: Match against ``set["database"]["filename"]`` OR
            ``set["database"]["full_path"]`` (exact string compare, both forms
            accepted). The first match is returned. If multiple match (should
            not happen, filenames embed timestamps, but be safe) the first is
            returned and a yellow note is printed.

    Returns:
        The matched backup-set dict, or None.
    """
    if latest:
        return target_backups[0] if target_backups else None

    if backup_file is not None:
        matches: list[dict] = []
        for bset in target_backups + other_backups:
            db = bset.get("database")
            if not db:
                continue
            if backup_file in (db.get("filename"), db.get("full_path")):
                matches.append(bset)
        if not matches:
            return None
        if len(matches) > 1:
            console.print(
                f"[yellow]Note:[/yellow] multiple backups match '{backup_file}'; "
                "using the first match."
            )
        return matches[0]

    return None


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
    # Build choices with badges
    choices: list[str | questionary.Separator] = []
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

    # Always add option to restore from remote source
    if not target_backups and not other_backups:
        # No local backups - show message
        choices.append(questionary.Separator("\n=== No local backups found ==="))

    choices.append(questionary.Separator("\n=== Remote Source ==="))
    remote_choice = "Restore from remote source (via sendme)"
    choices.append(remote_choice)
    backup_map[remote_choice] = {"_restore_from_ticket": True}

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


def restore_send_mode(
    project_name: str,
    site: str | None,
    bench_path: str | None,
    frappe_container,
    verbose: bool,
):
    """
    Send mode: Select a backup and share it via sendme.

    Args:
        project_name: Docker Compose project name
        site: Optional site name filter
        bench_path: Optional bench path
        frappe_container: Frappe container object
        verbose: Enable verbose output
    """
    import subprocess
    import tempfile
    from pathlib import Path

    # Get bench path from cache if not provided
    if not bench_path:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            bench_path = resolve_bench_path(project_name, None, None, verbose=verbose)
            if bench_path:
                if verbose:
                    stderr_console.print(f"[dim]Using cached bench path: {bench_path}[/dim]")
            else:
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                )
        else:
            # No cache found, run inspect to populate it
            stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")

            try:
                # Run inspect to populate cache (it has its own spinner)
                from .inspect import inspect as inspect_cmd_func

                # Call inspect directly with just the parameters it needs
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
                bench_path = resolve_bench_path(project_name, None, None, verbose=verbose)
                if bench_path:
                    if verbose:
                        stderr_console.print(
                            f"[dim]Using cached bench path from inspect: {bench_path}[/dim]"
                        )
                else:
                    # Still no cache, use default
                    bench_path = "/workspace/frappe-bench"
                    stderr_console.print(
                        f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                    )
            except typer.Exit:
                raise
            except Exception as e:
                # Inspect failed, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Inspect failed. Using default bench path: {bench_path}[/yellow]"
                )
                if verbose:
                    stderr_console.print(f"[dim]Inspect error: {e}[/dim]")

    # Scan available backups
    console.print("[bold cyan]Scanning for backups...[/bold cyan]")
    backups = scan_backups_for_all_sites(frappe_container, bench_path, verbose)

    if not backups:
        stderr_console.print("[bold red]Error:[/bold red] No backups found.")
        raise typer.Exit(code=1)

    # Group and sort backups
    if site:
        target_backups, other_backups = group_and_sort_backups(backups, site)
    else:
        # If no site specified, show all backups
        # Use empty string as target to show all as "other"
        target_backups, other_backups = group_and_sort_backups(backups, "")

    # Display selection menu
    selected = display_backup_selection_menu(target_backups, other_backups, site or "")
    if not selected:
        return

    # Collect all files for this backup
    files_to_send = []
    if selected.get("database"):
        files_to_send.append(selected["database"]["full_path"])
    if selected.get("files"):
        files_to_send.append(selected["files"]["full_path"])
    if selected.get("private_files"):
        files_to_send.append(selected["private_files"]["full_path"])
    if selected.get("site_config_backup"):
        files_to_send.append(selected["site_config_backup"]["full_path"])

    if not files_to_send:
        stderr_console.print("[bold red]Error:[/bold red] No files to send.")
        raise typer.Exit(code=1)

    # Create temporary directory for copying files from container
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        console.print()
        console.print(
            f"[bold cyan]Preparing {len(files_to_send)} file(s) for transfer...[/bold cyan]"
        )

        # Copy each file from container to temp directory
        for file_path in files_to_send:
            filename = file_path.split("/")[-1]

            if verbose:
                stderr_console.print(f"[dim]Copying {filename} from container...[/dim]")

            # Use docker cp to copy file from container

            try:
                # Get raw bits from container
                bits, _ = frappe_container.get_archive(file_path)

                # Write to tar file temporarily
                import tarfile

                tar_path = temp_path / f"{filename}.tar"
                with open(tar_path, "wb") as f:
                    for chunk in bits:
                        f.write(chunk)

                # Extract from tar
                with tarfile.open(tar_path, "r") as tar:
                    # Extract just the file we want
                    for member in tar.getmembers():
                        if member.isfile():
                            member.name = filename  # Rename to avoid path issues
                            tar.extract(member, temp_path)
                            break

                # Remove tar file
                tar_path.unlink()

            except Exception as e:
                stderr_console.print(f"[bold red]Error:[/bold red] Failed to copy {filename}: {e}")
                raise typer.Exit(code=1) from e

        console.print("[bold green]✓[/bold green] Files prepared for transfer")
        console.print()

        # Create sendme ticket
        sendme_cmd = get_sendme_command()

        console.print("[bold cyan]Creating sendme ticket...[/bold cyan]")
        console.print()

        try:
            # Run sendme to create ticket and start transfer
            cmd = [sendme_cmd, "send", str(temp_path)]

            if verbose:
                stderr_console.print(f"[dim]$ {' '.join(cmd)}[/dim]")

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )

            ticket = None
            ticket_line_pattern = re.compile(r"sendme receive (\S+)")

            # Read stdout to find the ticket
            if process.stdout:
                for line in iter(process.stdout.readline, ""):
                    stripped = line.strip()

                    # In verbose mode, only show important lines (not progress updates)
                    if verbose and stripped:
                        # Only show lines that start at column 0 (not indented progress bars)
                        if line and not line[0].isspace():
                            stderr_console.print(f"[dim]\\[sendme] {stripped}[/dim]")

                    match = ticket_line_pattern.search(line)
                    if match:
                        ticket = match.group(1)
                        break
                    if process.poll() is not None:  # Process terminated early
                        break

            if ticket:
                # Copy ticket to clipboard
                if copy_to_clipboard(ticket):
                    clipboard_msg = "[dim]Ticket copied to clipboard.[/dim]"
                else:
                    clipboard_msg = (
                        "[yellow]Could not copy to clipboard. Please copy it manually.[/yellow]"
                    )

                console.print(f"\n[bold green]sendme ticket:[/bold green] {ticket}")
                console.print(clipboard_msg)
                console.print("\n[bold cyan]Instructions for the other machine:[/bold cyan]")
                console.print("1. Run: [bold]cwcli restore <project_name> --receive[/bold]")
                console.print("2. Paste the ticket when prompted.")
                console.print("\n[dim]Waiting for transfer... Press Ctrl+C when done.[/dim]\n")

                # Wait for the process to complete, or for Ctrl+C
                process.wait()
            else:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Could not extract sendme ticket from output."
                )
                stderr_output = process.stderr.read() if process.stderr else ""
                if stderr_output:
                    stderr_console.print(f"[dim]sendme error output:[/dim]\n{stderr_output}")
                raise typer.Exit(code=1)

        except KeyboardInterrupt:
            console.print("\n[yellow]Send operation cancelled by user.[/yellow]")
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            raise typer.Exit(code=0) from None
        except FileNotFoundError as e:
            stderr_console.print(
                "[bold red]Error:[/bold red] sendme command not found. "
                "Try restarting your terminal or running: source ~/.bashrc"
            )
            raise typer.Exit(code=1) from e
        except Exception as e:
            stderr_console.print(f"[bold red]Error:[/bold red] Failed to run sendme: {e}")
            raise typer.Exit(code=1) from e


def restore_receive_mode(
    project_name: str,
    site: str | None,
    bench_path: str | None,
    mariadb_root_username: str | None,
    mariadb_root_password: str | None,
    admin_password: str | None,
    no_recache: bool,
    verbose: bool,
    yes: bool = False,
    no_migrate: bool = False,
):
    """
    Receive mode: Download backup from sendme and restore it.

    Args:
        project_name: Docker Compose project name
        site: Optional site name
        bench_path: Optional bench path
        mariadb_root_username: Optional MariaDB username
        mariadb_root_password: Optional MariaDB password
        admin_password: Optional admin password
        no_recache: Deprecated no-op (the missing-apps check reads everything
            live from the bench and the backup dump; retained for backward
            compatibility with the ``--no-recache`` flag)
        verbose: Enable verbose output
        yes: Skip the destructive-restore confirmation (non-interactive)
        no_migrate: Skip the post-restore ``bench migrate`` + instance restart
    """
    import subprocess
    import tempfile
    from pathlib import Path

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

    # Get bench path
    if not bench_path:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            bench_path = resolve_bench_path(project_name, None, None, verbose=verbose)
            if bench_path:
                if verbose:
                    stderr_console.print(f"[dim]Using cached bench path: {bench_path}[/dim]")
            else:
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                )
        else:
            # No cache found, run inspect to populate it
            stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")

            try:
                # Run inspect to populate cache (it has its own spinner)
                from .inspect import inspect as inspect_cmd_func

                # Call inspect directly with just the parameters it needs
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
                bench_path = resolve_bench_path(project_name, None, None, verbose=verbose)
                if bench_path:
                    if verbose:
                        stderr_console.print(
                            f"[dim]Using cached bench path from inspect: {bench_path}[/dim]"
                        )
                else:
                    # Still no cache, use default
                    bench_path = "/workspace/frappe-bench"
                    stderr_console.print(
                        f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                    )
            except typer.Exit:
                raise
            except Exception as e:
                # Inspect failed, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Inspect failed. Using default bench path: {bench_path}[/yellow]"
                )
                if verbose:
                    stderr_console.print(f"[dim]Inspect error: {e}[/dim]")

    # Get site if not provided. Resolve the default from EITHER
    # common_site_config.json's default_site OR sites/currentsite.txt (a plain
    # `bench use`d dev bench only has the latter).
    if not site:
        try:
            default_site = _resolve_default_site(
                project_name, bench_path, frappe_container, verbose=verbose
            )
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

    # Prompt for sendme ticket
    console.print()
    console.print("[bold cyan]Receive backup via sendme[/bold cyan]")
    console.print()

    ticket = questionary.text(
        "Enter the sendme ticket:",
        validate=lambda text: len(text.strip()) > 0 or "Ticket cannot be empty",
    ).ask()

    if not ticket:
        console.print("[yellow]Receive cancelled.[/yellow]")
        return

    # Clean up ticket - remove all whitespace including newlines, tabs, and spaces
    # This handles janky copy-paste from sendme's output with carriage returns
    ticket = "".join(ticket.split())

    if not ticket:
        stderr_console.print("[bold red]Error:[/bold red] Ticket cannot be empty after cleanup")
        raise typer.Exit(code=1)

    # Create temporary directory for download
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        console.print()
        console.print("[bold cyan]Downloading backup files...[/bold cyan]")

        sendme_cmd = get_sendme_command()

        try:
            # Run sendme to download files
            receive_cmd = [sendme_cmd, "receive", ticket]

            if verbose:
                stderr_console.print(f"[dim]$ {' '.join(receive_cmd)}[/dim]")

            result = subprocess.run(
                receive_cmd, cwd=temp_dir, capture_output=not verbose, text=True
            )

            if result.returncode != 0:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Failed to download files via sendme"
                )
                if result.stderr and not verbose:
                    stderr_console.print(result.stderr)
                raise typer.Exit(code=1)

        except FileNotFoundError as e:
            stderr_console.print(
                "[bold red]Error:[/bold red] sendme command not found. "
                "Try restarting your terminal or running: source ~/.bashrc"
            )
            raise typer.Exit(code=1) from e
        except Exception as e:
            stderr_console.print(f"[bold red]Error:[/bold red] Failed to run sendme: {e}")
            raise typer.Exit(code=1) from e

        console.print("[bold green]✓[/bold green] Files downloaded successfully")
        console.print()

        # Find downloaded backup files
        downloaded_items = list(temp_path.glob("*"))

        if not downloaded_items:
            stderr_console.print("[bold red]Error:[/bold red] No files were downloaded")
            raise typer.Exit(code=1)

        # Check if we received a single directory (a collection)
        if len(downloaded_items) == 1 and downloaded_items[0].is_dir():
            # This is a collection, look for files inside this directory
            download_root = downloaded_items[0]
            downloaded_files = list(download_root.glob("*"))
        else:
            # We received individual files
            downloaded_files = downloaded_items

        if not downloaded_files:
            stderr_console.print("[bold red]Error:[/bold red] No backup files found in download")
            raise typer.Exit(code=1)

        if verbose:
            stderr_console.print(f"[dim]Downloaded {len(downloaded_files)} file(s)[/dim]")
            for f in downloaded_files:
                stderr_console.print(f"[dim]  - {f.name}[/dim]")

        # Parse downloaded files to identify backup components
        database_file = None
        files_archive = None
        private_files_archive = None
        site_config_backup = None

        for file in downloaded_files:
            parsed = parse_backup_filename(file.name)
            if parsed:
                backup_type = parsed["backup_type"]
                if backup_type == "database":
                    database_file = file
                elif backup_type == "files":
                    files_archive = file
                elif backup_type == "private-files":
                    private_files_archive = file
                elif backup_type == "site_config_backup":
                    site_config_backup = file

        if not database_file:
            stderr_console.print(
                "[bold red]Error:[/bold red] No database backup found in downloaded files"
            )
            raise typer.Exit(code=1)

        # Copy files to container's backup directory
        backup_dir = f"{bench_path}/sites/{site}/private/backups"
        quoted_backup_dir = shlex.quote(backup_dir)

        console.print("[bold cyan]Copying files to container...[/bold cyan]")

        # Ensure backup directory exists
        test_cmd = f"test -d {quoted_backup_dir}"
        exit_code, _ = frappe_container.exec_run(f"sh -c '{test_cmd}'")
        if exit_code != 0:
            if verbose:
                stderr_console.print(f"[dim]Creating backup directory at {backup_dir}[/dim]")
            mkdir_cmd = f"mkdir -p {quoted_backup_dir}"
            exit_code, output = frappe_container.exec_run(f"sh -c '{mkdir_cmd}'")
            if exit_code != 0:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Failed to create backup directory"
                )
                raise typer.Exit(code=1)

        # Copy each file to container
        import tarfile

        for local_file in downloaded_files:
            if verbose:
                stderr_console.print(f"[dim]Copying {local_file.name} to container...[/dim]")

            # Create tar archive of the file
            tar_path = temp_path / f"{local_file.name}.tar"
            with tarfile.open(tar_path, "w") as tar:
                tar.add(local_file, arcname=local_file.name)

            # Stream the tar into the container instead of reading the whole
            # (possibly multi-GB) archive into host RAM, and fail loudly if the
            # copy did not succeed rather than letting it surface later as a
            # confusing "backup not found".
            with open(tar_path, "rb") as tar_file:
                copied = frappe_container.put_archive(backup_dir, tar_file)

            if not copied:
                stderr_console.print(
                    f"[bold red]Error:[/bold red] Failed to copy '{local_file.name}' "
                    "into the container."
                )
                raise typer.Exit(code=1)

            tar_path.unlink()

        console.print("[bold green]✓[/bold green] Files copied to container")
        console.print()

        # Now perform the restore using the copied files
        console.print("[bold cyan]Starting restore process...[/bold cyan]")
        console.print()

        # Resolve MariaDB credentials (interactive prompts or flags; a non-TTY
        # without --mariadb-root-password refuses rather than proceeding empty).
        mariadb_root_username, mariadb_root_password = _prompt_mariadb_credentials(
            mariadb_root_username, mariadb_root_password
        )

        # Check for missing apps before proceeding with restore
        console.print()
        show_tips = config_utils.get_show_tips()
        with TipSpinner("Checking for missing apps", console=stderr_console, enabled=show_tips):
            # The downloaded backup is already copied into the container's backup
            # dir; check the apps IT needs against this bench.
            missing_apps = check_missing_apps(
                frappe_container,
                project_name,
                bench_path,
                f"{backup_dir}/{database_file.name}",
                verbose=verbose,
                no_recache=no_recache,
            )

        if missing_apps:
            console.print()
            console.print(
                "[bold yellow]⚠ Warning:[/bold yellow] The following apps are installed on the backup site but not available on this bench:"
            )
            for app in missing_apps:
                console.print(f"  • {app}")
            console.print()
            console.print(
                "[dim]You may need to install these apps before restoring to avoid errors.[/dim]"
            )
            console.print(
                f"[dim]Install apps with: bench get-app <app-name> && bench --site {site} install-app <app-name>[/dim]"
            )
            console.print()

            if yes:
                console.print("[dim]Proceeding despite missing apps (--yes).[/dim]")
            elif not sys.stdin.isatty():
                stderr_console.print(
                    "[bold red]Error:[/bold red] Refusing to restore with missing apps "
                    "without confirmation. Re-run with --yes to proceed non-interactively."
                )
                raise typer.Exit(code=1)
            else:
                try:
                    proceed = questionary.confirm(
                        "Do you want to continue with the restore anyway?",
                        default=False,
                        auto_enter=False,
                    ).ask()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Restore cancelled.[/yellow]")
                    raise typer.Exit(code=1) from None

                if not proceed:
                    console.print("[yellow]Restore cancelled.[/yellow]")
                    raise typer.Exit(code=1)

        # Confirm the destructive restore. Receiving a peer's backup and running
        # `bench restore --force` drops and recreates the live site's database, so
        # it must carry the same warning + confirmation the normal restore path has
        # (this is not covered by the conditional missing-apps prompt above, which
        # only fires when apps are missing). Honor --yes for non-interactive use;
        # under a non-TTY without --yes, refuse and exit non-zero.
        console.print()
        console.print(
            f"[bold yellow]⚠ Warning:[/bold yellow] This will replace all data in site '{site}'"
        )
        console.print(f"[dim]Backup: {database_file.name}[/dim]")

        # Compare the backup's origin site against the target and warn on mismatch.
        origin_parsed = parse_backup_filename(database_file.name)
        origin_site = origin_parsed["site_name"] if origin_parsed else None
        if origin_site and origin_site != transform_site_name_to_backup_format(site):
            console.print(
                f"[bold yellow]⚠ Origin mismatch:[/bold yellow] this backup is from site "
                f"'{origin_site}', but you are restoring into '{site}'."
            )
        console.print()

        if yes:
            console.print("[dim]Proceeding without confirmation (--yes).[/dim]")
        elif not sys.stdin.isatty():
            stderr_console.print(
                "[bold red]Error:[/bold red] Refusing to restore without confirmation. "
                "Re-run with --yes to restore non-interactively."
            )
            raise typer.Exit(code=1)
        else:
            try:
                confirm = questionary.confirm(
                    "Are you sure you want to restore?", default=False, auto_enter=False
                ).ask()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1) from None

            if not confirm:
                console.print("[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1)

        # Build restore command. Secrets are passed via the environment (never on
        # the argv) so they do not appear in the container process list (M5).
        #
        # The database backup MUST be the FULL container path
        # (``<backup_dir>/<file>``), not the bare filename: `bench restore` rejects
        # a bare filename with "Invalid path <file>". The arg ORDER also mirrors the
        # normal restore path exactly (db path, credentials, --force, then the file
        # archives) so the two paths behave identically.
        restore_env: dict[str, str] = {}
        database_path = f"{backup_dir}/{database_file.name}"
        cmd = f"bench --site {shlex.quote(site)} restore {shlex.quote(database_path)}"

        cmd += f" --mariadb-root-username {shlex.quote(mariadb_root_username)}"
        restore_env["CWCLI_MARIADB_ROOT_PASSWORD"] = mariadb_root_password
        cmd += ' --mariadb-root-password "$CWCLI_MARIADB_ROOT_PASSWORD"'
        cmd += " --force"  # Bypass version check prompts for non-interactive restore

        if admin_password:
            restore_env["CWCLI_ADMIN_PASSWORD"] = admin_password
            cmd += ' --admin-password "$CWCLI_ADMIN_PASSWORD"'

        if files_archive:
            # Use full path to file in backup directory
            files_path = f"{backup_dir}/{files_archive.name}"
            cmd += f" --with-public-files {shlex.quote(files_path)}"
        if private_files_archive:
            # Use full path to file in backup directory
            private_files_path = f"{backup_dir}/{private_files_archive.name}"
            cmd += f" --with-private-files {shlex.quote(private_files_path)}"

        if verbose:
            # Secrets live in the environment, so the command itself is safe to print.
            stderr_console.print(f"[dim]$ {cmd}[/dim]")

        # Execute restore
        show_tips = config_utils.get_show_tips()
        with TipSpinner(
            f"Restoring backup to site '{site}'", console=stderr_console, enabled=show_tips
        ):
            exit_code, output = frappe_container.exec_run(
                ["sh", "-c", cmd], workdir=backup_dir, environment=restore_env
            )

        # Show output if verbose or on failure
        if verbose or exit_code != 0:
            if output:
                console.print()
                console.print("[dim]Restore output:[/dim]")
                try:
                    console.print(output.decode("utf-8"))
                except UnicodeDecodeError:
                    console.print(output.decode("utf-8", errors="replace"))

        console.print()
        if exit_code == 0:
            console.print(
                f"[bold green]✓[/bold green] Successfully restored backup to site '{site}'"
            )

            # Handle site_config_backup encryption key restoration
            if site_config_backup:
                console.print()
                console.print("[dim]Updating encryption key from backup...[/dim]")

                try:
                    # Read site_config_backup from container
                    site_config_path = f"{backup_dir}/{site_config_backup.name}"
                    quoted_config_path = shlex.quote(site_config_path)
                    exit_code, output = frappe_container.exec_run(
                        f"sh -c 'cat {quoted_config_path}'"
                    )

                    if exit_code == 0:
                        backup_config = json.loads(output.decode("utf-8"))
                        encryption_key = backup_config.get("encryption_key")

                        if encryption_key:
                            # Read current site config
                            current_site_config_path = f"{bench_path}/sites/{site}/site_config.json"
                            quoted_current_config = shlex.quote(current_site_config_path)
                            exit_code, output = frappe_container.exec_run(
                                f"sh -c 'cat {quoted_current_config}'"
                            )

                            if exit_code == 0:
                                current_config = json.loads(output.decode("utf-8"))
                                current_config["encryption_key"] = encryption_key

                                # Write updated config back using heredoc
                                updated_json = json.dumps(current_config, indent=1)
                                write_cmd = (
                                    f"cat > {quoted_current_config} << 'EOF'\n{updated_json}\nEOF"
                                )
                                exit_code, _ = frappe_container.exec_run(
                                    f"sh -c '{write_cmd}'", workdir=bench_path
                                )

                                if exit_code == 0:
                                    console.print(
                                        "[bold green]✓[/bold green] Encryption key updated from backup"
                                    )
                                else:
                                    stderr_console.print(
                                        "[yellow]Warning:[/yellow] Failed to update encryption key"
                                    )
                except Exception as e:
                    if verbose:
                        stderr_console.print(
                            f"[yellow]Warning:[/yellow] Could not update encryption key: {e}"
                        )

            # Migrate the restored DB to the code's schema, then restart the
            # instance so it comes back up cleanly (skippable with --no-migrate).
            if not no_migrate:
                migrate_ok = _post_restore_migrate_and_restart(
                    frappe_container, project_name, bench_path, site, verbose=verbose
                )
                if not migrate_ok:
                    raise typer.Exit(code=1)
        else:
            stderr_console.print(
                f"[bold red]✗[/bold red] Failed to restore backup to site '{site}'"
            )
            stderr_console.print()
            stderr_console.print("[bold]Common causes:[/bold]")
            stderr_console.print("  • Incorrect MariaDB credentials")
            stderr_console.print("  • Database already exists (drop it first)")
            stderr_console.print("  • Incompatible backup version")
            stderr_console.print("  • Insufficient disk space or permissions")
            stderr_console.print()
            stderr_console.print("[dim]Tip: Run with -v flag for detailed error output[/dim]")
            raise typer.Exit(code=1)


@handle_docker_errors
def restore(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(
        None,
        "--site",
        "-s",
        help="Site name to restore. If not provided, uses the default site "
        "(from common_site_config.json's default_site or sites/currentsite.txt).",
        autocompletion=complete_site_names,
    ),
    latest: bool = typer.Option(
        False,
        "--latest",
        help="Non-interactively select the most recent backup set for the target site.",
    ),
    backup_file: str | None = typer.Option(
        None,
        "--backup-file",
        help="Non-interactively select the backup set whose database file matches this "
        "filename (or full container path).",
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to target: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str | None = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
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
    send: bool = typer.Option(
        False,
        "--send",
        help="Send a backup to a remote location via sendme (P2P transfer).",
    ),
    receive: bool = typer.Option(
        False,
        "--receive",
        help="Receive and restore a backup from a remote location via sendme (P2P transfer).",
    ),
    no_recache: bool = typer.Option(
        False,
        "--no-recache",
        help="Deprecated no-op: the missing-apps check now reads app availability "
        "live from the bench, so it never re-caches. Kept for backward compatibility.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompts on BOTH the normal and "
        "--receive restore paths (the destructive-restore confirmation and the "
        "missing-apps prompt); a non-TTY without --yes refuses these with a "
        "non-zero exit. Does not remove the sendme-ticket or MariaDB-credential "
        "prompts.",
    ),
    no_migrate: bool = typer.Option(
        False,
        "--no-migrate",
        help="Skip the post-restore 'bench migrate' and instance restart. By "
        "default a successful restore is followed by 'bench migrate' (to bring "
        "the restored DB to the code's schema) and an instance restart.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Restore a site from a backup with interactive selection.

    This command scans all available backups across all sites in the bench,
    presents them in an interactive menu grouped by the target site, and
    executes the restore using 'bench restore'.

    If --site is not provided, the default site is used, resolved from either
    common_site_config.json's `default_site` or sites/currentsite.txt.

    After a successful restore, `bench migrate` is run and the instance is
    restarted (skip with --no-migrate).

    Use --send to share a backup via P2P transfer, or --receive to restore from a remote backup.

    Examples:
        cwcli restore my-project --site example.com
        cwcli restore my-project  # Uses default site
        cwcli restore my-project --send  # Send a backup via sendme
        cwcli restore my-project --receive  # Receive and restore from sendme
    """
    # Validate mutually exclusive flags
    if send and receive:
        stderr_console.print(
            "[bold red]Error:[/bold red] Cannot use --send and --receive together."
        )
        raise typer.Exit(code=1)

    # The --latest/--backup-file selectors only make sense on the normal restore
    # path (they bypass the interactive backup menu, which is not reached under
    # --send or --receive). They are mutually exclusive with each other and with
    # the sendme modes.
    if latest and backup_file is not None:
        stderr_console.print(
            "[bold red]Error:[/bold red] Cannot use --latest and --backup-file together; "
            "choose one non-interactive selector."
        )
        raise typer.Exit(code=1)
    if (latest or backup_file is not None) and (send or receive):
        stderr_console.print(
            "[bold red]Error:[/bold red] --latest/--backup-file only apply to the normal "
            "restore path, not --send or --receive."
        )
        raise typer.Exit(code=1)

    # Resolve which bench to restore into (--bench/--path, else the single bench,
    # else error on multi-bench ambiguity). Applies to all three modes below; a
    # None result (no cache yet) lets each mode's own inspect/default fallback run.
    bench_path = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)

    # Handle sendme modes
    if send or receive:
        # Ensure sendme is installed
        if not ensure_sendme_installed(verbose=verbose):
            stderr_console.print(
                "[bold red]Error:[/bold red] sendme is required for remote backup transfers."
            )
            raise typer.Exit(code=1)

        if receive:
            # Receive mode doesn't need containers initially
            return restore_receive_mode(
                project_name,
                site,
                bench_path,
                mariadb_root_username,
                mariadb_root_password,
                admin_password,
                no_recache,
                verbose,
                yes,
                no_migrate,
            )

    # Ensure containers are running (needed for both normal restore and send mode)
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

    # Handle send mode now that we have the container
    if send:
        return restore_send_mode(project_name, site, bench_path, frappe_container, verbose)

    # Get bench path from cache or use provided path
    if not bench_path:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            bench_path = resolve_bench_path(project_name, None, None, verbose=verbose)
            if bench_path:
                if verbose:
                    stderr_console.print(f"[dim]Using cached bench path: {bench_path}[/dim]")
            else:
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                )
        else:
            # No cache found, run inspect to populate it
            stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")

            try:
                # Run inspect to populate cache (it has its own spinner)
                from .inspect import inspect as inspect_cmd_func

                # Call inspect directly with just the parameters it needs
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
                bench_path = resolve_bench_path(project_name, None, None, verbose=verbose)
                if bench_path:
                    if verbose:
                        stderr_console.print(
                            f"[dim]Using cached bench path from inspect: {bench_path}[/dim]"
                        )
                else:
                    # Still no cache, use default
                    bench_path = "/workspace/frappe-bench"
                    stderr_console.print(
                        f"[yellow]Warning: Could not detect bench path. Using default: {bench_path}[/yellow]"
                    )
            except typer.Exit:
                raise
            except Exception as e:
                # Inspect failed, use default
                bench_path = "/workspace/frappe-bench"
                stderr_console.print(
                    f"[yellow]Warning: Inspect failed. Using default bench path: {bench_path}[/yellow]"
                )
                if verbose:
                    stderr_console.print(f"[dim]Inspect error: {e}[/dim]")

    # Get default site if not provided. Resolve from EITHER
    # common_site_config.json's default_site OR sites/currentsite.txt (a plain
    # `bench use`d dev bench only has the latter).
    if not site:
        try:
            default_site = _resolve_default_site(
                project_name, bench_path, frappe_container, verbose=verbose
            )
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

    # Scan backups
    show_tips = config_utils.get_show_tips()
    with TipSpinner("Scanning backups for all sites", console=stderr_console, enabled=show_tips):
        backups = scan_backups_for_all_sites(frappe_container, bench_path, verbose)

    # Group and sort backups (even if empty, we can still restore from remote)
    target_backups, other_backups = group_and_sort_backups(backups, site)

    # Resolve a backup set. The non-interactive selectors (--latest/--backup-file)
    # bypass the menu so an agent or script can pick a backup without a TTY. A
    # non-TTY WITHOUT a selector must refuse rather than silently taking the
    # menu's `questionary.select().ask() -> None` path and exiting 0 having done
    # nothing. The sentinel {"_restore_from_ticket": True} (remote restore via
    # sendme) is reachable ONLY through the interactive menu; the selector branch
    # can never produce it (correct: remote restore is reached via --receive).
    if latest or backup_file is not None:
        selected_backup = select_backup_set(
            target_backups, other_backups, latest=latest, backup_file=backup_file
        )
        if selected_backup is None:
            selector_desc = "--latest" if latest else f"--backup-file {backup_file!r}"
            stderr_console.print(
                f"[bold red]Error:[/bold red] no backup matched {selector_desc} for site "
                f"'{site}'."
            )
            stderr_console.print(
                f"[dim]Tip: run 'cwcli inspect {project_name}' to check the project's "
                "state (a non-interactive session cannot browse backups interactively).[/dim]"
            )
            raise typer.Exit(code=1)
    elif not sys.stdin.isatty():
        stderr_console.print(
            "[bold red]Error:[/bold red] non-interactive session and no backup selector; "
            "pass --latest or --backup-file <name> to pick a backup without a menu."
        )
        raise typer.Exit(code=1)
    else:
        # display_backup_selection_menu already prints "Restore cancelled." itself
        # on a None result; do not print it again here.
        selected_backup = display_backup_selection_menu(target_backups, other_backups, site)
        if selected_backup is None:
            raise typer.Exit(code=1)

    # Check if user selected to restore from remote source
    if selected_backup.get("_restore_from_ticket"):
        # Ensure sendme is installed
        if not ensure_sendme_installed(verbose=verbose):
            stderr_console.print(
                "[bold red]Error:[/bold red] sendme is required for remote backup transfers."
            )
            raise typer.Exit(code=1)

        # Call restore_receive_mode
        return restore_receive_mode(
            project_name,
            site,
            bench_path,
            mariadb_root_username,
            mariadb_root_password,
            admin_password,
            no_recache,
            verbose,
            yes,
            no_migrate,
        )

    # Check for missing apps before proceeding with restore: does the SELECTED
    # backup need apps this bench does not physically have?
    console.print()
    with TipSpinner("Checking for missing apps", console=stderr_console, enabled=show_tips):
        missing_apps = check_missing_apps(
            frappe_container,
            project_name,
            bench_path,
            selected_backup["database"]["full_path"],
            verbose=verbose,
            no_recache=no_recache,
        )

    if missing_apps:
        console.print()
        console.print(
            "[bold yellow]⚠ Warning:[/bold yellow] The following apps are installed on the backup site but not available on this bench:"
        )
        for app in missing_apps:
            console.print(f"  • {app}")
        console.print()
        console.print(
            "[dim]You may need to install these apps before restoring to avoid errors.[/dim]"
        )
        console.print(
            f"[dim]Install apps with: bench get-app <app-name> && bench --site {site} install-app <app-name>[/dim]"
        )
        console.print()

        # Gate the missing-apps "continue anyway?" prompt with --yes, mirroring the
        # receive path's contract: --yes proceeds, a non-TTY without --yes refuses
        # non-zero, an interactive TTY asks. The confirm below keeps auto-enter
        # DISABLED (load-bearing): it must consume its own trailing Enter or the
        # keystroke leaks into the following credential prompt (prompt_toolkit
        # internal buffer), so this block is NOT a candidate for
        # commands/utils.py:confirm_or_exit (which omits that setting).
        if yes:
            console.print("[dim]Proceeding despite missing apps (--yes).[/dim]")
        elif not sys.stdin.isatty():
            stderr_console.print(
                "[bold red]Error:[/bold red] Refusing to restore with missing apps "
                "without confirmation. Re-run with --yes to proceed non-interactively."
            )
            raise typer.Exit(code=1)
        else:
            try:
                proceed = questionary.confirm(
                    "Do you want to continue with the restore anyway?",
                    default=False,
                    auto_enter=False,
                ).ask()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1) from None

            if not proceed:
                console.print("[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1)

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

    # Gate the destructive-restore confirm with --yes, same shape as the receive
    # path: --yes proceeds, a non-TTY without --yes refuses non-zero, an
    # interactive TTY asks. Decline/Ctrl-C now exit non-zero (was Exit(0)).
    # The confirm keeps auto-enter DISABLED too (see missing-apps block above).
    if yes:
        console.print("[dim]Proceeding without confirmation (--yes).[/dim]")
    elif not sys.stdin.isatty():
        stderr_console.print(
            "[bold red]Error:[/bold red] Refusing to restore without confirmation. "
            "Re-run with --yes to restore non-interactively."
        )
        raise typer.Exit(code=1)
    else:
        try:
            confirm = questionary.confirm(
                "Are you sure you want to restore?", default=False, auto_enter=False
            ).ask()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Restore cancelled.[/yellow]")
            raise typer.Exit(code=1) from None

        if not confirm:
            console.print("[yellow]Restore cancelled.[/yellow]")
            raise typer.Exit(code=1)

    # Resolve MariaDB credentials AFTER the confirm. Interactive: prompt for the
    # username (default root) and password, actually collecting input; the username
    # prompt was previously missing entirely and the password was never collected.
    # Non-interactive: use the --mariadb-root-* flags; a non-TTY without a password
    # refuses rather than proceeding empty.
    mariadb_root_username, mariadb_root_password = _prompt_mariadb_credentials(
        mariadb_root_username, mariadb_root_password
    )

    # Defense-in-depth: reject a username with shell-special characters (it is also
    # shlex-quoted below, so this can never inject - it just fails fast on a typo).
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

    # Secrets are passed via the environment (never on the argv) so they do not
    # appear in the container process list (`ps`/`docker top`/exec-inspect) - M5.
    restore_env: dict[str, str] = {}
    cmd = f"bench --site {shlex.quote(site)} restore {shlex.quote(backup_file)}"

    # Add database credentials (username is always resolved - defaults to root).
    cmd += f" --mariadb-root-username {shlex.quote(mariadb_root_username)}"

    restore_env["CWCLI_MARIADB_ROOT_PASSWORD"] = mariadb_root_password
    cmd += ' --mariadb-root-password "$CWCLI_MARIADB_ROOT_PASSWORD"'
    cmd += " --force"  # Bypass version check prompts for non-interactive restore

    if admin_password:
        restore_env["CWCLI_ADMIN_PASSWORD"] = admin_password
        cmd += ' --admin-password "$CWCLI_ADMIN_PASSWORD"'

    # Add file restore flags if available
    if selected_backup["files"]:
        files_path = selected_backup["files"]["full_path"]
        cmd += f" --with-public-files {shlex.quote(files_path)}"

    if selected_backup["private_files"]:
        private_files_path = selected_backup["private_files"]["full_path"]
        cmd += f" --with-private-files {shlex.quote(private_files_path)}"

    if verbose:
        # Secrets live in the environment, so the command itself is safe to print.
        stderr_console.print(f"[dim]$ {cmd}[/dim]")

    # Execute restore with spinner for clean output
    console.print()
    with TipSpinner(f"Restoring site '{site}'", console=stderr_console, enabled=show_tips):
        exit_code, output = frappe_container.exec_run(
            ["sh", "-c", cmd], workdir=bench_path, environment=restore_env
        )

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

        # Migrate the restored DB to the code's schema, then restart the instance
        # so it comes back up cleanly (skippable with --no-migrate).
        if not no_migrate:
            migrate_ok = _post_restore_migrate_and_restart(
                frappe_container, project_name, bench_path, site, verbose=verbose
            )
            if not migrate_ok:
                raise typer.Exit(code=1)
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
