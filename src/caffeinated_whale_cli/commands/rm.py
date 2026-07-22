"""
Remove (delete) a Frappe project and its containers.

The destructive logic - the verified copy-out backup gate, the container/volume/
directory removal, and the honest-exit accounting - lives in ``core.remove``
(``core/rm.py``); this module is the human renderer over it. It keeps the argv
parsing, the confirm UX, the stopped-project start -> back up -> delete
orchestration (which prompts, so it stays outside the removal spinner and off the
prompt-free core), and the run-state reads that drive it.
"""

import sys
import time

import questionary
import typer

from ..core import rm as core_rm
from ..core.docker import get_project_containers
from ..core.errors import CwcliError
from ..utils import cache
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(help="Remove a Frappe project and its containers.")


class _RmRenderer:
    """Renders ``core.remove``'s typed events in this CLI's historical styling.

    Wraps the live ``status`` spinner handle so ``RmStep`` events update its
    label. ``RmNotice`` is a dim stdout line, ``RmWarning`` a yellow stderr
    warning (with an optional dim hint), ``RmError`` a red stderr error, and
    ``RmTrace`` a verbose-only dim stderr diagnostic - reproducing exactly what
    ``_remove_project`` used to print inline.
    """

    def __init__(self, status, *, verbose: bool):
        self._status = status
        self.verbose = verbose

    def __call__(self, event) -> None:
        if isinstance(event, core_rm.RmStep):
            self._status.update(f"[bold {event.style}]{event.label}[/bold {event.style}]")
        elif isinstance(event, core_rm.RmNotice):
            console.print(f"  [dim]{event.text}[/dim]")
        elif isinstance(event, core_rm.RmWarning):
            stderr_console.print(f"[yellow]Warning:[/yellow] {event.text}")
            if event.hint:
                stderr_console.print(f"[dim]{event.hint}[/dim]")
        elif isinstance(event, core_rm.RmError):
            stderr_console.print(f"[bold red]Error:[/bold red] {event.text}")
        elif isinstance(event, core_rm.RmTrace):
            if self.verbose:
                stderr_console.print(f"[dim]VERBOSE: {event.text}[/dim]")


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


def _project_run_state(project_name: str) -> str:
    """Classify a project so ``rm`` can decide whether to start it for a backup.

    Returns one of:
      * ``"running"`` - the frappe container is up (a live backup is possible),
      * ``"stopped"`` - containers exist but the frappe container is not running
        (it CAN be started for a transient pre-delete backup),
      * ``"orphan"`` - no containers at all (nothing to start; only ``--no-backup``
        can remove it), or
      * ``"error"`` - Docker could not be reached.
    """
    containers = get_project_containers(project_name)
    if containers is None:
        return "error"
    if not containers:
        return "orphan"
    frappe = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe is not None and frappe.status == "running":
        return "running"
    return "stopped"


def _wait_for_db_ready(
    container,
    *,
    attempts: int = 60,
    delay: float = 1.0,
    verbose: bool = False,
) -> bool:
    """Poll until MariaDB accepts connections, from inside the frappe container.

    A cold ``docker start`` returns before MariaDB has finished crash-recovery and
    init, so a ``bench backup`` run immediately after would fail to connect and
    spuriously fail the backup. MariaDB binds its 3306 port only once it is ready
    to serve, so a successful TCP connect to the ``mariadb`` service (``cwcli init``
    pins ``db_host mariadb``) is a reliable, credential-free readiness signal. Uses
    the frappe container's own Python (always present in a bench image). Bounded, so
    a DB that never comes up fails closed instead of hanging - the caller then
    aborts and keeps all data.
    """
    probe = (
        "import socket,sys; s=socket.socket(); s.settimeout(3); "
        "sys.exit(0 if s.connect_ex(('mariadb',3306))==0 else 1)"
    )
    for attempt in range(attempts):
        try:
            exit_code, _ = container.exec_run(["python3", "-c", probe])
        except Exception:
            exit_code = 1
        if exit_code == 0:
            if verbose:
                stderr_console.print("[dim]VERBOSE: MariaDB is accepting connections[/dim]")
            return True
        if attempt < attempts - 1:
            time.sleep(delay)
    if verbose:
        stderr_console.print(
            "[dim]VERBOSE: MariaDB did not become ready within " f"{attempts * delay:.0f}s[/dim]"
        )
    return False


