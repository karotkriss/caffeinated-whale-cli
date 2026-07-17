"""``cwcli restore`` - the renderer over ``core.restore`` (batch 11).

The destructive core (scan, selection, missing-apps, the ``bench restore --force``
exec with secrets off the argv, the encryption-key merge, migrate + restart, and
the streamed container copies) lives in ``core/restore.py``; this module is a
renderer. It keeps the frontend UX the core deliberately does not own: the
questionary backup menu, the missing-apps and destructive confirms, the MariaDB
credential prompts, and the sendme send/receive subprocess + ticket prompt (the
``commands/logs.py`` ``-it`` precedent: interactive host I/O the core does not
consume).

Container/bench/site resolution is a no-spinner PROLOGUE (the ``cwcli backup``
precedent), because every restore path needs a running container and resolves the
bench + site BEFORE the scan/download - the shared ``_resolve_bench_prologue``
collapses the three old per-mode copies of the no-cache inspect fallback.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

import questionary
import typer
from questionary import Style

from ..core import restore as core_restore
from ..core.envelope import Message, Status
from ..core.errors import CwcliError

# Re-export the pure/core helpers under this module's namespace so existing
# imports keep resolving; the core is the single implementation.
from ..core.restore import (  # noqa: E402,F401  (public re-exports)
    check_missing_apps,
    group_and_sort_backups,
    group_backup_sets,
    parse_backup_filename,
    scan_backups_for_all_sites,
    select_backup_set,
    transform_site_name_to_backup_format,
)
from ..utils import config_utils
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

DEFAULT_BENCH_PATH = "/workspace/frappe-bench"
_REMOTE_SENTINEL = "__restore_from_remote__"


# --------------------------------------------------------------------------- #
# Prologue helpers (no spinner - prompts/inspect run before any spinner).
# --------------------------------------------------------------------------- #
def _get_frappe_container(project_name: str):
    """Resolve the project's frappe container, rendering today's errors on miss."""
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
    return frappe_container


def _resolve_bench_prologue(
    project_name: str, bench: str | None, bench_path: str | None, verbose: bool
) -> str:
    """Resolve the bench path, populating the cache via inspect if it is empty.

    The shared no-spinner prologue that collapses the three old per-mode copies of
    the fallback: ``resolve_bench_path`` (which renders the multi-bench error and
    returns None only on a cold cache), then run inspect to populate, re-resolve,
    else the hardcoded default.
    """
    resolved = resolve_bench_path(project_name, bench, bench_path, verbose=verbose)
    if resolved:
        if verbose:
            stderr_console.print(f"[dim]Using cached bench path: {resolved}[/dim]")
        return resolved

    stderr_console.print("[yellow]No cached bench path found. Running inspect...[/yellow]")
    try:
        from ..core import inspect as core_inspect
        from .inspect import render_error_exit

        core_inspect.inspect(project_name, refresh="auto")
        resolved = resolve_bench_path(project_name, None, None, verbose=verbose)
        if resolved:
            if verbose:
                stderr_console.print(f"[dim]Using cached bench path from inspect: {resolved}[/dim]")
            return resolved
    except typer.Exit:
        raise
    except CwcliError as e:
        raise render_error_exit(project_name, e) from None
    except Exception as e:  # noqa: BLE001 - inspect failed; fall back to the default
        if verbose:
            stderr_console.print(f"[dim]Inspect error: {e}[/dim]")

    stderr_console.print(
        f"[yellow]Warning: Could not detect bench path. Using default: {DEFAULT_BENCH_PATH}[/yellow]"
    )
    return DEFAULT_BENCH_PATH


def _prompt_mariadb_credentials(
    mariadb_root_username: str | None,
    mariadb_root_password: str | None,
) -> tuple[str, str]:
    """Resolve the MariaDB root credentials in BOTH modes (unchanged UX).

    Interactive: prompt for the username (default ``root``) and password
    (blank is an error). Non-interactive: use the flags; a non-TTY WITHOUT
    ``--mariadb-root-password`` refuses non-zero rather than hanging or proceeding
    empty. Returns ``(username, password)`` both non-empty. Fired AFTER the
    confirms so a stray trailing Enter cannot leak into the password prompt.
    """
    is_tty = sys.stdin.isatty()

    if not mariadb_root_username:
        if is_tty:
            try:
                answer = questionary.text("MariaDB root username:", default="root").ask()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1) from None
            if answer is None:
                console.print("\n[yellow]Restore cancelled.[/yellow]")
                raise typer.Exit(code=1)
            mariadb_root_username = answer.strip() or "root"
        else:
            mariadb_root_username = "root"

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


# --------------------------------------------------------------------------- #
# Event rendering + warning rendering.
# --------------------------------------------------------------------------- #
def _render_warnings(warnings: list[Message]) -> None:
    """Render a core call's warnings as today's user-facing notes."""
    for w in warnings:
        if w.code == "default_site.resolved":
            console.print(f"[dim]Using default site: {w.text.split(': ', 1)[-1]}[/dim]")
        elif w.code == "bench.default_used":
            stderr_console.print(f"[yellow]Warning:[/yellow] {w.text}")
        elif w.code == "backup.multi_match":
            console.print(f"[yellow]Note:[/yellow] {w.text}")


def _make_event_renderer(spinner, verbose: bool):
    """A ``core.restore`` event callback that drives the spinner label and, under
    ``-v``, echoes command traces / raw output and always surfaces notices."""

    def _on_event(ev) -> None:
        if isinstance(ev, core_restore.RestoreStep):
            spinner.update(ev.message)
        elif isinstance(ev, core_restore.RestoreNotice):
            if ev.level == "warning":
                stderr_console.print(f"[yellow]Warning:[/yellow] {ev.text}")
            elif verbose:
                stderr_console.print(f"[dim]{ev.text}[/dim]")
        elif isinstance(ev, core_restore.RestoreTrace):
            if verbose:
                stderr_console.print(f"[dim]$ {ev.text}[/dim]")
        elif isinstance(ev, core_restore.RestoreOutput):
            if verbose and ev.text:
                console.print(ev.text)

    return _on_event


# --------------------------------------------------------------------------- #
# The interactive backup menu (rebuilt from the select_backup choice options).
# --------------------------------------------------------------------------- #
def _render_backup_menu(options: list[dict], target_site: str, *, offer_remote: bool) -> str | None:
    """Render today's rich backup-selection menu from the choice option rows.

    Returns the selected database full path, ``_REMOTE_SENTINEL`` (remote restore),
    or None (cancelled). ``offer_remote`` appends the sendme option (normal path
    only; ``--send`` never offers it).
    """
    choices: list = []
    value_by_label: dict[str, str] = {}

    target_opts = [o for o in options if o["group"] == "target"]
    other_opts = [o for o in options if o["group"] == "other"]

    def _badge(o: dict) -> str:
        badges = []
        if "FILES" in o["badges"]:
            badges.append("[FILES]")
        if "PRIVATE" in o["badges"]:
            badges.append("[PRIVATE]")
        return " ".join(badges) if badges else "[DATABASE ONLY]"

    if target_opts:
        choices.append(questionary.Separator(f"\n=== Backups from site: {target_site} ==="))
        for o in target_opts:
            text = f"{o['label']}  {_badge(o)}"
            choices.append(text)
            value_by_label[text] = o["value"]

    if other_opts:
        choices.append(questionary.Separator("\n=== Backups from other sites ==="))
        for o in other_opts:
            text = f"{o['label']} ({o['site_dir']})  {_badge(o)}"
            choices.append(text)
            value_by_label[text] = o["value"]

    if not target_opts and not other_opts:
        choices.append(questionary.Separator("\n=== No local backups found ==="))

    if offer_remote:
        choices.append(questionary.Separator("\n=== Remote Source ==="))
        remote_choice = "Restore from remote source (via sendme)"
        choices.append(remote_choice)
        value_by_label[remote_choice] = _REMOTE_SENTINEL

    custom_style = Style(
        [
            ("qmark", "fg:#00ff00 bold"),
            ("question", "fg:#00ffff bold"),
            ("answer", "fg:#00ff00 bold"),
            ("pointer", "fg:#ffff00 bold"),
            ("highlighted", "fg:#ffff00 bold"),
            ("selected", "fg:#00ff00"),
            ("separator", "fg:#666666"),
            ("instruction", "fg:#888888"),
            ("text", "fg:#ffffff"),
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
    return value_by_label.get(choice)


# --------------------------------------------------------------------------- #
# The two gates (rendered from plan facts; consent granted to restore_apply).
# --------------------------------------------------------------------------- #
def _gate(*, prompt_text: str, yes_text: str, refuse_text: str, yes: bool, isatty: bool) -> None:
    """A single --yes/non-TTY/confirm gate. Proceeds, or raises typer.Exit(1)."""
    if yes:
        console.print(f"[dim]{yes_text}[/dim]")
        return
    if not isatty:
        stderr_console.print(refuse_text)
        raise typer.Exit(code=1)
    try:
        answer = questionary.confirm(prompt_text, default=False, auto_enter=False).ask()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Restore cancelled.[/yellow]")
        raise typer.Exit(code=1) from None
    if not answer:
        console.print("[yellow]Restore cancelled.[/yellow]")
        raise typer.Exit(code=1)


def _render_missing_apps_gate(plan, *, yes: bool, isatty: bool) -> None:
    if not plan.missing_apps:
        return
    console.print()
    console.print(
        "[bold yellow]⚠ Warning:[/bold yellow] The following apps are installed on the backup "
        "site but not available on this bench:"
    )
    for app in plan.missing_apps:
        console.print(f"  • {app}")
    console.print()
    console.print("[dim]You may need to install these apps before restoring to avoid errors.[/dim]")
    console.print(
        f"[dim]Install apps with: bench get-app <app-name> && bench --site {plan.site} "
        "install-app <app-name>[/dim]"
    )
    console.print()
    _gate(
        prompt_text="Do you want to continue with the restore anyway?",
        yes_text="Proceeding despite missing apps (--yes).",
        refuse_text=(
            "[bold red]Error:[/bold red] Refusing to restore with missing apps without "
            "confirmation. Re-run with --yes to proceed non-interactively."
        ),
        yes=yes,
        isatty=isatty,
    )


def _render_destructive_gate(plan, *, yes: bool, isatty: bool) -> None:
    console.print()
    console.print(
        f"[bold yellow]⚠ Warning:[/bold yellow] This will replace all data in site '{plan.site}'"
    )
    console.print(f"[dim]Backup: {plan.backup_filename}[/dim]")
    if not plan.is_receive:
        console.print(f"[dim]From: {plan.backup_timestamp}[/dim]")
        console.print(f"[dim]Will restore: {', '.join(plan.restore_items)}[/dim]")
    if plan.origin_mismatch:
        console.print(
            f"[bold yellow]⚠ Origin mismatch:[/bold yellow] this backup is from site "
            f"'{plan.origin_mismatch}', but you are restoring into '{plan.site}'."
        )
    console.print()
    _gate(
        prompt_text="Are you sure you want to restore?",
        yes_text="Proceeding without confirmation (--yes).",
        refuse_text=(
            "[bold red]Error:[/bold red] Refusing to restore without confirmation. "
            "Re-run with --yes to restore non-interactively."
        ),
        yes=yes,
        isatty=isatty,
    )


# --------------------------------------------------------------------------- #
# Apply + result rendering.
# --------------------------------------------------------------------------- #
def _apply_and_render(
    plan,
    *,
    mariadb_root_username: str,
    mariadb_root_password: str,
    admin_password: str | None,
    no_migrate: bool,
    verbose: bool,
) -> None:
    """Run ``restore_apply`` under a spinner and render the outcome + exit code."""
    show_tips = config_utils.get_show_tips()
    console.print()
    try:
        with TipSpinner(
            f"Restoring site '{plan.site}'", console=stderr_console, enabled=show_tips
        ) as spinner:
            result = core_restore.restore_apply(
                plan,
                mariadb_root_username=mariadb_root_username,
                mariadb_root_password=mariadb_root_password,
                admin_password=admin_password,
                consent=True,
                no_migrate=no_migrate,
                on_event=_make_event_renderer(spinner, verbose),
            )
    except CwcliError as e:
        _handle_apply_error(e, plan.site, verbose)

    report = result.data
    assert report is not None

    console.print()
    console.print(f"[bold green]✓[/bold green] Successfully restored site '{plan.site}'")
    console.print(f"[dim]From backup: {plan.backup_filename}[/dim]")
    if report.included_files:
        console.print("[dim]Including file archives[/dim]")
    if report.encryption_key_updated:
        console.print("[dim]Updated encryption_key from backup site_config[/dim]")

    if report.migrate_ran:
        if report.migrate_ok:
            console.print(f"[bold green]✓[/bold green] Migrated site '{plan.site}'")
        else:
            for w in result.warnings:
                if w.code == "migrate.failed":
                    stderr_console.print(f"[bold yellow]⚠ Warning:[/bold yellow] {w.text}")
                    stderr_console.print(
                        f"[dim]Run it manually: cwcli run {plan.project_name} -- "
                        f"bench --site {plan.site} migrate[/dim]"
                    )
        console.print()
        console.print("[bold cyan]Restarting instance...[/bold cyan]")
        if report.restart_log:
            console.print(
                f"[bold green]✓[/bold green] Instance restarted (logs: {report.restart_log})"
            )
        else:
            console.print("[bold green]✓[/bold green] Instance restart requested")

    if result.status is Status.WARNING and report.migrate_ran and not report.migrate_ok:
        raise typer.Exit(code=1)


def _handle_apply_error(e: CwcliError, site: str, verbose: bool) -> NoReturn:
    """Render a restore_apply failure with today's messages, then Exit(1)."""
    if e.code == "restore.failed":
        output = (e.detail or {}).get("output")
        if output:  # a failure always shows the restore output (as today)
            console.print()
            console.print("[dim]Restore output:[/dim]")
            console.print(output)
        console.print()
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

    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    raise typer.Exit(code=1)


