import typer
from ..utils.db_utils import get_cached_project_data, get_bench_by_alias

app = typer.Typer(name="apps", help="Commands for working with bench aliases and apps")


from typing import Optional
from rich.console import Console
from rich.table import Table

@app.command("list-apps")
def list_apps(
    ctx: typer.Context,
    project_name: Optional[str] = typer.Argument(
        None,
        help="The Docker Compose project to query (optional if alias is unique).",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Just output app names, one per line.",
        rich_help_panel="Output Formatting",
    ),
) -> None:
    """
    List available apps for the specified bench alias (project optional when alias is unique).
    By default outputs a table of app, version, branch, and sites.
    """
    console = Console()
    bench_alias = ctx.obj.get("bench")
    if not bench_alias:
        typer.echo("Error: --bench <alias> is required to list apps.", err=True)
        raise typer.Exit(code=1)

    if project_name:
        cache = get_cached_project_data(project_name)
        if not cache:
            typer.echo(
                f"No cached data for project '{project_name}'. Run 'cwcli inspect {project_name}' first.",
                err=True,
            )
            raise typer.Exit(code=1)

        benches = [b for b in cache["bench_instances"] if b.get("alias") == bench_alias]
        if not benches:
            typer.echo(
                f"No bench with alias '{bench_alias}' found in project '{project_name}'.",
                err=True,
            )
            raise typer.Exit(code=1)

        bench = benches[0]
    else:
        # Global lookup by alias
        data = get_bench_by_alias(bench_alias)
        if not data:
            typer.echo(f"No bench with alias '{bench_alias}' found in cache.", err=True)
            raise typer.Exit(code=1)

        project_name = data["project_name"]
        bench = data["bench"]

    # Collect detailed info from cached site/app entries
    apps_info: dict = {}
    for site_data in bench.get("sites", []):
        site_name = site_data.get("name")
        for app_entry in site_data.get("installed_apps", []):
            parts = app_entry.split(maxsplit=2)
            if len(parts) == 3:
                name, version, branch = parts
            else:
                name = parts[0]
                version = parts[1] if len(parts) > 1 else ""
                branch = parts[2] if len(parts) > 2 else ""
            info = apps_info.setdefault(name, {"version": version, "branch": branch, "sites": set()})
            info["sites"].add(site_name)

    if quiet:
        for name in sorted(apps_info):
            typer.echo(name)
        return

    # Render table
    table = Table(title=f"Apps for bench '{bench_alias}'")
    table.add_column("App", style="cyan", no_wrap=True)
    table.add_column("Version", style="green")
    table.add_column("Branch", style="magenta")
    table.add_column("Sites", style="yellow")

    for name in sorted(apps_info):
        info = apps_info[name]
        sites_str = ", ".join(sorted(info["sites"]))
        table.add_row(name, info["version"], info["branch"], sites_str)
    console.print(table)
