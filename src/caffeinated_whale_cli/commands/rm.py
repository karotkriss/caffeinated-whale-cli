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
from ..utils.config_utils import PROJECTS_DIR
from ..utils.console import console, stderr_console
from ..utils.docker_utils import (
    get_project_containers,
    get_project_volumes,
    handle_docker_errors,
)

app = typer.Typer(help="Remove a Frappe project and its containers.")

# Marker in a Frappe backup filename that identifies the database dump - the one
# artifact a "backup" cannot be trusted without (Frappe names it
# ``<timestamp>-<site>-database.sql.gz``). Matched case-insensitively so an
# uncompressed ``.sql`` variant is still recognised.
_DB_DUMP_MARKER = "database.sql"


def _is_safe_project_dir(project_dir: Path) -> bool:
    """
    Return True only if ``project_dir`` resolves to a path strictly inside
    ``PROJECTS_DIR`` (a real child, never ``PROJECTS_DIR`` itself or an ancestor).

    This is the last-line guard against a ``project_name`` that escapes the
    projects root once joined - ``PROJECTS_DIR / ".."`` resolves to the parent
    ``~/.cwcli`` that holds every project plus the cache and config, and an
    absolute component resets the join entirely. Any ``rmtree``/archive of an
    escaped path could wipe unrelated data, so callers must gate on this before
    touching the filesystem.
    """
    try:
        root = PROJECTS_DIR.resolve()
        target = project_dir.resolve()
    except OSError:
        return False
    return target != root and root in target.parents


