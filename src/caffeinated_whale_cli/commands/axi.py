"""``cwcli axi`` - the agent-facing (AXI) surface over the shared logic core.

Each verb parses its flags, calls the SAME core function the human CLI calls,
serializes the returned DTO to TOON on stdout, and maps the result status (or a
raised :class:`CwcliError`) to an exit code (0 success, 1 error, 2 usage). It
adds no business logic and never prompts: a decision the core cannot resolve
from flags becomes a structured usage error on stdout, not an interactive
prompt.

``axi`` stdout is ALWAYS TOON, never JSON (JSON lives only on the human commands'
``--json`` flag). The shared emitters (:func:`emit_result`, :func:`emit_axi_error`,
:func:`emit_axi_choice_as_usage_error`, and the empty-state line) all emit valid
TOON - key:value ``error:``/``help:`` lines and proper ``name[N]:`` blocks, never
bare hardcoded lines - so success, error, needs-choice, and empty-state output
are uniformly TOON, and every later migrated verb inherits that. The spec pins
the error format as ``error: <message>`` (plus ``help: <hint>``); those two
diagnostics go through ``toon.kv``, which renders a plain message unquoted
(``error: <message>``, matching the spec) and only quotes it when it holds a
TOON-special character (``:`` ``"`` ``'`` ``,``, edge whitespace, a bare number)
- exactly what keeps a stray-colon message parseable. Progress/diagnostics go to
stderr; stdout carries only TOON.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import typer

from ..core import backup as core_backup
from ..core import list as core_list
from ..core import start as core_start
from ..core import status as core_status
from ..core import where as core_where
from ..core.envelope import Choice
from ..core.envelope import Status as CoreStatus
from ..core.errors import CwcliError, ErrorKind
from ..utils import toon

app = typer.Typer(
    help="Agent-facing surface: structured TOON output on stdout, no interactive prompts."
)

_DESCRIPTION = "Manage Frappe and ERPNext Docker instances - structured and non-interactive."


# --------------------------------------------------------------------------- shared helpers


def exit_for(kind: ErrorKind) -> int:
    """Map an error kind to a process exit code (USAGE -> 2, everything else -> 1)."""
    return 2 if kind is ErrorKind.USAGE else 1


def emit_result(data, *, warnings=None) -> None:
    """Emit a single DTO as TOON, folding in any warnings."""
    typer.echo(toon.encode(asdict(data), warnings=warnings or []))


def emit_axi_error(error: CwcliError) -> None:
    """Render a typed error as TOON: an ``error:`` line plus an optional ``help:`` line.

    Both go through ``toon.kv`` so a plain message stays ``error: <message>`` (the
    spec format) while a message carrying a TOON-special character is quoted, so
    the line stays parseable rather than being mis-split by a strict TOON reader.
    """
    typer.echo(toon.kv("error", error.message))
    if error.hint:
        typer.echo(toon.kv("help", error.hint))


def _choice_error_message(choice: Choice) -> str:
    if choice.kind == "select_bench":
        return "multiple benches; pass --bench <index|label>"
    if choice.kind == "confirm_start":
        return choice.prompt
    return f"a decision is required: {choice.prompt}"


def emit_axi_choice_as_usage_error(choice: Choice) -> None:
    """Render a needs-choice as a TOON usage error naming the flag the agent must pass.

    The available benches are emitted as a proper ``options[N]:`` TOON block (not
    bare indented lines), so the whole document stays uniformly TOON-parseable.
    """
    typer.echo(toon.kv("error", _choice_error_message(choice)))
    if choice.kind == "select_bench":
        options = [f"[{o['value']}] {o['label']}" for o in choice.options or []]
        typer.echo(toon.block("options", options))
        typer.echo(toon.kv("help", "re-run with --bench <index|label>"))
    elif choice.kind == "confirm_start":
        typer.echo(toon.kv("help", "start it first with 'cwcli start <project>'"))


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


def _instance_rows(instances) -> list[dict]:
    """DTO -> flat TOON-table rows (ports space-joined into a single scalar cell)."""
    return [
        {
            "projectName": i.project_name,
            "status": i.status,
            "ports": " ".join(i.ports) if i.ports else "N/A",
        }
        for i in instances
    ]


def _instances_toon(instances) -> str:
    """The ``instances`` TOON block, or a definitive empty-state line."""
    if instances:
        return toon.table(
            "instances", _instance_rows(instances), ["projectName", "status", "ports"]
        )
    return toon.kv("instances", "0 Frappe instances found")


# ------------------------------------------------------------------------------------ home


@app.callback(invoke_without_command=True)
def home(ctx: typer.Context) -> None:
    """Content-first home: identify the tool, show live instances, suggest next steps."""
    if ctx.invoked_subcommand is not None:
        return

    try:
        instances = core_list.list_instances().data
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None
    assert instances is not None

    lines = [
        toon.kv("bin", _bin_path()),
        toon.kv("description", _DESCRIPTION),
        _instances_toon(instances),
        toon.block(
            "help",
            [
                "Run `cwcli axi backup <project> --site <site>` to back up a site's database",
                "Run `cwcli axi ls` to list instances",
                "Run `cwcli axi where <term>` to search cached apps and sites",
            ],
        ),
    ]
    typer.echo("\n".join(lines))
    raise typer.Exit(0)


# -------------------------------------------------------------------------------------- ls


@app.command("ls")
def axi_ls() -> None:
    """List all Frappe/ERPNext instances; emit them as TOON."""
    try:
        instances = core_list.list_instances().data
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None
    assert instances is not None

    typer.echo(_instances_toon(instances))
    raise typer.Exit(0)


# ----------------------------------------------------------------------------------- where


@app.command("where")
def axi_where(
    search: str = typer.Argument(..., help="Match against cached app or site names."),
    apps_only: bool = typer.Option(False, "--apps", "-a", help="Search only for apps."),
    sites_only: bool = typer.Option(False, "--sites", "-s", help="Search only for sites."),
    installed_only: bool = typer.Option(
        False, "--installed", "-i", help="Show only installed apps (app search only)."
    ),
) -> None:
    """Search cached instances for apps/sites matching a string; emit matches as TOON."""
    try:
        result = core_where.where(
            search, apps_only=apps_only, sites_only=sites_only, installed_only=installed_only
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    assert result.data is not None
    emit_result(result.data, warnings=result.warnings)
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
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0 if result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)


# ----------------------------------------------------------------------------- start


@app.command("start")
def axi_start(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Auto-resolve port conflicts by stopping conflicting Frappe projects.",
    ),
) -> None:
    """Start a project's containers + bench; emit the outcome as TOON (never prompts).

    An already-running bench is a clean no-op (``already_running: true``). Port
    conflicts are surfaced as a ``CONFLICT`` error naming ``--yes`` (which
    auto-resolves conflicting Frappe projects); a multi-bench project with no
    ``--bench`` is a usage error naming ``--bench``.
    """
    _axi_resolve_port_conflicts(project, yes)

    try:
        result = core_start.start(project, bench=bench)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a StartOutcome
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0 if result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)


def _axi_resolve_port_conflicts(project: str, yes: bool) -> None:
    """Never-prompt host-side port pre-step for ``axi start`` (D6).

    Only meaningful when the container is not already up (a running instance owns
    its ports). A non-Frappe holder cannot be auto-resolved -> ``CONFLICT``; a
    Frappe holder is a ``CONFLICT`` naming ``--yes`` unless ``--yes`` was passed,
    in which case the conflicting Frappe projects are stopped (stdout stays TOON).
    """
    from .start import _frappe_running, detect_port_conflicts

    if _frappe_running(project):
        return
    conflicting, non_frappe = detect_port_conflicts(project)
    if non_frappe:
        emit_axi_error(
            CwcliError(
                ErrorKind.CONFLICT,
                "port.conflict_external",
                f"Ports {', '.join(str(p) for p in non_frappe)} are held by non-Frappe "
                "processes; stop them before starting this project.",
            )
        )
        raise typer.Exit(exit_for(ErrorKind.CONFLICT))
    if conflicting:
        if not yes:
            emit_axi_error(
                CwcliError(
                    ErrorKind.CONFLICT,
                    "port.conflict_frappe",
                    f"Ports needed by '{project}' are in use by other Frappe "
                    f"projects: {', '.join(conflicting)}.",
                    hint="pass --yes to stop the conflicting Frappe project(s)",
                )
            )
            raise typer.Exit(exit_for(ErrorKind.CONFLICT))
        # --yes: stop the conflicting Frappe projects (running -> stdout-silent).
        from .stop import _stop_project

        for proj in conflicting:
            _stop_project(proj, verbose=False)

        # Re-check ALL ports after stopping (mirrors _check_port_conflicts' post-
        # stop recheck): a teardown race or a non-Frappe process could still hold
        # them, and core.start's container.start() has no CwcliError of its own
        # for a port already in use - so a residual conflict must be caught here,
        # not left to surface as a raw docker.errors.APIError.
        remaining_conflicting, remaining_non_frappe = detect_port_conflicts(project)
        if remaining_conflicting or remaining_non_frappe:
            emit_axi_error(
                CwcliError(
                    ErrorKind.CONFLICT,
                    "port.conflict_after_stop",
                    f"Ports needed by '{project}' are still in use after stopping "
                    "conflicting Frappe projects.",
                )
            )
            raise typer.Exit(exit_for(ErrorKind.CONFLICT))


# ---------------------------------------------------------------------------- status


@app.command("status")
def axi_status(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
) -> None:
    """Report a project's per-process health; emit the report as TOON (``overall`` first)."""
    try:
        result = core_status.status(project, bench=bench)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a StatusReport
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0)