def _transient_start_for_backup(
    project_name: str,
    verbose: bool = False,
    assume_yes: bool = False,
) -> tuple[bool, bool]:
    """Bring a STOPPED project up just long enough to take a pre-delete backup.

    A live ``bench backup`` needs the frappe + mariadb containers running, so when
    ``rm`` targets a stopped project on the data-destroying path (``--volumes``
    without ``--no-backup``) the databases would otherwise be deleted with no
    backup. This transiently starts the project - reusing ``start``'s port-conflict
    handling (:func:`commands.start._check_port_conflicts`) so it behaves exactly
    like ``cwcli start`` when other Frappe projects hold the ports - then waits for
    MariaDB to accept connections. Only the CONTAINERS are started; the supervisord
    web/worker stack is NOT launched, because a backup does not need it (and
    launching it would add failure modes, e.g. a first-time ``pip install
    supervisor``, that must not block a delete-time backup).

    Must be called OUTSIDE the removal spinner: ``_check_port_conflicts`` may prompt
    at a TTY (the documented spinner-over-questionary deadlock).

    Returns ``(ok, started)``:
      * ``ok`` - the project is up and MariaDB is ready to be backed up.
      * ``started`` - at least one container was started, so the caller can stop the
        project again (return it to its stopped state) if the removal is aborted.

    Raises ``typer.Exit`` (from ``_check_port_conflicts``) when host ports cannot be
    freed; the caller treats that as a failed start and aborts, keeping all data.
    """
    from .start import _check_port_conflicts  # lazy import: avoid a module-load cycle

    # 1. Free the host ports (may prompt at a TTY; refuses non-interactively). This
    #    runs BEFORE any container is started, so ``started`` stays False if it
    #    raises typer.Exit.
    _check_port_conflicts(project_name, verbose=verbose, assume_yes=assume_yes)

    # 2. Start every stopped container.
    containers = get_project_containers(project_name)
    if not containers:
        return (False, False)

    started = False
    try:
        for container in containers:
            if container.status != "running":
                container.start()
                started = True
    except Exception as e:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not start '{project_name}' to take a backup: {e}"
        )
        return (False, started)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        return (False, started)

    # 3. Wait for MariaDB to accept connections before the caller runs `bench backup`.
    if not _wait_for_db_ready(frappe_container, verbose=verbose):
        stderr_console.print(
            f"[yellow]Warning:[/yellow] MariaDB for '{project_name}' did not become ready; "
            "a backup could not be taken."
        )
        return (False, started)

    return (True, started)


