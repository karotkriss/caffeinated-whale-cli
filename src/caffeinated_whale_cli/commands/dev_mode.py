"""``cwcli dev-mode <project> <site> on|off``: developer mode for one site (or the
whole bench with ``--common``), the renderer over ``core.dev_mode``."""

from __future__ import annotations

import typer

from ..core import dev_mode as core_dev_mode
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils.completion_utils import complete_project_names, complete_site_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running, resolve_bench_path


@handle_docker_errors
def dev_mode(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Argument(..., help="The site.", autocompletion=complete_site_names),
    state: str = typer.Argument(None, help="'on' or 'off'; omit to show the current state."),
    bench: str = typer.Option(None, "--bench", help="Which bench: its numeric index or label."),
    common: bool = typer.Option(
        False,
        "--common",
        help="Set it bench-wide in common_site_config.json instead of the site's site_config.json.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation (and auto-start a stopped instance)."
    ),
):
    """Show, turn on, or turn off developer mode for a site.

    Developer mode makes customisations save to the app's source files, shows full
    tracebacks in the browser and rebuilds assets on change. Frappe reads it from
    common_site_config.json (bench-wide) with site_config.json overriding, so the
    read names the source of the value you see.

    Examples:
        cwcli dev-mode my-project example.localhost
        cwcli dev-mode my-project example.localhost on
        cwcli dev-mode my-project example.localhost off --common
    """
    if state is not None and state.lower() not in ("on", "off"):
        stderr_console.print("[bold red]Error:[/bold red] state must be 'on' or 'off'.")
        raise typer.Exit(code=2)
    ensure_containers_running(project_name, require_running=True, auto_start=yes)
    bench_path = resolve_bench_path(project_name, bench, None)
    if state is None:
        try:
            current = core_dev_mode.read_dev_mode(
                project_name, bench_path or "/workspace/frappe-bench", site
            )
        except CwcliError as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            raise typer.Exit(code=1) from e
        where = {
            "site": "set in site_config.json",
            "common": "set bench-wide in common_site_config.json",
            "default": "not set (Frappe's default)",
        }[current.source]
        console.print(
            f"developer mode for {site}: [bold]{'on' if current.effective else 'off'}[/bold] ({where}{'' if current.verified else ', from the cache'})"
        )
        return
    on = state.lower() == "on"
    consent = yes
    while True:
        try:
            result = core_dev_mode.set_dev_mode(
                project_name,
                site=site,
                bench_path=bench_path,
                on=on,
                scope="common" if common else "site",
                consent=consent,
            )
        except CwcliError as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            if e.hint:
                stderr_console.print(f"[dim]{e.hint}[/dim]")
            raise typer.Exit(code=1) from e
        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "confirm_dev_mode"
        ):
            if not typer.confirm(result.choice.prompt, default=False):
                console.print("Left as it was.")
                return
            consent = True
            continue
        if result.status is Status.NEEDS_CHOICE:
            stderr_console.print(
                f"[bold red]Error:[/bold red] {result.choice.prompt if result.choice else 'a choice is needed'}"
            )
            raise typer.Exit(code=1)
        break
    outcome = result.data
    assert outcome is not None
    for w in result.warnings:
        stderr_console.print(f"[yellow]{w.text}[/yellow]")
    console.print(
        f"developer mode for {site}: [bold]{'on' if outcome.after.effective else 'off'}[/bold] (source: {outcome.after.source}{'' if outcome.cache_cleared else '; cache NOT cleared'})"
    )
    if not outcome.cache_cleared:
        raise typer.Exit(code=1)
