"""``cwcli new-bench <project> <name>``: add a bench to a running instance, the
human renderer over ``core.new_bench``."""

from __future__ import annotations

import os
import sys

import typer

from ..core import new_bench as core_new_bench
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.init import InitOutput, InitStepStart
from ..utils import cache
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running


@handle_docker_errors
def new_bench(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench_name: str = typer.Argument(..., help="The new bench's folder name, e.g. bench-v15."),
    site: str = typer.Option(
        None, "--site", help="The bench's first site (default: <name>.localhost)."
    ),
    version: str = typer.Option(
        "16", "--version", help="Frappe version: a major (16) or a tag (16.3.0)."
    ),
    frappe_branch: str = typer.Option(
        None, "--frappe-branch", help="A raw Frappe git ref instead of --version."
    ),
    frappe_url: str = typer.Option(
        None,
        "--frappe-url",
        help="A Frappe fork to build from (private forks use the credential bridge).",
    ),
    erpnext: bool = typer.Option(False, "--erpnext", help="Install ERPNext on the first site."),
    apps: list[str] = typer.Option(
        None,
        "--app",
        "-a",
        help="An extra app (git URL or name) to fetch and install on the first site (repeatable).",
    ),
    admin_password: str = typer.Option(
        None,
        "--admin-password",
        help="Administrator password (prefer CWCLI_ADMIN_PASSWORD or the prompt).",
    ),
    db_root_password: str = typer.Option(
        None,
        "--db-root-password",
        help="The instance's EXISTING MariaDB root password (checked before any work; it does not set a new one); default: the one saved at init, else 123.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start a stopped instance without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Stream bench's output."),
):
    """Add a bench to an existing instance: the same provisioning as init's bench 0
    (bench init for the version or fork, configs, first site, optional ERPNext),
    then its supervisor and any extra apps.

    Examples:
        CWCLI_ADMIN_PASSWORD=... cwcli new-bench my-project bench-v15 --version 15 --erpnext
        cwcli new-bench my-project hrms-bench --app https://github.com/frappe/hrms
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    password = admin_password or os.environ.get("CWCLI_ADMIN_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            stderr_console.print(
                "[bold red]Error:[/bold red] an Administrator password is required: set CWCLI_ADMIN_PASSWORD or pass --admin-password."
            )
            raise typer.Exit(code=2)
        password = typer.prompt(
            "Administrator password for the first site", hide_input=True, confirmation_prompt=True
        )

    def on_event(event) -> None:
        if isinstance(event, InitStepStart) and (event.message or event.phase):
            stderr_console.print(f"[dim]{event.message or event.phase}[/dim]")
        elif verbose and isinstance(event, InitOutput):
            stderr_console.print(event.text, end="")

    try:
        result = core_new_bench.new_bench(
            project_name,
            bench_name=bench_name,
            site_name=site or f"{bench_name}.localhost",
            admin_password=password,
            frappe_ref=frappe_branch,
            version=version,
            frappe_url=frappe_url,
            install_erpnext=erpnext,
            apps=list(apps or []),
            db_root_password=db_root_password or os.environ.get("CWCLI_DB_ROOT_PASSWORD"),
            after_init=lambda path: cache.recache_project(
                project_name, bench_path=path, warnings=[]
            ),
            on_event=on_event,
        )
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        if e.hint:
            stderr_console.print(f"[dim]{e.hint}[/dim]")
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
    console.print(
        f"[bold green]✓[/bold green] Bench {report.bench_name} created at {report.bench_path} ({report.frappe_ref}), site {report.site}"
        + (", ERPNext installed" if report.erpnext_installed else "")
        + (", processes started" if report.started else ", NOT started")
        + (f", apps: {', '.join(report.apps_installed)}" if report.apps_installed else "")
        + (f", failed apps: {', '.join(report.apps_failed)}" if report.apps_failed else "")
    )
    raise typer.Exit(code=1 if report.apps_failed or not report.started else 0)
