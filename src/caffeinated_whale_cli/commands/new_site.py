"""``cwcli new-site <project> <site>``: a new site on an existing bench, the human
renderer over ``core.new_site``."""

from __future__ import annotations

import os
import sys

import typer

from ..core import new_site as core_new_site
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.init import InitOutput, InitStepStart
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from . import deps_ui
from .utils import ensure_containers_running, resolve_bench_path


@handle_docker_errors
def new_site(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    site: str = typer.Argument(..., help="The new site's name, e.g. shop.localhost."),
    bench: str = typer.Option(None, "--bench", help="Which bench: its numeric index or label."),
    apps: list[str] = typer.Option(
        None,
        "--app",
        "-a",
        help="An app to install on the site (repeatable): a bench app's name, or a git URL "
        "to fetch onto the bench first.",
    ),
    default: bool = typer.Option(
        False, "--default", help="Make it the bench's default site (bench use)."
    ),
    dev_mode: bool = typer.Option(
        False, "--dev-mode", help="Turn developer mode on for the new site."
    ),
    admin_password: str = typer.Option(
        None,
        "--admin-password",
        help="Administrator password (prefer the CWCLI_ADMIN_PASSWORD env var or the prompt; a flag lands on the argv).",
    ),
    db_root_password: str = typer.Option(
        None,
        "--db-root-password",
        help="The instance's EXISTING MariaDB root password (checked before any work; it does not set a new one); default: the one saved at init, else 123.",
    ),
    with_deps: bool = typer.Option(False, "--with-deps", help=deps_ui.WITH_DEPS_HELP),
    ignore_frappe_range: bool = typer.Option(
        False, "--ignore-frappe-range", help=deps_ui.IGNORE_RANGE_HELP
    ),
    dep_source: list[str] = typer.Option(None, "--dep-source", help=deps_ui.DEP_SOURCE_HELP),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start a stopped instance without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Stream bench's output."),
):
    """Create a site on an existing bench: bench new-site, install-app per --app,
    bench use with --default, developer mode with --dev-mode.

    Examples:
        CWCLI_ADMIN_PASSWORD=... cwcli new-site my-project shop.localhost --app erpnext --default
        cwcli new-site my-project shop.localhost --bench 1 --dev-mode
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    bench_path = resolve_bench_path(project_name, bench, None, verbose=verbose)
    password = admin_password or os.environ.get("CWCLI_ADMIN_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            stderr_console.print(
                "[bold red]Error:[/bold red] an Administrator password is required: set CWCLI_ADMIN_PASSWORD or pass --admin-password."
            )
            raise typer.Exit(code=2)
        password = typer.prompt(
            "Administrator password for the new site", hide_input=True, confirmation_prompt=True
        )

    def on_event(event) -> None:
        if isinstance(event, InitStepStart) and event.message:
            stderr_console.print(f"[dim]{event.message}[/dim]")
        elif verbose and isinstance(event, InitOutput):
            stderr_console.print(event.text, end="")

    picked = [a for a in (apps if isinstance(apps, list) else []) if a]
    # A URL is fetched onto the bench first; a name must already be on it.
    urls = [a for a in picked if "/" in a or ":" in a]

    def _create(**answers):
        return core_new_site.new_site(
            project_name,
            site=site,
            admin_password=password,
            bench_path=bench_path,
            apps=[a for a in picked if a not in urls],
            fetch=urls,
            default=default,
            developer_mode=dev_mode,
            db_root_password=db_root_password or os.environ.get("CWCLI_DB_ROOT_PASSWORD"),
            ignore_frappe_range=ignore_frappe_range is True,
            on_event=on_event,
            **answers,
        )

    try:
        result = deps_ui.run_with_answers(
            _create,
            with_deps=with_deps is True,
            dep_sources=deps_ui.parse_dep_sources(
                dep_source if isinstance(dep_source, list) else None
            ),
            interactive=deps_ui.interactive(),
        )
    except CwcliError as e:
        deps_ui.render_error(e)
        raise typer.Exit(code=1) from e
    if result.status is Status.NEEDS_CHOICE:
        stderr_console.print(
            f"[bold red]Error:[/bold red] {result.choice.prompt if result.choice else 'a choice is needed'}"
        )
        raise typer.Exit(code=1)
    report = result.data
    assert report is not None
    for w in result.warnings:
        stderr_console.print(f"[yellow]{w.text}[/yellow]")
    from ..utils import cache

    cache.recache_project(project_name, bench_path=report.bench_path, warnings=[])
    console.print(
        f"[bold green]✓[/bold green] Site {report.site} created on {report.bench_path}"
        + (f", apps: {', '.join(report.apps_installed)}" if report.apps_installed else "")
        + (" (default site)" if report.default_set else "")
        + (" (developer mode on)" if report.developer_mode else "")
    )
    console.print(f"Open it with: cwcli open {project_name}  ·  or in the web UI")