def _resolve_plan_or_exit(
    project_name: str,
    *,
    site: str | None,
    latest: bool,
    backup_file: str | None,
    selected_backup: str | None,
    bench_path: str,
    verbose: bool,
    receive: bool = False,
    downloaded_files: list | None = None,
):
    """Call the plan builder under a spinner and render its warnings/errors."""
    show_tips = config_utils.get_show_tips()
    try:
        with TipSpinner("Scanning backups", console=stderr_console, enabled=show_tips) as spinner:
            on_event = _make_event_renderer(spinner, verbose)
            if receive:
                result = core_restore.receive_plan(
                    project_name,
                    site=site,
                    bench_path=bench_path,
                    downloaded_files=downloaded_files or [],
                    on_event=on_event,
                )
            else:
                result = core_restore.restore_plan(
                    project_name,
                    site=site,
                    latest=latest,
                    backup_file=backup_file,
                    selected_backup=selected_backup,
                    bench_path=bench_path,
                    on_event=on_event,
                )
    except CwcliError as e:
        _handle_plan_error(project_name, e)
    _render_warnings(result.warnings)
    return result


def _handle_plan_error(project_name: str, e: CwcliError) -> NoReturn:
    """Render a plan-phase failure with today's messages, then Exit(1)."""
    if e.code == "site.no_default":
        stderr_console.print(
            "[bold red]Error:[/bold red] No site specified and no default site found in config."
        )
        stderr_console.print(
            f"[dim]Tip: Run 'cwcli inspect {project_name}' first, or specify --site explicitly.[/dim]"
        )
        raise typer.Exit(code=1)
    if e.code == "backup.no_match":
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        stderr_console.print(
            f"[dim]Tip: run 'cwcli inspect {project_name}' to check the project's state "
            "(a non-interactive session cannot browse backups interactively).[/dim]"
        )
        raise typer.Exit(code=1)
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Normal restore path.
# --------------------------------------------------------------------------- #
def _run_normal(
    project_name: str,
    *,
    site: str | None,
    latest: bool,
    backup_file: str | None,
    bench_path: str,
    mariadb_root_username: str | None,
    mariadb_root_password: str | None,
    admin_password: str | None,
    yes: bool,
    no_migrate: bool,
    verbose: bool,
) -> None:
    isatty = sys.stdin.isatty()
    selected_backup: str | None = None

    while True:
        result = _resolve_plan_or_exit(
            project_name,
            site=site,
            latest=latest,
            backup_file=backup_file,
            selected_backup=selected_backup,
            bench_path=bench_path,
            verbose=verbose,
        )
        if result.status is Status.OK:
            plan = result.data
            break
        # NEEDS_CHOICE: select_backup (the interactive menu).
        choice = result.choice
        assert choice is not None and choice.kind == "select_backup"
        if not isatty:
            stderr_console.print(
                "[bold red]Error:[/bold red] non-interactive session and no backup selector; "
                "pass --latest or --backup-file <name> to pick a backup without a menu."
            )
            raise typer.Exit(code=1)
        picked = _render_backup_menu(choice.options or [], site or "", offer_remote=True)
        if picked is None:
            raise typer.Exit(code=1)  # menu already printed "Restore cancelled."
        if picked == _REMOTE_SENTINEL:
            if not ensure_sendme_installed(verbose=verbose):
                stderr_console.print(
                    "[bold red]Error:[/bold red] sendme is required for remote backup transfers."
                )
                raise typer.Exit(code=1)
            return _run_receive(
                project_name,
                site=site,
                bench_path=bench_path,
                ticket=None,
                mariadb_root_username=mariadb_root_username,
                mariadb_root_password=mariadb_root_password,
                admin_password=admin_password,
                yes=yes,
                no_migrate=no_migrate,
                verbose=verbose,
            )
        selected_backup = picked

    _render_missing_apps_gate(plan, yes=yes, isatty=isatty)
    _render_destructive_gate(plan, yes=yes, isatty=isatty)
    username, password = _prompt_mariadb_credentials(mariadb_root_username, mariadb_root_password)
    _apply_and_render(
        plan,
        mariadb_root_username=username,
        mariadb_root_password=password,
        admin_password=admin_password,
        no_migrate=no_migrate,
        verbose=verbose,
    )