def _stop_after_transient_start(project_name: str) -> None:
    """Return a project to its stopped state after a transient start-for-backup.

    Called on the keep-everything abort path (the start failed, or the backup could
    not be verified) so ``rm`` never leaves a project running that it found stopped.
    Best-effort: a failure to stop is a warning, not a hard error - the data is
    intact either way and the user can stop it manually.
    """
    from ..core import stop as core_stop  # lazy import: avoid a module-load cycle

    console.print(f"[dim]Returning '{project_name}' to its stopped state...[/dim]")
    try:
        with stderr_console.status(
            f"[bold yellow]Stopping '{project_name}'...[/bold yellow]", spinner="dots"
        ):
            core_stop.stop(project_name)
    except Exception as e:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Could not stop '{project_name}' after aborting; it may "
            f"still be running: {e}"
        )


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
@handle_docker_errors
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
    - Removes the project's own Docker network
    - Deletes the local project directory (~/.cwcli/projects/{name}/, or $CWCLI_HOME/projects/{name}/ if that override is set)
    - Clears the project from the cache

    Use --no-volumes to keep volumes:
    - Keeps the named Docker volumes (preserves databases, sites, and files)
    - Still removes the containers, project network, project directory, and cache entry

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

    A STOPPED project on the --volumes path is transiently STARTED so a live
    backup can be taken (start -> back up -> delete), reusing the same port-conflict
    handling as `cwcli start`. If it cannot be started, or the backup fails, the
    removal is aborted, ALL data is kept, the project is returned to its stopped
    state, and the command exits non-zero - pass --no-backup to delete without a
    backup instead. An orphan project (no containers) cannot be started, so it too
    is refused on the --volumes path unless --no-backup is given.

    Use --no-backup to skip backups (not recommended):
    - Skips database backups (and the backup safety gate)
    - Skips recaching (faster but risky)

    Examples:
        cwcli rm my-project                 # Remove everything (with confirmation)
        cwcli rm my-project --no-volumes    # Keep volumes, remove the remaining project resources
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
    # cwcli-state tree (~/.cwcli by default, or $CWCLI_HOME when that override
    # is set). Drop invalid names with a clear error; if any was rejected the
    # command exits non-zero even when valid names remain.
    valid_names = []
    invalid_names = []
    for name in project_names_to_process:
        if core_rm.is_valid_project_name(name):
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
            # Disclose BEFORE the confirm (not after) that a stopped project will be
            # started to take a backup first. A live `bench backup` needs a running
            # project, so a stopped one is transiently started -> backed up ->
            # deleted; the user should know that at decision time.
            if not no_backup:
                to_start = [
                    p for p in project_names_to_process if _project_run_state(p) == "stopped"
                ]
                if to_start:
                    console.print()
                    console.print(
                        "[yellow]Note:[/yellow] The following project(s) are stopped and will be "
                        "started to take a backup first, then deleted "
                        "(start [bold]->[/bold] back up [bold]->[/bold] delete):"
                    )
                    for p in to_start:
                        console.print(f"  • [bold]{p}[/bold]")
                    console.print(
                        "[dim]If a project cannot be started or backed up, it is left untouched "
                        "and nothing is deleted.[/dim]"
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
        console.print(
            "[dim]The project's own Docker network is also removed; it holds no user data.[/dim]"
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
        # For a STOPPED project on the data-destroying path, bring it up just long
        # enough to take a verified backup, THEN let core.remove delete it. A live
        # `bench backup` is impossible while stopped, so without this the databases
        # would be deleted with no backup. Done OUTSIDE the removal spinner because
        # port-conflict resolution may prompt (the spinner-over-questionary
        # deadlock). If the start/backup cannot happen, abort this project and keep
        # ALL its data; the user can retry, or pass --no-backup to delete without a
        # backup.
        started_for_backup = False
        if volumes and not no_backup and _project_run_state(name) == "stopped":
            console.print(
                f"[cyan]'{name}' is stopped; starting it to take a backup before removal...[/cyan]"
            )
            start_ok = False
            try:
                start_ok, started_for_backup = _transient_start_for_backup(
                    name, verbose=actual_verbose, assume_yes=yes
                )
            except typer.Exit:
                # Port conflict could not be resolved (or was declined): treat as a
                # failed start and abort this project.
                start_ok = False
            except KeyboardInterrupt:
                console.print("\n[yellow]Operation cancelled.[/yellow]")
                if started_for_backup:
                    _stop_after_transient_start(name)
                raise typer.Exit(code=1) from None

            if not start_ok:
                # Could not bring the project up for a backup (port conflict, image
                # gone, crash, or the DB never became ready). Do NOT delete anything;
                # return it to its stopped state and record the failure so the
                # command exits non-zero.
                if started_for_backup:
                    _stop_after_transient_start(name)
                any_failure = True
                stderr_console.print(
                    f"[bold red]✗[/bold red] Project '{name}' was not removed: it could not be "
                    "started to take a backup (nothing was deleted)."
                )
                stderr_console.print(
                    "[dim]Fix the start failure and retry, or pass --no-backup to delete without "
                    "a backup (the databases will be lost).[/dim]"
                )
                continue

        core_error: CwcliError | None = None
        result = None
        with stderr_console.status(
            f"[bold red]Removing '{name}'...[/bold red]", spinner="dots"
        ) as status:
            renderer = _RmRenderer(status, verbose=actual_verbose)
            try:
                result = core_rm.remove(
                    name,
                    remove_volumes=volumes,
                    no_backup=no_backup,
                    on_event=renderer,
                )
            except CwcliError as e:
                # A Docker connection error (or an invalid name that slipped the
                # pre-filter): a hard failure for this project. The core raised
                # before touching anything data-bearing.
                core_error = e

        if core_error is not None:
            stderr_console.print(f"[bold red]Error:[/bold red] {core_error.message}")
            if started_for_backup:
                _stop_after_transient_start(name)
            any_failure = True
            continue

        assert result is not None and result.data is not None
        outcome = result.data

        # A non-empty ``failures`` list means at least one requested step did not
        # complete (backup gate, volume/dir removal, container removal). Do not
        # print a green check for a project that did not fully remove - report the
        # failures and force a non-zero exit.
        step_failures = outcome.failures

        # If the removal was aborted (e.g. a backup could not be verified) and we
        # transiently started this project for the backup, return it to its original
        # stopped state - the early abort leaves the containers running.
        if step_failures and started_for_backup:
            _stop_after_transient_start(name)

        if step_failures:
            any_failure = True

        # Print results outside spinner context
        if outcome.found:
            total_found += 1
            containers_removed = outcome.containers_removed
            total_removed += containers_removed

            if step_failures:
                stderr_console.print(
                    f"[bold red]✗[/bold red] Project '{name}' was not fully removed:"
                )
                for failure in step_failures:
                    stderr_console.print(f"    [yellow]- {failure}[/yellow]")
            elif outcome.orphan:
                cleaned = []
                if outcome.volumes_removed:
                    cleaned.append(f"{outcome.volumes_removed} orphaned volume(s)")
                if outcome.network_removed:
                    cleaned.append("network")
                if outcome.dir_removed:
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
                if outcome.network_removed:
                    console.print(f"  [dim]Network removed for '{name}'[/dim]")
                if not volumes:
                    console.print(f"  [dim]Volumes preserved for '{name}'[/dim]")
        else:
            # Genuinely not found: the core returned found=False (the old code
            # printed this from inside _remove_project).
            stderr_console.print(f"[bold red]Error:[/bold red] Project '{name}' not found.")

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
