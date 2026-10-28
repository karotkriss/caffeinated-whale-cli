"""``cwcli backups``: the cwcli-owned backup store (``core.backup_store``) on the human
surface - list the sets, take one into the store, verify, delete, restore one into a site.
A thin renderer: every decision is the core's."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from ..core import backup_store as core_backup_store
from ..core import db_password as core_db_password
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(help="Backups kept outside the instances: list, take, verify, delete, restore.")


def _narrate(event) -> None:
    kind = type(event).__name__
    if kind == "StoreStep":
        if event.status == "start":
            stderr_console.print(f"[dim]{event.message}[/dim]")
        elif event.status == "failed":
            stderr_console.print(f"[yellow]{event.message}[/yellow]")
    elif kind == "StoreNotice":
        stderr_console.print(
            f"[yellow]{event.text}[/yellow]"
            if event.level == "warning"
            else f"[dim]{event.text}[/dim]"
        )
    elif hasattr(event, "message") and getattr(event, "phase", None):
        stderr_console.print(f"[dim]{event.message}[/dim]")


def _fail(e: CwcliError) -> None:
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.hint:
        stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1)


def _print_sets(sets, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps([b.to_dict() for b in sets]))
        return
    if not sets:
        console.print("[dim]No backup sets in the store.[/dim]")
        return
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    for col in ("Key", "Created (UTC)", "Reason", "Parts", "Size", "Verified"):
        table.add_column(col)
    for b in sets:
        parts = ", ".join(p.kind.replace("_", " ") for p in b.parts)
        size = f"{b.size / 1048576:.1f} MB" if b.size < 1 << 30 else f"{b.size / (1 << 30):.2f} GB"
        table.add_row(
            b.key,
            b.created_at,
            b.reason,
            parts,
            size,
            "[green]yes[/green]" if b.verified else "[red]NO[/red]",
        )
    console.print(table)


@app.command("list")
def list_cmd(
    project: str = typer.Argument(
        None, help="Narrow to one instance.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(None, "--site", "-s", help="Narrow to one site."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List the sets in the store, newest first."""
    _print_sets(core_backup_store.list_sets(project, site), as_json)
    if not as_json:
        budget = core_backup_store.disk_budget()
        tone = "yellow" if budget["warn"] else "dim"
        console.print(
            f"[{tone}]Store {budget['root']}: {budget['store_gb']} GB used, {budget['free_gb']} GB free on its disk"
            + (f" (under the {budget['min_free_gb']} GB floor)" if budget["warn"] else "")
            + f"[/{tone}]"
        )


