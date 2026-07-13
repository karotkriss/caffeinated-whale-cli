"""``cwcli axi`` - the agent-facing (AXI) surface over the shared logic core.

Each verb parses its flags, calls the SAME core function the human CLI calls,
serializes the returned DTO to TOON on stdout, and maps the result status (or a
raised :class:`CwcliError`) to an exit code (0 success, 1 error, 2 usage). It
adds no business logic and never prompts: a decision the core cannot resolve
from flags becomes a structured usage error on stdout, not an interactive
prompt. Progress/diagnostics go to stderr; stdout carries only TOON.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import typer

from ..core import backup as core_backup
from ..core.envelope import Choice
from ..core.envelope import Status as CoreStatus
from ..core.errors import CwcliError, ErrorKind
from ..utils import toon
from .list import _list_instances

app = typer.Typer(
    help="Agent-facing surface: structured TOON output on stdout, no interactive prompts."
)

_DESCRIPTION = "Manage Frappe and ERPNext Docker instances - structured and non-interactive."


# --------------------------------------------------------------------------- shared helpers


def exit_for(kind: ErrorKind) -> int:
    """Map an error kind to a process exit code (USAGE -> 2, everything else -> 1)."""
    return 2 if kind is ErrorKind.USAGE else 1


def emit_axi_error(error: CwcliError) -> None:
    """Render a typed error as structured stdout: ``error:`` + optional ``help:``."""
    typer.echo(f"error: {error.message}")
    if error.hint:
        typer.echo(f"help: {error.hint}")


def emit_axi_choice_as_usage_error(choice: Choice) -> None:
    """Render a needs-choice as a usage error naming the flag the agent must pass."""
    if choice.kind == "select_bench":
        typer.echo("error: multiple benches; pass --bench <index|label>")
        for option in choice.options or []:
            typer.echo(f"  [{option['value']}] {option['label']}")
        typer.echo("help: re-run with --bench <index|label>")
    elif choice.kind == "confirm_start":
        typer.echo(f"error: {choice.prompt}")
        typer.echo("help: start it first with 'cwcli start <project>'")
    else:  # pragma: no cover - defensive; only two choice kinds exist today
        typer.echo(f"error: a decision is required: {choice.prompt}")


def _collapse_home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def _bin_path() -> str:
    """Absolute path of the current executable, with the home dir collapsed to ``~``."""
    raw = sys.argv[0] or "cwcli"
    try:
        resolved = str(Path(raw).resolve())
    except OSError:  # pragma: no cover - resolve is robust in practice
        resolved = raw
    return _collapse_home(resolved)


# ------------------------------------------------------------------------------------ home


@app.callback(invoke_without_command=True)
def home(ctx: typer.Context) -> None:
    """Content-first home: identify the tool, show live instances, suggest next steps."""
    if ctx.invoked_subcommand is not None:
        return

    instances = _list_instances()
    lines = [toon.kv("bin", _bin_path()), toon.kv("description", _DESCRIPTION)]

    if instances:
        rows = [
            {
                "projectName": inst["projectName"],
                "status": inst["status"],
                "ports": " ".join(inst["ports"]) if inst["ports"] else "N/A",
            }
            for inst in instances
        ]
        lines.append(toon.table("instances", rows, ["projectName", "status", "ports"]))
    else:
        lines.append("instances: 0 Frappe instances found")

    lines.append(
        toon.block(
            "help",
            [
                "Run `cwcli axi backup <project> --site <site>` to back up a site's database",
                "Run `cwcli axi backup <project> --with-files` to include public/private files",
                "Run `cwcli ls` for the full human-readable instance table",
            ],
        )
    )
    typer.echo("\n".join(lines))
    raise typer.Exit(0)


# ---------------------------------------------------------------------------------- backup


@app.command("backup")
def axi_backup(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    site: str = typer.Option(
        None, "--site", "-s", help="Site to back up (default: the default site)."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    with_files: bool = typer.Option(
        False, "--with-files", help="Include public and private files."
    ),
) -> None:
    """Back up a site's database (and optionally files); emit the outcome as TOON."""
    try:
        result = core_backup.backup(project, site=site, bench=bench, with_files=with_files)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a BackupOutcome
    typer.echo(toon.encode(asdict(result.data), warnings=result.warnings))
    raise typer.Exit(0 if result.status is CoreStatus.OK else 1)