# --------------------------------------------------------------------------- #
# Receive path (sendme download stays frontend; the core copies-in + restores).
# --------------------------------------------------------------------------- #
def _run_receive(
    project_name: str,
    *,
    site: str | None,
    bench_path: str,
    ticket: str | None,
    mariadb_root_username: str | None,
    mariadb_root_password: str | None,
    admin_password: str | None,
    yes: bool,
    no_migrate: bool,
    verbose: bool,
) -> None:
    isatty = sys.stdin.isatty()

    console.print()
    console.print("[bold cyan]Receive backup via sendme[/bold cyan]")
    console.print()
    if ticket is None:
        if isatty:
            ticket = questionary.text(
                "Enter the sendme ticket:",
                validate=lambda text: len(text.strip()) > 0 or "Ticket cannot be empty",
            ).ask()
            if not ticket:
                stderr_console.print("[bold red]Error:[/bold red] Receive cancelled: no ticket.")
                raise typer.Exit(code=1)
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No sendme ticket provided and not running "
                "interactively.\n[dim]Pass --ticket <sendme-ticket> to receive "
                "non-interactively.[/dim]"
            )
            raise typer.Exit(code=1)
    ticket = "".join(ticket.split())
    if not ticket:
        stderr_console.print("[bold red]Error:[/bold red] Ticket cannot be empty after cleanup")
        raise typer.Exit(code=1)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        console.print()
        console.print("[bold cyan]Downloading backup files...[/bold cyan]")
        sendme_cmd = get_sendme_command()
        try:
            receive_cmd = [sendme_cmd, "receive", ticket]
            if verbose:
                stderr_console.print(f"[dim]$ {' '.join(receive_cmd)}[/dim]")
            proc = subprocess.run(receive_cmd, cwd=temp_dir, capture_output=not verbose, text=True)
            if proc.returncode != 0:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Failed to download files via sendme"
                )
                if proc.stderr and not verbose:
                    stderr_console.print(proc.stderr)
                raise typer.Exit(code=1)
        except FileNotFoundError as e:
            stderr_console.print(
                "[bold red]Error:[/bold red] sendme command not found. "
                "Try restarting your terminal or running: source ~/.bashrc"
            )
            raise typer.Exit(code=1) from e

        console.print("[bold green]✓[/bold green] Files downloaded successfully")
        console.print()

        downloaded_items = list(temp_path.glob("*"))
        if not downloaded_items:
            stderr_console.print("[bold red]Error:[/bold red] No files were downloaded")
            raise typer.Exit(code=1)
        if len(downloaded_items) == 1 and downloaded_items[0].is_dir():
            downloaded_files = list(downloaded_items[0].glob("*"))
        else:
            downloaded_files = downloaded_items
        if not downloaded_files:
            stderr_console.print("[bold red]Error:[/bold red] No backup files found in download")
            raise typer.Exit(code=1)
        if verbose:
            stderr_console.print(f"[dim]Downloaded {len(downloaded_files)} file(s)[/dim]")
            for f in downloaded_files:
                stderr_console.print(f"[dim]  - {f.name}[/dim]")

        console.print("[bold cyan]Copying files to container...[/bold cyan]")
        result = _resolve_plan_or_exit(
            project_name,
            site=site,
            latest=False,
            backup_file=None,
            selected_backup=None,
            bench_path=bench_path,
            verbose=verbose,
            receive=True,
            downloaded_files=downloaded_files,
        )
        # receive_plan resolves fully (no menu), so status is OK here.
        plan = result.data
        assert plan is not None
        console.print("[bold green]✓[/bold green] Files copied to container")
        console.print()

        username, password = _prompt_mariadb_credentials(
            mariadb_root_username, mariadb_root_password
        )
        _render_missing_apps_gate(plan, yes=yes, isatty=isatty)
        _render_destructive_gate(plan, yes=yes, isatty=isatty)
        _apply_and_render(
            plan,
            mariadb_root_username=username,
            mariadb_root_password=password,
            admin_password=admin_password,
            no_migrate=no_migrate,
            verbose=verbose,
        )