def _is_valid_project_name(name: str) -> bool:
    """
    Reject project names that are not a single, safe directory entry under
    ``PROJECTS_DIR``.

    A project name is only ever one directory beneath the projects root. Empty,
    ``.``, ``..``, absolute, or separator-bearing names let a ``pathlib`` join
    escape that root (see :func:`_is_safe_project_dir`), so ``cwcli rm ..`` could
    otherwise archive-and-``rmtree`` the entire ``~/.cwcli`` tree. Validate before
    any filesystem or volume operation.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if Path(name).is_absolute():
        return False
    return _is_safe_project_dir(PROJECTS_DIR / name)


def _list_sites(container, bench_path: str) -> list[str] | None:
    """
    Return the real Frappe sites under ``{bench_path}/sites``.

    A bench ``sites/`` directory holds more than sites: ``apps.txt``,
    ``apps.json``, ``assets``, ``common_site_config.json``, ``currentsite.txt``
    (written by ``bench use``), lock files, and so on. A real site is a directory
    that contains a ``site_config.json``, so detect sites by probing for that file
    rather than denylisting known non-site names - a denylist can never be
    complete, and any unlisted entry (e.g. ``currentsite.txt``) would be mistaken
    for a site, fail its ``bench backup``, and wrongly block removal.

    Detection is deliberately FAIL-SAFE: an entry is excluded only when we can
    positively confirm it is not a site - a non-directory, or a readable directory
    with no ``site_config.json``. Anything ambiguous (the probe erroring, an
    unreadable directory, or unexpected output) is treated as a real site that
    must be backed up, so a transiently unreadable/erroring ``site_config.json``
    can never let a site's volume be deleted with no backup. This keeps C1's
    guarantee fail-closed under ambiguity: worst case it blocks a delete (which
    ``--no-backup`` can override), never loses data.

    Returns the list of site names (possibly empty), or ``None`` if the sites
    directory itself could not be listed.
    """
    exit_code, output = container.exec_run(f"ls -1 {bench_path}/sites")
    if exit_code != 0:
        return None

    sites: list[str] = []
    for entry in output.decode("utf-8").split("\n"):
        entry = entry.strip()
        if not entry:
            continue
        entry_dir = f"{bench_path}/sites/{entry}"
        # One shell probe with three positive verdicts: SITE (has
        # site_config.json), NOTASITE (not a dir, or a readable dir with no
        # config), AMBIGUOUS (dir exists but is unreadable so we cannot confirm).
        probe = (
            f'sh -c \'if [ ! -d "{entry_dir}" ]; then echo NOTASITE; '
            f'elif [ -f "{entry_dir}/site_config.json" ]; then echo SITE; '
            f'elif [ -r "{entry_dir}" ]; then echo NOTASITE; '
            f"else echo AMBIGUOUS; fi'"
        )
        probe_code, probe_out = container.exec_run(probe)
        verdict = probe_out.decode("utf-8").strip() if probe_code == 0 else ""
        # Exclude ONLY on a positive NOTASITE. SITE, AMBIGUOUS, an unexpected
        # token, empty output, or a non-zero probe exit all fail closed -> site.
        if verdict != "NOTASITE":
            sites.append(entry)
    return sites


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
        True only if EVERY site was fully backed up with its database dump
        confirmed present and non-empty on the host archive directory; False if
        any site's backup failed or its dump did not land. A partial success is
        reported as False so the caller never destroys data for a site whose
        backup is missing.
    """
    try:
        # Get the list of real sites (directories with a site_config.json).
        sites = _list_sites(container, bench_path)

        if sites is None:
            if verbose:
                stderr_console.print(
                    f"[dim]VERBOSE: Could not list sites directory: {bench_path}/sites[/dim]"
                )
            return False

        if not sites:
            if verbose:
                stderr_console.print("[dim]VERBOSE: No sites found to backup[/dim]")
            return True

        # Create backups directory
        backups_dir = archive_dir / "backups"
        backups_dir.mkdir(exist_ok=True)

        backed_up_count = 0
        failed_sites: list[str] = []
        for site in sites:
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Backing up site '{site}'...[/dim]")

            # Run bench backup command for the site
            backup_cmd = f"cd {bench_path} && bench --site {site} backup --with-files"
            exit_code, output = container.exec_run(f"sh -c '{backup_cmd}'", workdir=bench_path)

            if exit_code != 0:
                stderr_console.print(f"[yellow]Warning:[/yellow] Could not backup site '{site}'")
                if verbose:
                    stderr_console.print(
                        f"[dim]VERBOSE: Backup command output: {output.decode('utf-8')}[/dim]"
                    )
                failed_sites.append(site)
                continue

            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Backup created for '{site}'[/dim]")

            # A zero exit from `bench backup` only means the dump was written
            # INSIDE the container (i.e. inside the volume we are about to
            # delete). It is not a backup until the bytes are copied out to the
            # host archive, so verify each expected artifact actually lands here
            # and, critically, that the database dump is present and non-empty.
            site_backup_dir = f"{bench_path}/sites/{site}/private/backups"
            exit_code, ls_output = container.exec_run(f"ls -1t {site_backup_dir}")

            if exit_code != 0:
                stderr_console.print(
                    f"[yellow]Warning:[/yellow] Could not list backups for site '{site}'"
                )
                failed_sites.append(site)
                continue

            backup_files = [f.strip() for f in ls_output.decode("utf-8").split("\n") if f.strip()]

            # Copy the most recent backups
            site_archive_backups = backups_dir / site
            site_archive_backups.mkdir(exist_ok=True)

            db_dump_saved = False
            copy_failed = False
            # Get the top 5 (newest-first) files to capture the whole newest set
            # (database, site config, files, private-files).
            for backup_file in backup_files[:5]:
                source_path = f"{site_backup_dir}/{backup_file}"

                # Use docker exec `cat` to copy the file out.
                exit_code, file_data = container.exec_run(f"cat {source_path}")

                if exit_code != 0:
                    # An artifact we could not copy out. Fail closed: a backup
                    # missing any of its parts is not a trustworthy backup.
                    copy_failed = True
                    if verbose:
                        stderr_console.print(
                            f"[dim]VERBOSE: Could not copy {backup_file} to archive[/dim]"
                        )
                    continue

                dest_file = site_archive_backups / backup_file
                dest_file.write_bytes(file_data or b"")

                # Confirm the artifact actually landed on the host. The database
                # dump additionally must be non-empty - an empty dump is no backup
                # at all (other artifacts, e.g. a files tar, may legitimately be
                # small, so only the dump is size-checked).
                try:
                    size = dest_file.stat().st_size
                except OSError:
                    size = 0

                if _DB_DUMP_MARKER in backup_file.lower() and size > 0:
                    db_dump_saved = True

                if verbose:
                    stderr_console.print(f"[dim]VERBOSE: Copied {backup_file} to archive[/dim]")

            if db_dump_saved and not copy_failed:
                backed_up_count += 1
            else:
                stderr_console.print(
                    f"[yellow]Warning:[/yellow] Backup for site '{site}' was not fully copied to "
                    "the host archive (missing or empty database dump)"
                )
                failed_sites.append(site)

        if backed_up_count > 0:
            console.print(f"  [dim]Backed up {backed_up_count} site(s) to {backups_dir}[/dim]")

        # Success requires EVERY site to have a confirmed dump. Any failure means
        # the caller must not delete data that was not safely captured.
        return not failed_sites

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

        # Archive site_config.json files from all real sites.
        sites_dir = f"{bench_path}/sites"
        sites = _list_sites(container, bench_path)

        if sites:
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