@app.command("capture")
@handle_docker_errors
def capture_cmd(
    project: str = typer.Argument(..., help="The instance.", autocompletion=complete_project_names),
    site: str = typer.Option(None, "--site", "-s", help="The site (defaults like `cwcli backup`)."),
    bench: str = typer.Option(None, "--bench", "-b", help="Bench index or label (multi-bench)."),
    no_files: bool = typer.Option(False, "--no-files", help="Database and config only."),
    reason: str = typer.Option(
        "manual", "--reason", help="One of: " + ", ".join(core_backup_store.REASONS)
    ),
    note: str = typer.Option(None, "--note", help="A free note kept with the set."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Back a site up INTO the store (verified copy-out; in-bench copies removed once verified)."""
    try:
        result = core_backup_store.capture(
            project,
            site=site,
            bench=bench,
            with_files=not no_files,
            reason=reason,
            note=note,
            on_event=None if as_json else _narrate,
        )
    except CwcliError as e:
        _fail(e)
        return
    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        stderr_console.print(f"[yellow]{result.choice.prompt}[/yellow]")
        raise typer.Exit(code=2)
    bset = result.data
    assert bset is not None
    for w in result.warnings:
        stderr_console.print(f"[yellow]{w.text}[/yellow]")
    if as_json:
        console.print_json(json.dumps(bset.to_dict()))
    else:
        console.print(
            f"{'[green]Verified[/green]' if bset.verified else '[red]NOT verified[/red]'} {bset.key} ({bset.size / 1048576:.1f} MB)"
        )
        for c in bset.checks:
            console.print(
                f"  {'[green]ok[/green]' if c.ok else '[red]failed[/red]'} {c.name}: {c.detail}"
            )
    raise typer.Exit(code=0 if bset.verified else 1)


@app.command("verify")
def verify_cmd(
    key: str = typer.Argument(..., help="The set key, project/site/id."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Re-run every check on a stored set."""
    bset = core_backup_store.find_set(key)
    if bset is None:
        stderr_console.print(f"[bold red]Error:[/bold red] no backup set {key}")
        raise typer.Exit(code=1)
    verified = core_backup_store.verify_set(bset)
    if as_json:
        console.print_json(json.dumps(verified.to_dict()))
    else:
        for c in verified.checks:
            console.print(
                f"{'[green]ok[/green]' if c.ok else '[red]failed[/red]'} {c.name}: {c.detail}"
            )
    raise typer.Exit(code=0 if verified.verified else 1)


@app.command("delete")
def delete_cmd(
    key: str = typer.Argument(..., help="The set key, project/site/id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete a set from the store (never a site's last good one)."""
    bset = core_backup_store.find_set(key)
    if bset is None:
        stderr_console.print(f"[bold red]Error:[/bold red] no backup set {key}")
        raise typer.Exit(code=1)
    if not yes:
        if not sys.stdin.isatty():
            stderr_console.print(
                "[bold red]Error:[/bold red] pass --yes to delete without a prompt."
            )
            raise typer.Exit(code=1)
        if not typer.confirm(f"Delete {bset.key} ({bset.size / 1048576:.1f} MB)?"):
            raise typer.Exit(code=0)
    try:
        core_backup_store.delete_set(bset, consent=True)
    except CwcliError as e:
        _fail(e)
    console.print(f"Deleted {key}")


@app.command("restore")
@handle_docker_errors
def restore_cmd(
    key: str = typer.Argument(..., help="The set key, project/site/id."),
    project: str = typer.Argument(
        ..., help="The TARGET instance.", autocompletion=complete_project_names
    ),
    site: str = typer.Option(..., "--site", "-s", help="The TARGET site (replaced in full)."),
    bench: str = typer.Option(None, "--bench", "-b", help="Target bench index or label."),
    admin_password: str = typer.Option(
        None, "--admin-password", help="Reset Administrator to this after the restore."
    ),
    mariadb_root_username: str = typer.Option("root", "--mariadb-root-username"),
    mariadb_root_password: str = typer.Option(
        None, "--mariadb-root-password", help="Defaults to the instance's remembered one."
    ),
    keep_config: bool = typer.Option(
        False,
        "--keep-config",
        help="Keep the target's site_config as bench restore leaves it (default: carry the set's app settings, keep the target's db_* keys).",
    ),
    no_migrate: bool = typer.Option(False, "--no-migrate"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm replacing the target site's data."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Restore a stored set into a site (same site, a new one, another bench or instance)."""
    if not yes:
        if not sys.stdin.isatty():
            stderr_console.print(
                "[bold red]Error:[/bold red] this replaces every byte of the target site; pass --yes."
            )
            raise typer.Exit(code=1)
        if not typer.confirm(f"Replace ALL data of site '{site}' on {project} with {key}?"):
            raise typer.Exit(code=0)
    try:
        result = core_backup_store.restore_set(
            key,
            project=project,
            site=site,
            bench=bench,
            mariadb_root_username=mariadb_root_username,
            mariadb_root_password=core_db_password.resolve(project, mariadb_root_password),
            admin_password=admin_password,
            consent=True,
            no_migrate=no_migrate,
            config_mode="keep" if keep_config else "carry",
            on_event=None if as_json else _narrate,
        )
    except CwcliError as e:
        _fail(e)
        return
    if result.status is Status.NEEDS_CHOICE and result.choice is not None:
        stderr_console.print(f"[yellow]{result.choice.prompt}[/yellow]")
        raise typer.Exit(code=2)
    report = result.data
    assert report is not None
    if as_json:
        console.print_json(json.dumps(asdict(report)))
    else:
        console.print(f"Restored {key} into {report.site} on {report.project}")
        for c in report.post_checks:
            console.print(
                f"  {'[green]ok[/green]' if c.ok else '[red]failed[/red]'} {c.name}: {c.detail}"
            )
        if report.migrate_ran and not report.migrate_ok:
            console.print(
                "[red]bench migrate failed after the restore; see the output above.[/red]"
            )
    raise typer.Exit(code=0 if result.status is Status.OK else 1)


# ---------------------------------------------------------- phase 2: move, clone, sendme


def _narrate_transfer(event) -> None:
    kind = type(event).__name__
    if kind == "Ticket":
        console.print(f"\n[bold green]sendme ticket:[/bold green] {event.ticket}")
        console.print(
            "[dim]On the other machine: cwcli backups receive <ticket>; Ctrl+C here when done.[/dim]\n"
        )
    elif kind == "TransferProgress":
        stderr_console.print(f"[cyan]{event.message}[/cyan]")
    elif kind == "Received":
        console.print("[bold green]✓[/bold green] " + ", ".join(event.sets))
    else:
        _narrate(event)


@app.command("transfer")
@handle_docker_errors
def transfer_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    site: list[str] = typer.Option(
        ..., "--site", "-s", help="Source site, or SOURCE:TARGET to name the copy (repeatable)."
    ),
    mode: str = typer.Option(
        "clone", "--mode", help="clone (keep the source) or move (drop it after)."
    ),
    bench: str | None = typer.Option(None, "--bench", "-b"),
    to_project: str | None = typer.Option(
        None, "--to-project", help="Target instance (default: same)."
    ),
    to_bench: str | None = typer.Option(None, "--to-bench", help="Target bench (default: same)."),
    admin_password: str | None = typer.Option(
        None,
        "--admin-password",
        envvar="CWCLI_ADMIN_PASSWORD",
        help="For a site created on the way.",
    ),
    mariadb_root_password: str | None = typer.Option(
        None, "--mariadb-root-password", envvar="CWCLI_DB_ROOT_PASSWORD"
    ),
    keep_config: bool = typer.Option(False, "--keep-config"),
    no_files: bool = typer.Option(False, "--no-files"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="A move drops each source; this is its consent."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Clone or move one or more sites to another name, bench or instance (x of N)."""
    from ..core import transfer as core_transfer

    pairs = [(s.split(":", 1)[0], s.split(":", 1)[1] if ":" in s else s) for s in site]
    if mode == "move" and not yes:
        stderr_console.print(
            "[bold red]Error:[/bold red] a move drops each source site; pass --yes."
        )
        raise typer.Exit(code=1)
    try:
        result = core_transfer.transfer_sites(
            project,
            pairs,
            mode=mode,
            bench=bench,
            target_project=to_project,
            target_bench=to_bench,
            admin_password=admin_password or __import__("secrets").token_urlsafe(18),
            mariadb_root_password=core_db_password.resolve(project, mariadb_root_password),
            target_mariadb_root_password=(
                core_db_password.resolve(to_project, mariadb_root_password) if to_project else None
            ),
            config_mode="keep" if keep_config else "carry",
            with_files=not no_files,
            consent=yes,
            on_event=None if as_json else _narrate_transfer,
        )
    except CwcliError as e:
        _fail(e)
    report = result.data
    assert report is not None
    if as_json:
        console.print_json(json.dumps(asdict(report)))
    else:
        for i in report.items:
            mark = "[green]ok[/green]" if i.ok else f"[red]{i.error or 'not done'}[/red]"
            console.print(f"{i.site} -> {report.target_project}:{i.target_site}  {mark}")
    raise typer.Exit(code=0 if report.ok else 1)


@app.command("send")
def send_cmd(
    key: list[str] = typer.Argument(
        ..., help="Set key(s): project/site/id (several travel as one ticket)."
    ),
) -> None:
    """Serve one or more sets over sendme and print the ticket; Ctrl+C when the other side is done."""
    from ..core import transfer as core_transfer

    try:
        core_transfer.send_sets(key, on_event=_narrate_transfer)
    except CwcliError as e:
        _fail(e)
    except KeyboardInterrupt:
        console.print("[dim]Stopped serving.[/dim]")


@app.command("receive")
def receive_cmd(
    ticket: str = typer.Argument(...),
    project: str | None = typer.Option(
        None, "--project", help="File a single set under this instance."
    ),
    site: str | None = typer.Option(None, "--site", help="... and this site name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Download a sendme ticket into the store (then restore it with `cwcli backups restore`)."""
    from ..core import transfer as core_transfer

    try:
        result = core_transfer.receive_sets(
            ticket, project=project, site=site, on_event=None if as_json else _narrate_transfer
        )
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps(result.data))


# ----------------------------------------------------------- phase 3: retention, schedule


@app.command("prune")
def prune_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Only list what the policy would remove."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Remove them (pruning deletes backups)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Apply the instance's retention policy to the store: list what goes, and remove it with --yes."""
    try:
        result = core_backup_store.prune(
            project, consent=yes, dry_run=dry_run, on_event=None if as_json else _narrate
        )
    except CwcliError as e:
        _fail(e)
    if result.status is Status.NEEDS_CHOICE:
        stderr_console.print(
            f"[bold red]Error:[/bold red] {result.choice.prompt if result.choice else 'consent needed'} "
            "Pass --yes to remove them, or --dry-run to only list them."
        )
        raise typer.Exit(code=1)
    data = result.data or {}
    if as_json:
        console.print_json(json.dumps(data))
        return
    rows = data.get("would_remove") or data.get("removed") or []
    verb = "would remove" if dry_run else "removed"
    console.print(f"{len(rows)} set(s) {verb}; {data.get('kept', 0)} kept")
    for d in rows:
        console.print(f"  • {d['key']}  [dim]{d['why']}[/dim]")


@app.command("settings")
def settings_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    daily: int | None = typer.Option(None, "--daily", help="Local daily sets to keep."),
    weekly: int | None = typer.Option(None, "--weekly"),
    monthly: int | None = typer.Option(None, "--monthly"),
    safety_days: int | None = typer.Option(None, "--safety-days"),
    safety_max: int | None = typer.Option(None, "--safety-max"),
    min_free_gb: float | None = typer.Option(None, "--min-free-gb"),
    stale_after_days: int | None = typer.Option(None, "--stale-after-days"),
    schedule: str | None = typer.Option(
        None, "--schedule", help="on or off: one daily backup of every site, then the prune."
    ),
    daily_at: str | None = typer.Option(
        None, "--daily-at", help="Local time of the daily backup, HH:MM."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show or change the instance's backup settings (retention, safety, disk floor, schedule)."""
    values: dict = {}
    local = {
        k: v
        for k, v in (("daily", daily), ("weekly", weekly), ("monthly", monthly))
        if v is not None
    }
    if local:
        values["local"] = local
    safety = {
        k: v for k, v in (("days", safety_days), ("max_per_site", safety_max)) if v is not None
    }
    if safety:
        values["safety"] = safety
    if min_free_gb is not None:
        values["min_free_gb"] = min_free_gb
    if stale_after_days is not None:
        values["stale_after_days"] = stale_after_days
    sched: dict[str, Any] = {}
    if schedule is not None:
        if schedule not in ("on", "off"):
            stderr_console.print("[bold red]Error:[/bold red] --schedule takes on or off")
            raise typer.Exit(code=2)
        sched["enabled"] = schedule == "on"
    if daily_at is not None:
        if not __import__("re").fullmatch(r"[0-2]\d:[0-5]\d", daily_at):
            stderr_console.print("[bold red]Error:[/bold red] --daily-at takes HH:MM")
            raise typer.Exit(code=2)
        sched["daily_at"] = daily_at
    if sched:
        values["schedule"] = sched
    try:
        current = (
            core_backup_store.save_settings(project, values)
            if values
            else core_backup_store.settings(project)
        )
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps(current))
        return
    for k, v in current.items():
        console.print(f"{k}: {json.dumps(v)}")


# ------------------------------------------------------------ phase 3: off-site copies

s3_app = typer.Typer(
    help="Encrypted off-site copies of the store in an S3 bucket (AWS, MinIO, ...)."
)
app.add_typer(s3_app, name="s3")
key_app = typer.Typer(help="The key that encrypts off-site copies; export it to restore elsewhere.")
app.add_typer(key_app, name="key")


@s3_app.command("configure")
def s3_configure_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="https://s3.<region>.amazonaws.com or http://host:9000"
    ),
    bucket: str | None = typer.Option(None, "--bucket"),
    prefix: str | None = typer.Option(None, "--prefix"),
    region: str | None = typer.Option(None, "--region"),
    access_key: str | None = typer.Option(None, "--access-key"),
    secret_key: str | None = typer.Option(
        None,
        "--secret-key",
        envvar="CWCLI_S3_SECRET_KEY",
        help="Stored on this machine only (0600); prefer the env var.",
    ),
    copy_on_backup: bool | None = typer.Option(None, "--copy-on-backup/--no-copy-on-backup"),
    enable: bool | None = typer.Option(None, "--enable/--disable"),
    probe: bool = typer.Option(
        True, "--probe/--no-probe", help="Check the bucket answers (creating it when absent)."
    ),
) -> None:
    """Point the instance's off-site copies at a bucket; the secret never prints."""
    from ..core import s3 as core_s3

    values: dict[str, Any] = {}
    for k, v in (
        ("endpoint", endpoint),
        ("bucket", bucket),
        ("prefix", prefix),
        ("region", region),
        ("access_key", access_key),
        ("copy_on_backup", copy_on_backup),
        ("enabled", enable),
    ):
        if v is not None:
            values[k] = v
    if secret_key:
        core_backup_store.set_s3_secret(secret_key, project=project)
    if values:
        core_backup_store.save_settings(project, {"s3_target": values})
    cfg = core_backup_store.settings(project)["s3_target"]
    console.print(
        f"endpoint: {cfg['endpoint']}  bucket: {cfg['bucket']}  prefix: {cfg['prefix']}  region: {cfg['region']}  access key: {cfg['access_key'] or '-'}  enabled: {cfg['enabled']}  copy on backup: {cfg['copy_on_backup']}"
    )
    if probe and cfg.get("enabled"):
        try:
            target = core_backup_store.offsite_target(project)
        except CwcliError as e:
            _fail(e)
        verdict = core_s3.probe(target)
        if verdict["ok"]:
            console.print(
                f"[bold green]✓[/bold green] bucket answers ({verdict['objects']} object(s) under the prefix)"
            )
        else:
            stderr_console.print(f"[bold red]✗[/bold red] {verdict['error']}")
            raise typer.Exit(code=1)


@s3_app.command("list")
def s3_list_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """The sets the bucket holds for the instance, and whether each is still local."""
    try:
        rows = core_backup_store.offsite_list(project)
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps(rows))
        return
    if not rows:
        console.print("[dim]No off-site copies yet.[/dim]")
    for d in rows:
        console.print(
            f"{d['key']}  {d.get('created_at')}  {d.get('reason')}  {'local' if d['local'] else 'off-site only'}"
        )


@s3_app.command("copy")
@handle_docker_errors
def s3_copy_cmd(key: list[str] = typer.Argument(..., help="Set key(s): project/site/id")) -> None:
    """Encrypt and upload the set(s) to the instance's bucket."""
    for k in key:
        found = core_backup_store.find_set(k)
        if found is None:
            stderr_console.print(f"[bold red]Error:[/bold red] no such set {k}")
            raise typer.Exit(code=1)
        try:
            core_backup_store.offsite_copy(found, on_event=_narrate)
        except CwcliError as e:
            _fail(e)


@s3_app.command("fetch")
def s3_fetch_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    key: list[str] = typer.Argument(..., help="Set key(s) to download into the local store."),
) -> None:
    """Download and decrypt set(s) from the bucket into this machine's store."""
    for k in key:
        try:
            got = core_backup_store.offsite_fetch(project, k, on_event=_narrate)
        except CwcliError as e:
            _fail(e)
        console.print(f"{got.key}  {'verified' if got.verified else 'NOT verified'}")


@s3_app.command("prune")
def s3_prune_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Apply the S3 retention (7 daily / 4 weekly / 12 monthly by default) to the bucket."""
    try:
        result = core_backup_store.offsite_prune(
            project, consent=yes, dry_run=dry_run, on_event=None if as_json else _narrate
        )
    except CwcliError as e:
        _fail(e)
    if result.status is Status.NEEDS_CHOICE:
        stderr_console.print(
            "[bold red]Error:[/bold red] pass --yes to remove them, or --dry-run to list."
        )
        raise typer.Exit(code=1)
    data = result.data or {}
    if as_json:
        console.print_json(json.dumps(data))
        return
    rows = data.get("would_remove") or data.get("removed") or []
    console.print(
        f"{len(rows)} off-site copy(ies) {'would be' if dry_run else ''} removed; {data.get('kept', 0)} kept"
    )
    for d in rows:
        console.print(f"  • {d['key']}  [dim]{d['why']}[/dim]")


@key_app.command("export")
def key_export_cmd(
    to: Path = typer.Option(..., "--to", help="Write the 32-byte key here (mode 0600).")
) -> None:
    """Export the off-site encryption key to a file (never printed); import it where the copies are restored."""
    from ..core import backup_crypto

    try:
        out = backup_crypto.export_key(to)
    except CwcliError as e:
        _fail(e)
    console.print(f"key written to {out} (keep it with the same care as the bucket's credentials)")


@key_app.command("import")
def key_import_cmd(
    src: Path = typer.Option(
        ..., "--from", help="A key file written by `cwcli backups key export`."
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Replace a key this machine already has."
    ),
) -> None:
    """Install the off-site encryption key from another machine."""
    from ..core import backup_crypto

    try:
        p = backup_crypto.import_key(src, replace=replace)
    except CwcliError as e:
        _fail(e)
    console.print(f"key installed at {p}")


# -------------------------------------------------- phase 3: download, upload, drills


@app.command("download")
def download_cmd(
    key: str = typer.Argument(..., help="Set key: project/site/id"),
    to: Path = typer.Option(
        Path("."), "--to", help="Directory to write <project>__<site>__<id>.tar into."
    ),
) -> None:
    """Write one set as a tar (a plain copy of its parts and manifest; not encrypted)."""
    from ..core import transfer as core_transfer

    try:
        out = core_transfer.export_tar(key, to)
    except CwcliError as e:
        _fail(e)
    console.print(str(out))


@app.command("upload")
def upload_cmd(
    tar: Path = typer.Argument(
        ..., help="A tar written by `cwcli backups download` (or a bundle)."
    ),
    project: str | None = typer.Option(
        None, "--project", help="File a single set under this instance."
    ),
    site: str | None = typer.Option(None, "--site", help="... and this site name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """File a downloaded set into this machine's store."""
    from ..core import transfer as core_transfer

    try:
        result = core_transfer.import_tar(tar, project=project, site=site)
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps(result.data))
    else:
        for k in (result.data or {}).get("sets", []):
            console.print(k)


@app.command("drill")
@handle_docker_errors
def drill_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    site: str = typer.Option(..., "--site", "-s"),
    bench: str | None = typer.Option(None, "--bench", "-b"),
    set_key: str | None = typer.Option(
        None, "--set", help="A set key; default: the site's last good backup."
    ),
    mariadb_root_password: str | None = typer.Option(
        None, "--mariadb-root-password", envvar="CWCLI_DB_ROOT_PASSWORD"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Restore drill: restore the site's latest good set into a throwaway site, check it answers, drop it, record the verdict."""
    from ..core import backup_drill

    try:
        result = backup_drill.run_drill(
            project,
            site=site,
            bench=bench,
            set_key=set_key,
            mariadb_root_password=core_db_password.resolve(project, mariadb_root_password),
            on_event=None if as_json else _narrate_transfer,
        )
    except CwcliError as e:
        _fail(e)
    report = result.data
    assert report is not None
    if as_json:
        console.print_json(json.dumps(asdict(report)))
    else:
        console.print(
            f"drill of {report.set_key or '-'} into {report.drill_site}: {'[green]OK[/green]' if report.ok else '[red]FAILED[/red]'} - {report.detail}"
        )
    raise typer.Exit(code=0 if report.ok else 1)
