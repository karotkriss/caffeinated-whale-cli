"""
Search all cached instances for apps or sites by name.

Provides a quick way to find which projects contain a specific app or site.
"""

import json

import typer
from rich.console import Console
from rich.table import Table

from ..utils import db_utils

console = Console()


def _search_apps(search_term: str) -> list[dict]:
    """
    Search for apps across all projects.

    Args:
        search_term: String to search for (case-insensitive).

    Returns:
        List of matches with project, bench, app name, and install details.
    """
    db_utils.initialize_database()
    results = []
    search_lower = search_term.lower()

    # Search available apps
    available_apps = (
        db_utils.AvailableApp.select(db_utils.AvailableApp, db_utils.Bench, db_utils.Project)
        .join(db_utils.Bench)
        .join(db_utils.Project)
    )

    for app_record in available_apps:
        if search_lower in app_record.name.lower():
            results.append(
                {
                    "type": "app",
                    "project": app_record.bench.project.name,
                    "bench": app_record.bench.path,
                    "name": app_record.name,
                    "version": None,
                    "branch": None,
                    "site": None,
                    "installed": False,
                }
            )

    # Search installed apps (with version/branch info)
    installed_apps = (
        db_utils.InstalledAppDetail.select(
            db_utils.InstalledAppDetail, db_utils.Site, db_utils.Bench, db_utils.Project
        )
        .join(db_utils.Site)
        .join(db_utils.Bench)
        .join(db_utils.Project)
    )

    for app_record in installed_apps:
        if search_lower in app_record.name.lower():
            results.append(
                {
                    "type": "app",
                    "project": app_record.site.bench.project.name,
                    "bench": app_record.site.bench.path,
                    "name": app_record.name,
                    "version": app_record.version or None,
                    "branch": app_record.branch or None,
                    "site": app_record.site.name,
                    "installed": True,
                }
            )

    return results


def _search_sites(search_term: str) -> list[dict]:
    """
    Search for sites across all projects.

    Args:
        search_term: String to search for (case-insensitive).

    Returns:
        List of matches with project, bench, and site name.
    """
    db_utils.initialize_database()
    results = []
    search_lower = search_term.lower()

    sites = (
        db_utils.Site.select(db_utils.Site, db_utils.Bench, db_utils.Project)
        .join(db_utils.Bench)
        .join(db_utils.Project)
    )

    for site in sites:
        if search_lower in site.name.lower():
            results.append(
                {
                    "type": "site",
                    "project": site.bench.project.name,
                    "bench": site.bench.path,
                    "name": site.name,
                }
            )

    return results


def _deduplicate_app_results(results: list[dict]) -> list[dict]:
    """
    Deduplicate app results, preferring installed apps over available apps.

    When the same app appears as both available and installed in the same project,
    keep only the installed entries (which have more info like version/branch).
    """
    # First pass: collect all installed apps per project
    installed_by_project = {}
    for result in results:
        if result["installed"]:
            key = (result["project"], result["name"])
            if key not in installed_by_project:
                installed_by_project[key] = []
            installed_by_project[key].append(result)

    # Second pass: filter out "available" entries when installed version exists
    deduplicated = []
    seen_installed = set()

    for result in results:
        key = (result["project"], result["name"])
        if result["installed"]:
            # Include all installed entries (different sites may have same app)
            site_key = (result["project"], result["name"], result.get("site"))
            if site_key not in seen_installed:
                seen_installed.add(site_key)
                deduplicated.append(result)
        else:
            # Only include "available" if no installed version exists in this project
            if key not in installed_by_project:
                deduplicated.append(result)

    return deduplicated


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
):
    """
    Search all cached instances for apps or sites matching a string.

    Examples:

        cwcli where erpnext          # Find all instances with 'erpnext'

        cwcli where payments --apps  # Find apps matching 'payments'

        cwcli where .local --sites   # Find sites matching '.local'
    """
    # Validate mutually exclusive options
    if apps_only and sites_only:
        console.print("[red]Error: Cannot use --apps and --sites together.[/red]")
        raise typer.Exit(1)

    results = []

    # Determine what to search
    search_apps = not sites_only
    search_sites = not apps_only

    if search_apps:
        app_results = _search_apps(search)
        if installed_only:
            app_results = [r for r in app_results if r.get("installed")]
        app_results = _deduplicate_app_results(app_results)
        results.extend(app_results)

    if search_sites:
        site_results = _search_sites(search)
        results.extend(site_results)

    if not results:
        if not json_output:
            console.print(f"[yellow]No matches found for '{search}'.[/yellow]")
        else:
            typer.echo("[]")
        raise typer.Exit()

    # Sort results by project, then type, then name
    results.sort(key=lambda x: (x["project"], x["type"], x["name"]))

    if json_output:
        typer.echo(json.dumps(results, indent=2))
        raise typer.Exit()

    # Display results in tables
    app_results = [r for r in results if r["type"] == "app"]
    site_results = [r for r in results if r["type"] == "site"]

    if app_results:
        table = Table(title=f"Apps matching '{search}'")
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("App", style="green")
        table.add_column("Version", style="dim")
        table.add_column("Branch", style="dim")
        table.add_column("Site", style="magenta")

        for result in app_results:
            version = result.get("version") or "-"
            branch = result.get("branch") or "-"
            site = result.get("site") or "(available)"
            table.add_row(
                result["project"],
                result["name"],
                version,
                branch,
                site,
            )

        console.print(table)

    if site_results:
        if app_results:
            console.print()  # Add spacing between tables

        table = Table(title=f"Sites matching '{search}'")
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("Site", style="green")
        table.add_column("Bench Path", style="dim")

        for result in site_results:
            table.add_row(
                result["project"],
                result["name"],
                result["bench"],
            )

        console.print(table)

    # Summary
    total = len(results)
    console.print(f"\n[dim]Found {total} match{'es' if total != 1 else ''}.[/dim]")
