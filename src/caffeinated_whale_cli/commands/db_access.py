"""``cwcli db-access``: which sites' database users can still connect from where the
frappe container is now, and the one repair (``USER@'%'``). See ``core.db_access``."""

from __future__ import annotations

import json

import typer

from ..core import db_access as core_db_access
from ..core import docker as core_docker
from ..core import resolvers
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors

app = typer.Typer(help="Site database users vs the frappe container's address: check and repair.")


def _resolve(project: str, bench: str | None):
    container = core_docker.get_frappe_container(project)
    resolved = resolvers.resolve_bench(project, bench, None)
    if resolved is not None and resolved.status is Status.NEEDS_CHOICE:
        stderr_console.print(
            "[bold red]Error:[/bold red] this instance has several benches; pass --bench."
        )
        raise typer.Exit(code=2)
    path = resolved.data if resolved is not None and resolved.data else resolvers.DEFAULT_BENCH_PATH
    return container, path


def _print(rows, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps([a.to_dict() for a in rows]))
        return
    for a in rows:
        mark = "[green]ok[/green]" if a.ok else "[red]REFUSED[/red]"
        hosts = ", ".join(a.hosts) or "-"
        console.print(
            f"{a.site}  {a.db_user}@{{{hosts}}}  container {a.client_host}  {mark}"
            f"{'  (repaired)' if a.repaired else ''}"
        )


@app.command("check")
@handle_docker_errors
def check_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    bench: str | None = typer.Option(None, "--bench", "-b"),
    mariadb_root_password: str | None = typer.Option(
        None, "--mariadb-root-password", envvar="CWCLI_DB_ROOT_PASSWORD"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Which sites' database users would be refused from the container's current address (exit 1 if any)."""
    container, path = _resolve(project, bench)
    try:
        rows = core_db_access.check(
            container, path, project=project, root_password=mariadb_root_password
        )
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None
    _print(rows, as_json)
    raise typer.Exit(code=0 if all(a.ok for a in rows) else 1)


@app.command("repair")
@handle_docker_errors
def repair_cmd(
    project: str = typer.Argument(..., autocompletion=complete_project_names),
    bench: str | None = typer.Option(None, "--bench", "-b"),
    site: list[str] = typer.Option(
        None, "--site", "-s", help="Only these sites (default: every refused one)."
    ),
    mariadb_root_password: str | None = typer.Option(
        None, "--mariadb-root-password", envvar="CWCLI_DB_ROOT_PASSWORD"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Rename the users to '%' (changes MariaDB grants)."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Rename each refused site's database user to USER@'%' so any container address may connect."""
    if not yes:
        stderr_console.print("[bold red]Error:[/bold red] this renames MariaDB users; pass --yes.")
        raise typer.Exit(code=1)
    container, path = _resolve(project, bench)
    try:
        result = core_db_access.repair(
            container,
            path,
            project=project,
            root_password=mariadb_root_password,
            sites=list(site) if site else None,
        )
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None
    _print(result.data or [], as_json)
    for w in result.warnings:
        stderr_console.print(f"[yellow]{w.text}[/yellow]")
    raise typer.Exit(code=0 if all(a.ok for a in (result.data or [])) else 1)
