"""
Search all cached instances for apps or sites by name.

Thin CLI frontend over :func:`core.where.where`: it owns only the flag parsing,
the rich tables, and the JSON/plain rendering. The search, dedup, sort, and the
``--apps``/``--sites`` conflict all live in the core.
"""

import json

import typer
from rich.console import Console
from rich.table import Table

from ..core import where as core_where
from ..core.errors import CwcliError
from ..core.where import WhereMatch

console = Console()


#: The state token rendered per row, so a human sees the same distinction the
#: structured output carries: a cached hit is never shown as a confirmed one.
_STATE_CELLS = {
    core_where.PROJECT_PRESENT: "[green]present[/green]",
    core_where.PROJECT_ABSENT: "[red]absent (cached)[/red]",
    core_where.PROJECT_UNVERIFIED: "[yellow]unverified[/yellow]",
}


def _state_cell(project_state: str) -> str:
    return _STATE_CELLS.get(project_state, project_state)


def _match_to_json(match: WhereMatch) -> dict:
    """The historical per-record JSON shape (site records omit the app-only fields)."""
    if match.type == "site":
        return {
            "type": match.type,
            "project": match.project,
            "bench": match.bench,
            "name": match.name,
            "project_state": match.project_state,
        }
    return {
        "type": match.type,
        "project": match.project,
        "bench": match.bench,
        "name": match.name,
        "version": match.version,
        "branch": match.branch,
        "site": match.site,
        "installed": match.installed,
        "project_state": match.project_state,
    }


def where(
    search: str = typer.Argument(
        ...,
        help="Search string to match against app or site names (case-insensitive).",
    ),
    apps_only: bool = typer.Option(
        False,
        "--apps",
        "-a",
        help="Search only for apps.",
    ),
    sites_only: bool = typer.Option(
        False,
        "--sites",
        "-s",
        help="Search only for sites.",
    ),
    installed_only: bool = typer.Option(
        False,
        "--installed",
        "-i",
        help="Show only installed apps (not just available). Only applies to app search.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON.",
    ),
    no_verify: bool = typer.Option(
        False,
        "--no-verify",
        help="Skip the live check that each match's instance still exists.",
    ),
):
    """
    Search all cached instances for apps or sites matching a string.

    Examples:

        cwcli where erpnext          # Find all instances with 'erpnext'

        cwcli where payments --apps  # Find apps matching 'payments'

        cwcli where .local --sites   # Find sites matching '.local'
    """
    try:
        result = core_where.where(
            search,
            apps_only=apps_only,
            sites_only=sites_only,
            installed_only=installed_only,
            verify=not no_verify,
        )
    except CwcliError as e:
        console.print(f"[red]Error: {e.message}[/red]")
        raise typer.Exit(1) from None

    matches = result.data.matches if result.data else []

    if not matches:
        if not json_output:
            console.print(f"[yellow]No matches found for '{search}'.[/yellow]")
        else:
            typer.echo("[]")
        raise typer.Exit()

    if json_output:
        typer.echo(json.dumps([_match_to_json(m) for m in matches], indent=2))
        raise typer.Exit()

    # Display results in tables
    app_results = [m for m in matches if m.type == "app"]
    site_results = [m for m in matches if m.type == "site"]

    if app_results:
        table = Table(title=f"Apps matching '{search}'")
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("App", style="green")
        table.add_column("Version", style="dim")
        table.add_column("Branch", style="dim")
        table.add_column("Site", style="magenta")
        table.add_column("Instance", style="dim")

        for match in app_results:
            version = match.version or "-"
            branch = match.branch or "-"
            site = match.site or "(available)"
            table.add_row(
                match.project,
                match.name,
                version,
                branch,
                site,
                _state_cell(match.project_state),
            )

        console.print(table)

    if site_results:
        if app_results:
            console.print()  # Add spacing between tables

        table = Table(title=f"Sites matching '{search}'")
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("Site", style="green")
        table.add_column("Bench Path", style="dim")
        table.add_column("Instance", style="dim")

        for match in site_results:
            table.add_row(
                match.project,
                match.name,
                match.bench,
                _state_cell(match.project_state),
            )

        console.print(table)

    # Summary
    total = len(matches)
    console.print(f"\n[dim]Found {total} match{'es' if total != 1 else ''}.[/dim]")
    for warning in result.warnings:
        console.print(f"[yellow]{warning.text}[/yellow]")