# --------------------------------------------------------------------------- #
# Send path (scan + copy-out in the core; the sendme subprocess is frontend).
# --------------------------------------------------------------------------- #
def _run_send(project_name: str, *, site: str | None, bench_path: str, verbose: bool) -> None:
    frappe_container = _get_frappe_container(project_name)

    console.print("[bold cyan]Scanning for backups...[/bold cyan]")
    backups = core_restore.scan_backups_for_all_sites(frappe_container, bench_path)
    if not backups:
        stderr_console.print("[bold red]Error:[/bold red] No backups found.")
        raise typer.Exit(code=1)

    target_backups, other_backups = core_restore.group_and_sort_backups(backups, site or "")
    options = core_restore._menu_options(target_backups, other_backups)
    picked = _render_backup_menu(options, site or "", offer_remote=False)
    if picked is None:
        return
    selected = core_restore._match_selected(target_backups, other_backups, picked)
    if not selected:
        return

    files_to_send = [
        selected[k]["full_path"]
        for k in ("database", "files", "private_files", "site_config_backup")
        if selected.get(k)
    ]
    if not files_to_send:
        stderr_console.print("[bold red]Error:[/bold red] No files to send.")
        raise typer.Exit(code=1)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        console.print()
        console.print(
            f"[bold cyan]Preparing {len(files_to_send)} file(s) for transfer...[/bold cyan]"
        )
        try:
            core_restore.copy_backup_files_out(frappe_container, files_to_send, temp_path)
        except CwcliError as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            raise typer.Exit(code=1) from e
        console.print("[bold green]✓[/bold green] Files prepared for transfer")
        console.print()

        sendme_cmd = get_sendme_command()
        console.print("[bold cyan]Creating sendme ticket...[/bold cyan]")
        console.print()
        try:
            cmd = [sendme_cmd, "send", str(temp_path)]
            if verbose:
                stderr_console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
            ticket = None
            ticket_line_pattern = re.compile(r"sendme receive (\S+)")
            if process.stdout:
                for line in iter(process.stdout.readline, ""):
                    stripped = line.strip()
                    if verbose and stripped and line and not line[0].isspace():
                        stderr_console.print(f"[dim]\\[sendme] {stripped}[/dim]")
                    match = ticket_line_pattern.search(line)
                    if match:
                        ticket = match.group(1)
                        break
                    if process.poll() is not None:
                        break

            if ticket:
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
    ticket: str | None = typer.Option(
        None,
        "--ticket",
        help="The sendme ticket for --receive. When supplied, the ticket prompt is "
        "skipped; a non-TTY without --ticket refuses with a non-zero exit.",
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
    if send and receive:
        stderr_console.print(
            "[bold red]Error:[/bold red] Cannot use --send and --receive together."
        )
        raise typer.Exit(code=1)
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

    if send or receive:
        if not ensure_sendme_installed(verbose=verbose):
            stderr_console.print(
                "[bold red]Error:[/bold red] sendme is required for remote backup transfers."
            )
            raise typer.Exit(code=1)

    # Ensure containers are running, then resolve the bench (a no-spinner prologue
    # shared by all three modes; every path needs a running container and a
    # resolved bench BEFORE the scan/download).
    ensure_containers_running(project_name, require_running=True, verbose=verbose)
    resolved_bench = _resolve_bench_prologue(project_name, bench, bench_path, verbose)

    if receive:
        return _run_receive(
            project_name,
            site=site,
            bench_path=resolved_bench,
            ticket=ticket,
            mariadb_root_username=mariadb_root_username,
            mariadb_root_password=mariadb_root_password,
            admin_password=admin_password,
            yes=yes,
            no_migrate=no_migrate,
            verbose=verbose,
        )
    if send:
        return _run_send(project_name, site=site, bench_path=resolved_bench, verbose=verbose)

    return _run_normal(
        project_name,
        site=site,
        latest=latest,
        backup_file=backup_file,
        bench_path=resolved_bench,
        mariadb_root_username=mariadb_root_username,
        mariadb_root_password=mariadb_root_password,
        admin_password=admin_password,
        yes=yes,
        no_migrate=no_migrate,
        verbose=verbose,
    )