def _remove_named_volumes(
    project_name: str,
    verbose: bool = False,
    status=None,
    failures: list[str] | None = None,
) -> int:
    """
    Remove the named Docker Compose volumes for a project.

    ``Container.remove(v=True)`` only removes a container's *anonymous* volumes.
    The named volumes that frappe-docker creates (e.g. ``sites``, ``db-data``)
    are labeled with the compose project and must be removed explicitly, or the
    databases and sites survive removal despite what the user was told.

    Args:
        project_name: Name of the project whose volumes should be removed
        verbose: Enable verbose output
        status: Status context for spinner
        failures: Optional list to append human-readable step failures to, so the
            caller can report an accurate (non-zero) outcome when a volume could
            not be enumerated or removed.

    Returns:
        Number of named volumes removed.
    """
    volumes = get_project_volumes(project_name)

    if volumes is None:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not enumerate volumes for '{project_name}'; "
            "some named volumes may remain."
        )
        if failures is not None:
            failures.append(f"could not enumerate named volumes for '{project_name}'")
        return 0

    if not volumes:
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: No named volumes found for '{project_name}'[/dim]")
        return 0

    removed = 0
    for volume in volumes:
        try:
            if status:
                status.update(f"[bold red]Removing volume '{volume.name}'...[/bold red]")
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Removing volume '{volume.name}'[/dim]")
            volume.remove(force=True)
            removed += 1
        except Exception as e:
            stderr_console.print(
                f"[yellow]Warning:[/yellow] Could not remove volume '{volume.name}': {e}"
            )
            if failures is not None:
                failures.append(f"could not remove volume '{volume.name}'")
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Exception: {e}[/dim]")

    if removed > 0:
        console.print(f"  [dim]Removed {removed} named volume(s) for '{project_name}'[/dim]")

    return removed


def _archive_project_directory(
    project_name: str,
    archive_dir: Path | None = None,
    verbose: bool = False,
) -> bool:
    """
    Archive the project's cwcli-managed configuration into ``archive_dir`` before
    the local project directory is deleted.

    Only the small, reliable ``conf/`` subdirectory of
    ``~/.cwcli/projects/{project_name}/`` is archived - it holds the generated
    ``docker-compose.yml``, which is the cwcli instance config and the one thing
    not recoverable from elsewhere. The rest of that directory is the
    frappe-docker devcontainer bind mount: a multi-hundred-MB ``frappe-bench``
    whose virtualenv and node_modules contain dangling symlinks that do not
    resolve on the host. Copying the whole tree both wastes space (its databases
    and files are already captured by the live ``bench backup`` into this same
    archive directory) and makes ``copytree`` raise on those dangling symlinks,
    which previously aborted removal and left the named volume and project
    directory behind - reintroducing issue #19 in real conditions.

    The database/files safety net is therefore the ``bench backup`` output (under
    ``archive_dir/backups/``); ``conf/`` is the config safety net archived here.

    Args:
        project_name: Name of the project whose configuration should be archived
        archive_dir: Destination archive directory for the copy
        verbose: Enable verbose output

    Returns:
        True if the copy succeeded or there was nothing to archive, False only
        if the copy itself failed (so the caller can refuse to delete data that
        was not safely archived).
    """
    project_dir = PROJECTS_DIR / project_name

    # Last-line guard: never read/copy from a path that escapes the projects
    # root. Refusing (False) makes the caller skip deletion, not proceed.
    if not _is_safe_project_dir(project_dir):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Refusing to archive '{project_dir}': path is outside "
            f"the projects directory ({PROJECTS_DIR})."
        )
        return False

    if not project_dir.exists() or archive_dir is None:
        if verbose and not project_dir.exists():
            stderr_console.print(
                f"[dim]VERBOSE: No project directory to archive at {project_dir}[/dim]"
            )
        return True

    conf_dir = project_dir / "conf"
    if not conf_dir.is_dir():
        # No cwcli-managed config to preserve (older or partial layout). There is
        # nothing small and reliable to archive, so report success and let the
        # caller proceed; the database/files safety net is the bench backup.
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: No conf/ directory to archive at {conf_dir}[/dim]")
        return True

    try:
        dest = archive_dir / "project_files" / "conf"
        # symlinks=True + ignore_dangling_symlinks keeps the copy robust even if a
        # config dir ever contains a (possibly dangling) symlink: links are copied
        # as links rather than followed, so copytree never raises on them.
        shutil.copytree(
            conf_dir,
            dest,
            dirs_exist_ok=True,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Archived project config to {dest}[/dim]")
        return True
    except Exception as e:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not archive project configuration for "
            f"'{project_name}': {e}"
        )
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Exception: {e}[/dim]")
        return False


def _delete_project_directory(
    project_name: str,
    verbose: bool = False,
    failures: list[str] | None = None,
) -> bool:
    """
    Delete the project's local directory at ``~/.cwcli/projects/{project_name}/``.

    Must only be called after :func:`_archive_project_directory` has succeeded.
    Without this step ``cwcli rm`` leaves the project lingering on disk - the
    bug reported in issue #19.

    Args:
        project_name: Name of the project to remove the directory for
        verbose: Enable verbose output
        failures: Optional list to append a human-readable failure to when the
            ``rmtree`` itself fails (as opposed to there being nothing to remove),
            so the caller can report a non-zero outcome.

    Returns:
        True if a directory was removed, False if there was nothing to remove
        or removal failed.
    """
    project_dir = PROJECTS_DIR / project_name

    # Last-line guard before ``rmtree``: never delete a path that resolves
    # outside the projects root. ``PROJECTS_DIR / ".."`` would otherwise wipe the
    # entire ``~/.cwcli`` tree (every project, the cache, config).
    if not _is_safe_project_dir(project_dir):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Refusing to delete '{project_dir}': path is outside "
            f"the projects directory ({PROJECTS_DIR})."
        )
        if failures is not None:
            failures.append(
                f"refused to delete path outside projects directory for '{project_name}'"
            )
        return False

    if not project_dir.exists():
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: No project directory to remove at {project_dir}[/dim]"
            )
        return False

    try:
        shutil.rmtree(project_dir)
        console.print(f"  [dim]Removed project directory {project_dir}[/dim]")
        return True
    except Exception as e:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not remove project directory '{project_dir}': {e}"
        )
        if failures is not None:
            failures.append(f"could not remove project directory for '{project_name}'")
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Exception: {e}[/dim]")
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
        A result dict with keys ``found`` (bool), ``orphan`` (bool, no
        containers but volumes/dir remained), ``containers`` (int removed),
        ``volumes`` (int removed), ``dir_removed`` (bool), ``backup_ok`` (bool -
        True unless a backup was attempted and did not fully succeed), and
        ``failures`` (list[str] - non-empty means a requested step failed and the
        caller must exit non-zero).
    """
    result: dict = {
        "found": False,
        "orphan": False,
        "containers": 0,
        "volumes": 0,
        "dir_removed": False,
        "backup_ok": True,
        "failures": [],
    }

    # Defense-in-depth: never operate on a name that escapes the projects root.
    # The CLI already filters these, but guard here too so no internal caller can
    # reach a destructive path with a ``.``/``..``/absolute/separator name.
    if not _is_valid_project_name(project_name):
        stderr_console.print(
            f"[bold red]Error:[/bold red] Refusing to remove invalid project name "
            f"{project_name!r}."
        )
        result["failures"].append(f"invalid project name {project_name!r}")
        return result

    containers = get_project_containers(project_name)

    # None means a Docker connection error (distinct from an empty list). Do not
    # attempt destructive cleanup when we cannot even see the project.
    if containers is None:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Could not connect to Docker to inspect "
            f"'{project_name}'."
        )
        result["failures"].append(f"could not connect to Docker to inspect '{project_name}'")
        return result

    project_dir = PROJECTS_DIR / project_name
    dir_existed = project_dir.exists()

    is_orphan = not containers
    if is_orphan:
        # No containers left. The project may still have orphaned named volumes
        # and/or a lingering local directory (a prior partial rm, an older
        # cwcli, or a manual `docker rm`). Clean those up rather than refusing.
        # If there is genuinely nothing left, treat it as a typo / not found.
        orphan_volumes = get_project_volumes(project_name)
        if not dir_existed and not orphan_volumes:
            stderr_console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
            return result
        result["orphan"] = True

    result["found"] = True

    if verbose:
        stderr_console.print(
            f"[dim]VERBOSE: Found {len(containers)} container(s) for '{project_name}'[/dim]"
        )

    # Create a single archive directory for this removal. Database backups,
    # config archives, and a copy of the local project directory all land here
    # so nothing is deleted without a safety copy first.
    archive_base = Path.home() / ".cwcli" / "archive"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = archive_base / f"{project_name}_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Find frappe container for archiving and backup
    frappe_container = None
    for container in containers:
        if container.labels.get("com.docker.compose.service") == "frappe":
            frappe_container = container
            break

    # A live `bench backup` and the container-side config archive both shell into
    # the frappe container via exec_run, which only works while it is running. A
    # stopped frappe container is a normal rm case (the user simply never started
    # it, or stopped it): do NOT attempt those exec-based steps against it - they
    # would only emit confusing "could not backup/archive" warnings. The conf/
    # safety net is copied host-side by _archive_project_directory below.
    frappe_running = frappe_container is not None and frappe_container.status == "running"

    # Backup and archive before removal
    if frappe_running:
        # Try to get bench path from cache first
        bench_path = "/workspace/frappe-bench"  # default
        try:
            cached_data = db_utils.get_cached_project_data(project_name)
            if cached_data and cached_data.get("bench_instances"):
                bench_path = cached_data["bench_instances"][0]["path"]
        except Exception:
            pass

        # Backup databases (unless --no-backup). The result is the gate on
        # destroying data: a backup that did not fully land on the host archive
        # must NOT be trusted, so a False here blocks volume/directory removal
        # below just as a failed conf/ archive does.
        if not no_backup:
            if status:
                status.update(
                    f"[bold cyan]Backing up databases for '{project_name}'...[/bold cyan]"
                )
            result["backup_ok"] = _backup_sites(
                project_name, frappe_container, bench_path, archive_dir, verbose=verbose
            )

        # Archive configuration
        if status:
            status.update(f"[bold cyan]Archiving configuration for '{project_name}'...[/bold cyan]")
        _archive_project_config(project_name, frappe_container, bench_path, verbose=verbose)
    else:
        # No running container, so a live `bench backup` database dump is
        # impossible (whether the containers are stopped or already gone). Only
        # the host-side conf/ archive can be preserved.
        stderr_console.print(
            f"[yellow]Warning:[/yellow] No container was running for '{project_name}', "
            "so a fresh database backup could not be taken before cleanup."
        )

    # A verified live backup is the gate on destroying the named volumes. Evaluate
    # it BEFORE removing any container so a failed backup aborts while the frappe
    # container is still alive and a retry can still produce a backup. Removing
    # containers first and only THEN blocking on the backup would leave an orphan:
    # on the naive retry (`cwcli rm proj` again) there is no running container, so
    # no backup is attempted, backup_ok defaults True, the gate passes, and the
    # database volumes are deleted with NO backup - defeating C1. The gate is
    # scoped to volume deletion: under --no-volumes no volume data is destroyed, so
    # a failed backup does not abort; --no-backup opts out of the gate entirely.
    backup_failed = remove_volumes and not no_backup and not result["backup_ok"]
    if backup_failed:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Refusing to remove '{project_name}': a verified "
            "database backup could not be created."
        )
        stderr_console.print(
            "[dim]Resolve the backup failure and retry, or use --no-backup to remove "
            "without a backup (the databases will be lost).[/dim]"
        )
        message = f"a verified database backup could not be created for '{project_name}'"
        result["failures"].append(message)
        return result

    # Stop and remove each container
    removed_count = 0
    container_removal_failed = False
    for container in containers:
        # Bind a fallback name BEFORE reading ``container.name`` so the except
        # handler can never hit an unbound ``container_name`` (a NameError):
        # ``container.name`` is a docker-py property that can itself raise (e.g. a
        # KeyError on missing attrs), and that raise must surface as a recorded
        # container-removal failure, not a crash.
        container_name = "<unknown>"
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
            container_removal_failed = True
            result["failures"].append(f"failed to remove container '{container_name}'")
            stderr_console.print(
                f"[bold red]Error:[/bold red] Failed to remove container '{container_name}': {e}"
            )
            if verbose:
                stderr_console.print(f"[dim]VERBOSE: Exception: {e}[/dim]")

    result["containers"] = removed_count

    # Archive the local project directory BEFORE deleting anything. If the copy
    # fails we refuse to delete the named volumes or the directory, so nothing
    # that was not safely archived is destroyed.
    if status:
        status.update(f"[bold cyan]Archiving project directory for '{project_name}'...[/bold cyan]")
    archived_ok = _archive_project_directory(project_name, archive_dir=archive_dir, verbose=verbose)

    # Destroying data (the named volumes and the local directory) is gated on two
    # remaining safety conditions here. If either did not hold, skip the
    # destructive steps so nothing unrecoverable is lost:
    #   1. every container was removed cleanly (a caught container-removal error
    #      must not fall through to volume/dir destruction), and
    #   2. the conf/ config archive succeeded.
    # The third condition - a verified live database backup - is enforced EARLIER,
    # as an abort before any container is removed (see above), so a failed backup
    # never reaches this late gate and never tears down the containers a retry
    # would need.
    archive_failed = dir_existed and not archived_ok
    gate_blocked = container_removal_failed or archive_failed

    if gate_blocked:
        reasons = []
        if container_removal_failed:
            reasons.append("one or more containers could not be removed")
        if archive_failed:
            reasons.append("its configuration could not be archived")
        message = (
            f"Skipping volume and directory removal for '{project_name}' because "
            + " and ".join(reasons)
            + "."
        )
        stderr_console.print(f"[yellow]Warning:[/yellow] {message}")
        result["failures"].append(message)
    else:
        # Remove named compose volumes. Anonymous volumes were already handled by
        # container.remove(v=remove_volumes) above, but the named volumes that
        # hold the databases and sites must be removed explicitly or the data
        # survives.
        if remove_volumes:
            if status:
                status.update(f"[bold red]Removing volumes for '{project_name}'...[/bold red]")
            result["volumes"] = _remove_named_volumes(
                project_name, verbose=verbose, status=status, failures=result["failures"]
            )

        # Remove the local project directory (the cwcli instance of the
        # project). This is deleted regardless of --no-volumes: it is config,
        # not data, and leaving it behind is the lingering-project bug from
        # issue #19.
        if status:
            status.update(
                f"[bold red]Removing project directory for '{project_name}'...[/bold red]"
            )
        result["dir_removed"] = _delete_project_directory(
            project_name, verbose=verbose, failures=result["failures"]
        )

    # Clear the cache only when NO step failed (`result["failures"]` empty). This
    # covers both the gate-blocked case AND a post-gate failure inside the else
    # branch - a named-volume removal that raised, a volume enumeration that
    # errored, or a directory removal that failed all append to `failures`. In any
    # of those the data-bearing volume or directory may survive, so keep the cache
    # entry: the half-removed project stays visible in `ls`/`inspect` and can be
    # retried, rather than vanishing while its data still occupies disk. On a clean
    # full removal, orphan cleanup, or --no-volumes removal, `failures` is empty
    # and the cache is cleared as before.
    if not result["failures"] and (
        removed_count > 0 or result["volumes"] > 0 or result["dir_removed"]
    ):
        if verbose:
            stderr_console.print(f"[dim]VERBOSE: Clearing cache for '{project_name}'[/dim]")
        db_utils.clear_cache_for_project(project_name)

    return result


def _frappe_container_running(project_name: str) -> bool:
    """
    Report whether the project's ``frappe`` service container is currently running.

    Used to decide whether a pre-removal recache is worthwhile: the recache exists
    only to refresh site info for a live ``bench backup``, which is impossible when
    nothing is running. A return of False also keeps ``rm`` from entering the
    recache spinner on a stopped project, which is where an interactive
    "start the containers?" prompt would otherwise be trapped under the spinner.

    Returns True only if a ``frappe`` service container exists and reports status
    ``running``; False otherwise (no containers, a Docker error, or stopped).
    """
    containers = get_project_containers(project_name)
    if not containers:
        return False
    for container in containers:
        if container.labels.get("com.docker.compose.service") == "frappe":
            return bool(container.status == "running")
    return False


def _recover_trailing_flags(
    names: list[str] | None,
    verbose: bool,
    yes: bool,
    no_backup: bool,
    volumes: bool,
) -> tuple[list[str], bool, bool, bool, bool]:
    """
    Split project names from option flags that trailed the variadic argument.

    A variadic ``typer.Argument`` greedily consumes options that follow it, so
    ``cwcli rm myproj --yes`` would otherwise treat ``--yes`` as a second project
    name (and then prompt and try to remove a project literally named "--yes").
    Recover the common flags from the name list so flag order is forgiving.

    Returns the project names with flags removed, followed by the (possibly
    updated) ``verbose``, ``yes``, ``no_backup`` and ``volumes`` values.
    """
    projects: list[str] = []
    for name in names or []:
        if name in ("-v", "--verbose"):
            verbose = True
        elif name in ("-y", "--yes"):
            yes = True
        elif name == "--no-backup":
            no_backup = True
        elif name == "--volumes":
            volumes = True
        elif name == "--no-volumes":
            volumes = False
        else:
            projects.append(name)
    return projects, verbose, yes, no_backup, volumes


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
    - Removes all named Docker volumes for the project (deletes all data!)
    - Deletes the local project directory (~/.cwcli/projects/{name}/)
    - Clears the project from the cache

    Use --no-volumes to keep volumes:
    - Keeps the named Docker volumes (preserves databases, sites, and files)
    - Still removes the containers, project directory, and cache entry

    When volumes are being deleted (the default --volumes), a backup that cannot
    be fully created and verified (e.g. a wedged site, the DB is down, or the disk
    is full) blocks removal: the command aborts BEFORE any container is removed and
    exits non-zero, so data is never destroyed without a confirmed backup. Aborting
    before container removal keeps the whole project intact (containers, volumes,
    directory, and cache), so a retry can still take a live backup from the running
    container - if the containers were torn down first, the retry would be an
    orphan with no live database to back up. Under --no-volumes no volume data is
    destroyed, so a failed backup does NOT block the container/directory/cache
    cleanup and does not force a non-zero exit.

    Use --no-backup to skip backups (not recommended):
    - Skips database backups (and the backup safety gate)
    - Skips recaching (faster but risky)

    Examples:
        cwcli rm my-project                 # Remove everything (with confirmation)
        cwcli rm my-project --no-volumes    # Keep volumes, remove containers only
        cwcli rm my-project --no-backup     # Skip backups (not recommended)
        cwcli rm my-project --yes           # Skip confirmation
        cwcli ls | cwcli rm                 # Remove multiple projects via pipe
    """
    project_names_to_process = []

    # Recover flags that trailed the project name(s). A variadic argument greedily
    # eats options that follow it, so `cwcli rm myproj --yes` would otherwise treat
    # `--yes` as a second project name. Make flag order forgiving instead.
    filtered_project_names, actual_verbose, yes, no_backup, volumes = _recover_trailing_flags(
        project_name, verbose, yes, no_backup, volumes
    )
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

    # Reject names that could escape PROJECTS_DIR before anything destructive
    # runs. A project name is only ever a single directory under the projects
    # root; ``.``, ``..``, absolute, or separator-bearing names let a path join
    # escape it, so ``cwcli rm ..`` could otherwise archive-and-rmtree the entire
    # ~/.cwcli tree. Drop invalid names with a clear error; if any was rejected
    # the command exits non-zero even when valid names remain.
    valid_names = []
    invalid_names = []
    for name in project_names_to_process:
        if _is_valid_project_name(name):
            valid_names.append(name)
        else:
            invalid_names.append(name)

    for bad in invalid_names:
        stderr_console.print(
            f"[bold red]Error:[/bold red] Refusing to remove invalid project name {bad!r}: "
            "a project name must be a single directory under the projects root "
            "(no '.', '..', path separators, or absolute paths)."
        )

    if not valid_names:
        raise typer.Exit(code=1)

    names_rejected = bool(invalid_names)
    project_names_to_process = valid_names

    # Re-cache projects if not skipping backups. The recache only refreshes site
    # info so a live `bench backup` is accurate, which is moot when nothing is
    # running. A stopped project is a normal rm case: skip the recache instead of
    # auto-starting containers we are about to delete (wasteful and surprising),
    # and avoid entering the spinner where a "start the containers?" prompt would
    # otherwise be trapped. rm still proceeds to archive conf/ and remove the
    # volumes and project directory.
    if not no_backup:
        console.print()
        console.print("[bold cyan]Preparing for removal...[/bold cyan]")
        for project in project_names_to_process:
            if not _frappe_container_running(project):
                stderr_console.print(
                    f"[dim]Containers for '{project}' are not running; skipping recache "
                    "(a live backup can only be taken from a running project).[/dim]"
                )
                continue

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
                "[dim]This removes the containers, the named Docker volumes (databases, "
                "sites, and files), and the local project directory.[/dim]"
            )
        else:
            console.print(
                "[bold yellow]Note:[/bold yellow] Named volumes will be preserved (--no-volumes flag set)."
            )
            console.print(
                "[dim]The containers and the local project directory are still removed, but the "
                "named Docker volumes (databases, sites, and files) are kept so you can recreate "
                "the project from existing data.[/dim]"
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
    total_found = 0
    any_failure = names_rejected
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

        # A non-empty ``failures`` list means at least one requested step did not
        # complete (backup gate, volume/dir removal, container removal, Docker
        # error). Do not print a green check for a project that did not fully
        # remove - report the failures and force a non-zero exit.
        step_failures = result.get("failures") or []
        if step_failures:
            any_failure = True

        # Print results outside spinner context
        if result["found"]:
            total_found += 1
            containers_removed = result["containers"]
            total_removed += containers_removed

            if step_failures:
                stderr_console.print(
                    f"[bold red]✗[/bold red] Project '{name}' was not fully removed:"
                )
                for failure in step_failures:
                    stderr_console.print(f"    [yellow]- {failure}[/yellow]")
            elif result["orphan"]:
                cleaned = []
                if result["volumes"]:
                    cleaned.append(f"{result['volumes']} orphaned volume(s)")
                if result["dir_removed"]:
                    cleaned.append("project directory")
                detail = ", ".join(cleaned) if cleaned else "no leftover data"
                console.print(
                    f"[bold green]✓[/bold green] Project '{name}' cleaned up "
                    f"(0 containers; removed {detail})"
                )
            else:
                console.print(
                    f"[bold green]✓[/bold green] Project '{name}' removed "
                    f"({containers_removed} container(s))"
                )
                if not volumes:
                    console.print(f"  [dim]Volumes preserved for '{name}'[/dim]")
        # If the project was not found, the error message was already printed.

    console.print()
    if any_failure:
        stderr_console.print(
            "[bold red]✗[/bold red] Some removal steps failed; see the warnings above."
        )
        if total_removed > 0:
            console.print(f"  [dim]Removed {total_removed} container(s) before the failure.[/dim]")
        raise typer.Exit(code=1)

    if total_removed > 0:
        console.print(
            f"[bold green]✓[/bold green] Successfully removed {total_removed} container(s)"
        )
    elif total_found > 0:
        console.print(
            "[bold green]✓[/bold green] Cleaned up orphaned project(s); no containers were running."
        )
    else:
        console.print("[bold yellow]No projects were removed.[/bold yellow]")
