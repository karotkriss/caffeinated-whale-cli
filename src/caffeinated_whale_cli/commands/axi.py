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

import os
import sys
from dataclasses import asdict
from pathlib import Path

import click
import typer
from typer import core as _typer_core
from typer import rich_utils as _rich_utils

from ..core import apps as core_apps
from ..core import backup as core_backup
from ..core import bench_ops as core_bench_ops
from ..core import config as core_config
from ..core import init as core_init
from ..core import inspect as core_inspect
from ..core import label as core_label
from ..core import list as core_list
from ..core import logs as core_logs
from ..core import restart as core_restart
from ..core import start as core_start
from ..core import status as core_status
from ..core import stop as core_stop
from ..core import unlock as core_unlock
from ..core import update as core_update
from ..core import version as core_version
from ..core import where as core_where
from ..core.envelope import Choice
from ..core.envelope import Status as CoreStatus
from ..core.errors import CwcliError, ErrorKind
from ..utils import agent_hooks, cache, toon

# --------------------------------------------------------------- parse-error -> TOON layer
#
# Typer/click reject an unknown flag / missing argument / missing required option
# BEFORE any verb body runs, so the TOON emitters below never see them: the default
# is a rich panel on STDERR with empty STDOUT, exit 2, and no list of the command's
# valid flags. On an agent surface that is an AXI section-6 violation (structured
# errors belong on stdout; unrecognized input must fail loud, list the valid flags,
# and let the agent self-correct in one turn). This group class closes that gap by
# rendering axi-surface parse failures as the same `error:`+`help:` TOON the core
# errors already use, while leaving the human CLI's rich rendering untouched.


def _usage_error_message(error: click.UsageError) -> str:
    """The click message, minus typer's noisy empty-envvar suffix."""
    return error.format_message().replace(" (env var: 'None')", "")


def _param_metavar(param, ctx) -> str:
    try:
        return str(param.make_metavar(ctx))
    except TypeError:  # click < 8.2 signature
        return str(param.make_metavar())
    except Exception:  # pragma: no cover - defensive
        return str(param.name).upper()


def _usage_help_lines(error: click.UsageError) -> list[str]:
    """A one-line ``usage:`` string naming the command's arguments and valid flags,
    so an agent can self-correct in a single turn."""
    ctx = getattr(error, "ctx", None)
    if ctx is None:  # pragma: no cover - axi usage errors always carry a ctx
        return []
    args: list[str] = []
    opts: list[str] = []
    for param in ctx.command.get_params(ctx):
        if isinstance(param, click.Argument):
            args.append(_param_metavar(param, ctx))
        elif isinstance(param, click.Option):
            opts.append("[" + "/".join(param.opts + param.secondary_opts) + "]")
    return ["usage: " + " ".join([ctx.command_path, *args, *opts])]


def emit_usage_error_as_toon(error: click.UsageError) -> None:
    """Render a click parse failure as TOON on STDOUT: an ``error:`` line plus a
    ``help:`` usage line naming the command's valid flags.

    Mirrors :func:`emit_axi_error`'s stdout purity, so the axi surface stays
    uniformly TOON even when Typer's own parser rejects the input.
    """
    typer.echo(toon.kv("error", _usage_error_message(error)))
    lines = _usage_help_lines(error)
    if lines:
        typer.echo(toon.block("help", lines))


def _ctx_under_axi(ctx) -> bool:
    """True when the failing command lives on the axi surface - mounted under the
    human CLI (``cwcli axi ...``) or invoked as the axi app directly (tests)."""
    while ctx is not None:
        if isinstance(ctx.command, AxiToonGroup):
            return True
        ctx = ctx.parent
    return False


class ToonGroup(_typer_core.TyperGroup):
    """A Typer group that renders axi-surface parse failures as TOON on stdout.

    It reproduces Typer's own standalone exit handling (calling ``super().main``
    with ``standalone_mode=False`` and re-driving the exits) but intercepts the
    ``ClickException`` branch: an axi-surface :class:`click.UsageError` becomes an
    ``error:``+``help:`` TOON document on stdout (exit 2 preserved), while every
    non-axi error keeps Typer's default rich rendering on stderr.
    """

    def main(
        self,
        args=None,
        prog_name=None,
        complete_var=None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra,
    ):
        try:
            rv = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.ClickException as error:
            if not standalone_mode:
                raise
            if isinstance(error, click.UsageError) and _ctx_under_axi(getattr(error, "ctx", None)):
                emit_usage_error_as_toon(error)
            elif self.rich_markup_mode is not None:
                _rich_utils.rich_format_error(error)
            else:
                error.show()
            sys.exit(error.exit_code)
        except click.exceptions.Abort:
            if not standalone_mode:
                raise
            if self.rich_markup_mode is not None:
                _rich_utils.rich_abort_error()
            else:
                typer.echo("Aborted!", err=True)
            sys.exit(1)
        if not standalone_mode:
            return rv
        # A subcommand that raised typer.Exit(n) surfaces here as rv=n; a plain
        # return surfaces as None (a clean exit 0), matching standalone Typer.
        sys.exit(rv if isinstance(rv, int) else 0)


class AxiToonGroup(ToonGroup):
    """Marker subclass tagging the axi app's group, so a parse failure anywhere in
    its subtree is detected as axi-surface regardless of mount depth."""


app = typer.Typer(
    cls=AxiToonGroup,
    help="Agent-facing surface: structured TOON output on stdout, no interactive prompts.",
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
    if choice.kind == "select_process":
        return "unknown or ambiguous process; pass --process <label>"
    if choice.kind == "confirm_start":
        # A statement, never the raw prompt: the prompt is phrased as an
        # interactive question ("... Start it?"), but this surface never prompts,
        # so a trailing "?" reads as a question nobody will answer. The actionable
        # remedy rides the `help:` line below.
        return "the project's Frappe container is not running"
    if choice.kind == "confirm_reuse_bench":
        # `axi init` only. The bench directory already exists and neither
        # --reuse-bench nor --no-reuse-bench was passed, so the agent must say
        # what to do; the flag names ride the `help:` line below.
        path = (choice.options or [{}])[0].get("label") or "the target bench"
        return f"bench '{path}' already exists; pass --reuse-bench or --no-reuse-bench"
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
    elif choice.kind == "select_process":
        options = [o["label"] for o in choice.options or []]
        typer.echo(toon.block("options", options))
        typer.echo(toon.kv("help", "re-run with --process <label>"))
    elif choice.kind == "confirm_start":
        typer.echo(toon.kv("help", "start it first with 'cwcli start <project>'"))
    elif choice.kind == "confirm_reuse_bench":
        typer.echo(
            toon.kv(
                "help",
                "re-run with --reuse-bench to reuse it, or --no-reuse-bench and a "
                "different --bench name to create a fresh bench",
            )
        )


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
                # The verb that answers every other verb's `--bench`. A discovery
                # verb an agent cannot discover would be half a fix.
                "Run `cwcli axi benches <project>` to list a project's benches for `--bench`",
                # This block is a curated FEW next steps, not the surface: the
                # home is the per-session hook payload, so it stays small. That
                # leaves the other verbs (start/status/restart/stop/label/apps/
                # self-update) unreachable without a route to them - this is it.
                "Run `cwcli axi --help` to see every verb",
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
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Skip the live check that each match's instance still exists."
    ),
) -> None:
    """Search cached instances for apps/sites matching a string; emit matches as TOON.

    Results come from the cache, which outlives the instances it describes. Every
    row carries ``project_state``: ``present`` (the instance was confirmed live
    just now), ``absent`` (it is cached but gone), or ``unverified`` (the check
    did not run). ``verified`` on the document says whether the live check
    answered at all - an unreachable Docker daemon degrades to ``unverified``
    rather than vouching. Do not act on an ``absent`` or ``unverified`` row
    without confirming with ``cwcli axi ls``.
    """
    try:
        result = core_where.where(
            search,
            apps_only=apps_only,
            sites_only=sites_only,
            installed_only=installed_only,
            verify=not no_verify,
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


# ---------------------------------------------------------------------------------- unlock


@app.command("unlock")
def axi_unlock(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    site: str = typer.Option(
        None, "--site", "-s", help="Site to unlock (default: the default site)."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
) -> None:
    """Remove a site's locks folder; emit the outcome as TOON.

    The removed paths are emitted as a structured ``removed`` list. A site that was
    not locked is a clean success (``already_unlocked: true``), not an error. A
    stopped container and a multi-bench project with no ``--bench`` are usage
    errors, exactly as ``axi backup`` reports them.
    """
    try:
        result = core_unlock.unlock(project, site=site, bench=bench)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries an UnlockOutcome
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0 if result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)


# ---------------------------------------------------------------------------------- stop


@app.command("stop")
def axi_stop(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
) -> None:
    """Stop a project's containers; emit the outcome as TOON.

    Idempotent: an already-stopped project is a definitive success
    (``already_stopped: true``), not an error, so an agent can stop twice safely.
    """
    try:
        result = core_stop.stop(project)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    assert result.data is not None  # OK always carries a StopOutcome
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0)


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
    autorestart: bool = typer.Option(
        True,
        "--autorestart/--no-autorestart",
        help="Self-heal crashed processes: supervisord restarts a program that "
        "crashes (not one that exits cleanly). Set at launch.",
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
        result = core_start.start(project, bench=bench, autorestart=autorestart)
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
        # --yes: stop the conflicting Frappe projects. Via `core.stop`, which cannot
        # print: the old `_stop_project` emitted rich markup to STDOUT on its
        # not-found / already-stopped branches, so a teardown race between the
        # detection above and this call could corrupt the one-TOON-document
        # contract. A project that vanished or stopped in that window is fine here
        # (its ports are free either way) - the recheck below is what decides.

        for proj in conflicting:
            try:
                core_stop.stop(proj)
            except CwcliError as e:
                if e.kind is not ErrorKind.NOT_FOUND:
                    raise

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


# ------------------------------------------------------------------------------- logs


@app.command("logs")
def axi_logs(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    lines: int = typer.Option(
        100, "--lines", "-n", help="Number of lines to read from the end of each log."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    process: str = typer.Option(
        None,
        "--process",
        "-p",
        help="Read ONE process's log (e.g. web, worker). Omit for every process.",
    ),
) -> None:
    """Read a bounded tail of a bench's per-process logs; emit them as ONE TOON document.

    A bounded ``tail -n N``, NOT a follow: this verb reads and returns, it never streams
    (a follow cannot terminate into one document). Output is a metadata head (``project``,
    ``bench_path``, ``container``, ``not_cwcli_supervised``, ``lines_requested``) then one
    raw-line block per process, so log lines carrying colons/commas cannot corrupt the
    document. With ``--process`` only that one process's block appears.

    A running-but-quiet bench (up, but nothing written yet) is a SUCCESSFUL empty read
    (exit 0) - the ``axi self-update --check``/``axi status`` precedent, diverging from the
    human ``cwcli logs`` which errors. A stopped project is a usage error (exit 2) naming
    ``cwcli start``, matching every bench-scoped verb; a multi-bench project with no
    ``--bench`` and an unknown ``--process`` are usage errors naming the flag. A running
    container whose bench has no live manager is an operational error (exit 1). There is
    deliberately no ``--follow`` (use ``cwcli logs`` for an interactive tail) and no ``--yes``.
    """
    try:
        result = core_logs.read_logs(project, bench=bench, lines=lines, process=process)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK always carries a LogsRead
    _emit_logs_read(result.data, warnings=result.warnings)
    raise typer.Exit(0)


def _emit_logs_read(read, *, warnings=None) -> None:
    """Render a ``LogsRead`` as one TOON document: metadata head + one raw-line block per
    process. Not ``emit_result``: ``toon.encode`` would fold each process's lines into an
    inline comma-joined scalar list, mangling multi-line logs. ``toon.block`` emits one raw
    line per indented row (the ``help``/``notes`` shape), which log lines never re-parse as.
    """
    head = [
        toon.kv("project", read.project),
        toon.kv("bench_path", read.bench_path),
        toon.kv("container", read.container_name),
        toon.kv("not_cwcli_supervised", read.not_cwcli_supervised),
        toon.kv("lines_requested", read.lines_requested),
    ]
    if read.logs:
        head.extend(toon.block(group.process, group.lines) for group in read.logs)
    else:
        head.append(toon.kv("logs", "0 log lines"))
    if warnings:
        head.append(toon.block("warnings", [w.text for w in warnings]))
    typer.echo("\n".join(head))


# --------------------------------------------------------------------------- restart


@app.command("restart")
def axi_restart(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    process: str = typer.Option(
        ..., "--process", "-p", help="Which Procfile process to restart (e.g. web, worker)."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
) -> None:
    """Restart ONE supervised process; emit the outcome as TOON (never prompts, no --watch).

    A one-shot single-program mutation (siblings keep running). ``--process`` is
    required; an unknown/ambiguous process is a usage error listing the valid
    labels, and a multi-bench project with no ``--bench`` is a usage error naming
    ``--bench``. Whole-stack restart is not an axi verb (use ``cwcli axi start``).
    """
    try:
        result = core_restart.restart_process(project, process, bench=bench)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a ProcessRestartOutcome
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0 if result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)


# ---------------------------------------------------------------------------- inspect


@app.command("inspect")
def axi_inspect(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    update: bool = typer.Option(
        False, "--update", help="Force a full re-inspect and refresh the cache."
    ),
    no_refresh: bool = typer.Option(
        False,
        "--no-refresh",
        help="Serve the cached data verbatim; zero container calls (may be stale).",
    ),
) -> None:
    """Inspect a project's benches, sites, and apps; emit the report as TOON.

    ONE tiered read, not a read/refresh verb pair: by default it serves the cache
    when fresh, runs the cheap read-only drift check when the containers are up,
    and escalates to a full re-inspect (persisted to the cache) only on real
    drift or a cache miss - orchestrating those tiers by hand is exactly the
    complexity the tiers exist to hide. ``--update`` forces the full re-inspect;
    ``--no-refresh`` serves the cache verbatim.

    ``served_from`` names the tier that answered (cache/partial/full); a drift
    escalation that can no longer discover the bench serves the cached data with
    ``degraded: true`` and a warning, exit 0 (WARNING is a completed read).

    Deliberately NO ``--yes``: an axi verb must never open a start-from-axi path.
    A stopped project on the refresh path is a usage error (exit 2) naming
    ``cwcli start``, matching every other stopped-project fork on this surface.
    """
    refresh = "full" if update else ("cache_only" if no_refresh else "auto")
    try:
        result = core_inspect.inspect(project, refresh=refresh)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries an InspectReport
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0)


# ---------------------------------------------------------------------------- benches


@app.command("benches")
def axi_benches(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
) -> None:
    """List a project's benches with their indices and labels; emit them as TOON.

    The discovery verb behind every other verb's ``--bench``: when a bench-scoped
    verb reports "multiple benches; pass --bench <index|label>", this is what
    answers it. Nothing else on the agent surface can - ``axi ls`` carries no bench
    data, and ``axi where`` only yields a bench path from a search you must already
    know an app or site name to run.

    Read-only; touches no container. A project that has never been inspected is a
    structured error naming ``cwcli axi inspect``, NOT an empty list: "not
    inspected yet" and "has zero benches" are different facts, and only one has a
    remedy - and since this batch the remedy is agent-native, not the human
    command.
    """
    try:
        result = core_label.list_benches(project)
    except CwcliError as error:
        if error.code == "benches.none_cached":
            # The core's hint names the human `cwcli inspect`; on the agent
            # surface the remedy is the axi verb (the dead end this batch closed).
            error = CwcliError(
                error.kind,
                error.code,
                error.message,
                hint=f"Run 'cwcli axi inspect {project}' first.",
            )
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    assert result.data is not None  # OK always carries a BenchList
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0)


# ------------------------------------------------------------------------------ label


@app.command("label")
def axi_label(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    set_label: str = typer.Option(None, "--set", help="The user label to assign."),
    clear: bool = typer.Option(False, "--clear", help="Remove the bench's user label."),
) -> None:
    """Set or clear a bench's durable user label; emit the outcome as TOON.

    Exactly one of ``--set`` or ``--clear`` is required. Listing is NOT a mode of
    this verb: use ``cwcli axi benches``. Never prompts, and never starts a stopped
    project - the marker lives inside the bench, so a stopped container is a
    structured error pointing at ``cwcli start``.
    """
    if set_label is not None and clear:
        emit_axi_error(
            CwcliError(
                ErrorKind.USAGE,
                "label.selector_conflict",
                "Use either --set or --clear, not both.",
            )
        )
        raise typer.Exit(exit_for(ErrorKind.USAGE))
    if set_label is None and not clear:
        emit_axi_error(
            CwcliError(
                ErrorKind.USAGE,
                "label.no_operation",
                "Pass --set <label> to assign a label, or --clear to remove one.",
                hint="run `cwcli axi benches <project>` to list benches and their labels",
            )
        )
        raise typer.Exit(exit_for(ErrorKind.USAGE))

    try:
        if clear:
            result = core_label.clear_label(project, bench=bench)
        else:
            assert set_label is not None  # narrowed by the guards above
            result = core_label.set_label(project, bench=bench, label=set_label)
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a LabelOutcome
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0 if result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)


# --------------------------------------------------------------------------- apps list

apps_app = typer.Typer(help="Manage Frappe apps: structured, non-interactive.")
app.add_typer(apps_app, name="apps")


@apps_app.command("list")
def axi_apps_list(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    sites: list[str] = typer.Option(
        None, "--site", help="List installed apps for the named site(s). Repeatable."
    ),
    installed: bool = typer.Option(
        False, "--installed", help="Also report apps installed per site (all sites by default)."
    ),
) -> None:
    """List a bench's available apps, and (with --installed/--site) installed per site.

    The read an agent could not do before: which apps exist on a bench, and which
    are installed on which site. `axi benches` answers `--bench`; this answers what
    is on the bench it names.

    A site whose read FAILED is reported as null and exits 1, never as an empty
    list: "no apps" and "could not tell" are different facts, and only one of them
    is honest to act on. The exit code reads `ok`, NOT the envelope status - a
    partial read failure is a WARNING-shaped envelope, and WARNING maps to 0
    everywhere else.

    A stopped project is a usage error (exit 2) naming `cwcli start`, matching
    `axi backup`/`axi unlock`/`axi apps update`. There is deliberately no --yes:
    starting a container is UI-coupled, so an agent composes `cwcli axi start`
    then this verb.

    `axi apps install`/`axi apps uninstall` deliberately do NOT exist yet
    (captain-locked, 2026-07-15): letting an agent destroy site data is a product
    decision on its own evidence, not a side effect of a refactor.
    """
    try:
        result = core_apps.list_apps(
            project,
            bench=bench,
            sites=list(sites) if sites else None,
            installed=installed,
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries an AppsListing
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0 if result.data.ok else 1)


# ------------------------------------------------------------------------- apps update


@apps_app.command("update")
def axi_apps_update(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    apps: list[str] = typer.Argument(
        ..., help="App name(s) to update. Use 'frappe' to update the framework."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    sites: list[str] = typer.Option(
        None, "--site", help="Narrow migration to the named site(s). Repeatable."
    ),
    clear_cache: bool = typer.Option(
        False, "--clear-cache", help="Clear cache for affected sites after migration."
    ),
    clear_website_cache: bool = typer.Option(
        False, "--clear-website-cache", help="Clear website cache for affected sites."
    ),
    build: bool = typer.Option(False, "--build", help="Build assets after updating apps."),
    skip_maintenance: bool = typer.Option(
        False, "--skip-maintenance", help="Skip maintenance mode during update."
    ),
    no_recache: bool = typer.Option(
        False, "--no-recache", help="Skip re-caching after app updates."
    ),
) -> None:
    """Update app(s) and migrate affected sites; emit the report as TOON.

    Blocks until the update finishes and emits ONE terminal document, exactly as
    `axi backup` does for a minutes-long `bench backup`. Progress is deliberately
    not streamed: N documents on stdout would break the one-TOON-document contract,
    and an agent needs a verdict it can branch on rather than a progress bar.

    ``failed_*`` and ``unknown_*`` are NOT the same thing and must not be collapsed:
    a failure can be retried, while an ``unknown_*`` item's stream was lost, so it
    MAY STILL BE RUNNING and retrying it can do real harm.

    A stopped project is a usage error (exit 2) naming `cwcli start`, exactly as
    `axi backup`/`axi unlock` already document. There is deliberately no --yes:
    starting a container is UI-coupled and the core stays UI-pure about it, so
    an agent composes `cwcli axi start` then this verb.

    There is no `axi update`: the deprecated `cwcli update` spelling is not worth an
    agent-facing verb.
    """
    try:
        result = core_update.update(
            project,
            list(apps),
            bench=bench,
            sites=list(sites) if sites else None,
            clear_cache=clear_cache,
            clear_website_cache=clear_website_cache,
            build=build,
            skip_maintenance=skip_maintenance,
            no_recache=no_recache,
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries an UpdateReport
    emit_result(result.data, warnings=result.warnings)
    # The exit code reads report.ok, NOT result.status: a partial update failure is
    # a WARNING-shaped envelope, and the shipped `0 if status in (OK, WARNING)`
    # pattern would report success for an update that half failed.
    raise typer.Exit(0 if result.data.ok else 1)


# ----------------------------------------------------------------------- apps checkout


def _checkout_narrate(event) -> None:
    """The git steps, to STDERR.

    Deliberately narrates ``AppsOutput`` (git's OWN bytes), where
    :func:`_init_narrate` deliberately does NOT. The asymmetry is real: init's
    raw output is thousands of lines of bench build noise an agent will not
    parse, and ``cwcli logs`` exists to serve it afterwards. A checkout runs two
    or three short git commands, nothing is logged anywhere afterwards (a git
    step is not a supervised process), and the ENTIRE reason a step failed lives
    in those bytes - "Your local changes to the following files would be
    overwritten by checkout" is what tells an agent to pass --reset or to stop.
    Dropping it would leave a failure reported as a bare ``ok: false``.

    Everything here goes to stderr, including git's stdout, so the one-TOON-
    document contract on stdout holds.
    """
    if isinstance(event, core_apps.AppsCommand):
        print(f"$ {event.command}", file=sys.stderr, flush=True)
    elif isinstance(event, core_apps.AppsOutput):
        print(event.text, end="", file=sys.stderr, flush=True)


@apps_app.command("checkout")
def axi_apps_checkout(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    # Named `app_name` because `app` is this module's Typer instance; the metavar
    # keeps the agent-visible usage line matching the human `cwcli apps checkout`.
    app_name: str = typer.Argument(
        ..., metavar="APP", help="The app whose in-instance checkout to update."
    ),
    ref: str = typer.Argument(..., help="Branch, tag, or commit to fetch and check out."),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
    reset: bool = typer.Option(
        False,
        "--reset",
        help="Hard-reset the working tree to the fetched ref (discards local edits in the checkout).",
    ),
) -> None:
    """Fetch and check out a ref into an app already in the bench; emit the report as TOON.

    The gap `apps install` (a fresh get-app clone) and `apps update` (the tracked
    upstream on every app) leave: putting ONE named branch, tag, or commit under
    test in the EXISTING apps/<app> checkout. There is no `axi run`, so this is
    the only agent-surface route to that step.

    It exists on the agent surface even though `axi apps install`/`uninstall` do
    not, and that is not an inconsistency. Their deferral (captain-locked
    2026-07-15) names ONE threat: an agent DESTROYING SITE DATA, because
    `bench uninstall-app` drops the app's tables. A checkout runs `git fetch`
    then `git checkout -B` inside apps/<app> - no bench command, no site, no SQL,
    no table. Decided on its own evidence 2026-07-20; see
    `openspec/changes/add-axi-apps-checkout-verb/`.

    Safety posture, each guard against a named threat:

    - NO --yes and no auto-start (an agent silently starting containers a user
      deliberately stopped): a stopped project is a usage error naming
      `cwcli start`, as every bench-scoped axi verb already does.
    - The app must ALREADY be a git checkout (a typo'd app name reading as a
      silent no-op or as an implicit install): absent -> NOT_FOUND/app.no_checkout.
    - A checkout that would OVERWRITE a modified file is refused unless --reset
      (a non-conflicting dirty file rides through at exit 0 - this is not
      blanket dirty-tree protection). That refusal is GIT's, not cwcli's -
      `git checkout -B` exits non-zero and leaves the file intact - so there is
      deliberately no redundant cwcli-side pre-check; this verb's job is to
      make it legible as a failed step and a non-zero exit.
    - --reset is the one destructive element and stays an explicit opt-in
      (irrecoverable loss of uncommitted work in apps/<app>), reported as its own
      `reset` row. It is kept rather than withheld because without it an agent
      can reach a dirty tree it has no agent-surface way out of.

    The private-repo fetch rides the SAME credential bridge as apps install and
    apps update, which means this verb borrows the host's `gh`/`glab` auth for an
    in-container fetch. That is inherited, not new: `axi apps update` already
    wraps its whole dispatch in that bridge. The raw token still never enters the
    container, and the bridge is inert for public repos.

    KNOWN GAP: the report does not carry the commit the checkout landed on, so an
    agent cannot confirm the resulting git state from the agent surface. That is
    deliberately deferred to a READ (per-app git state on `axi apps list`), which
    serves every app rather than only the one just checked out, instead of adding
    a field to the AppsReport shared with install/uninstall/update.
    """
    try:
        result = core_apps.checkout_app(
            project,
            app_name,
            ref,
            bench=bench,
            reset=reset,
            auto_start=False,
            on_event=_checkout_narrate,
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries an AppsReport
    report = result.data

    # A checkout changes the app's git state (and its reported version), so refresh
    # the cache whenever any git step ran, matching the human verb. Post-mutation
    # epilogue gated on a condition already in the returned report. A failed recache
    # is a stderr warning, NOT a non-zero exit: the checkout itself landed, and
    # failing here would make an agent retry a mutation that already succeeded.
    if any(r.ok for r in report.results) and not cache.recache_project(project):
        print(
            f"Warning: checkout completed, but re-caching '{project}' failed; "
            "run 'cwcli inspect --update' to refresh.",
            file=sys.stderr,
            flush=True,
        )

    emit_result(report, warnings=result.warnings)
    # The exit code reads report.ok, NOT result.status: a failed step is a
    # WARNING-shaped envelope, and WARNING maps to exit 0 everywhere else, so a
    # status-driven code would report success for a checkout git refused.
    raise typer.Exit(0 if report.ok else 1)


# ------------------------------------------------------------------- migrate / tests


def _bench_op_narrate(event) -> None:
    """The bench command's own bytes, to STDERR.

    Forwards :class:`BenchOpOutput` verbatim and UNPARSED, on the same reasoning
    ``_checkout_narrate`` forwards git's bytes and ``_init_narrate`` deliberately
    does not: neither a migrate nor a test run is a supervised process, so neither
    writes to a log ``cwcli logs`` can serve afterwards. The name of the patch that
    blew up, and the assertion that failed, exist ONLY here. Dropping them leaves a
    failure reported as a bare ``ok: false`` and an agent with no next move.

    Deliberately NOT summarized into pass/fail counts: cwcli does not own the test
    runner's output format, and the guess would be wrong the first time a suite used
    a different runner.

    Everything goes to stderr, so the one-TOON-document contract on stdout holds.
    """
    if isinstance(event, core_bench_ops.BenchOpCommand):
        print(f"$ {event.command}", file=sys.stderr, flush=True)
    elif isinstance(event, core_bench_ops.BenchOpOutput):
        print(event.text, end="", file=sys.stderr, flush=True)


@app.command("migrate")
def axi_migrate(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    site: str = typer.Option(
        None, "--site", "-s", help="Site to migrate (default: the bench's default site)."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
) -> None:
    """Run 'bench migrate' against ONE site under maintenance mode; emit the report as TOON.

    The gap `axi apps update` leaves. That verb migrates too, but only as the tail
    of a `git pull` across every named app - so an agent that has just pinned a
    feature branch with `axi apps checkout` cannot migrate without a pull that moves
    the ref it pinned. This is the migrate on its own.

    BLAST RADIUS, stated plainly: this applies every pending schema patch from every
    installed app to the site's LIVE database. It ALTERs tables and runs patch code
    the apps ship, it is not transactional across patches, and cwcli has no rollback
    - a patch that fails partway leaves the database partially migrated. Recovery is
    from a backup. Compose `cwcli axi backup <project> --site <site>` first if the
    data matters; this verb deliberately does not take one for you, because
    `axi apps update` does not either and a silent backup is not a guard.

    Safety posture, each guard against a named threat:

    - EXACTLY ONE site per invocation, never a fan-out (an agent that ran this
      discovering it migrated four sites, which is what `apps update` does because
      there the APP is the subject). The RESOLVED site is in the report, so what was
      acted on can always be read back.
    - Maintenance mode is enabled first and a failed enable REFUSES the migrate
      (migrating a site still serving live traffic). It is disabled in a `finally`,
      and a site left in maintenance is reported as `maintenance_left_on` and fails
      the verb, because that site is DOWN and the agent must know. There is NO
      --skip-maintenance: a flag that removes the gate has no named beneficiary.
    - NO --yes and no auto-start (an agent starting containers a user deliberately
      stopped): a stopped project is a usage error naming `cwcli start`.

    bench's own output goes to stderr in full and unparsed - the failing patch names
    itself there and nowhere else.
    """
    try:
        result = core_bench_ops.migrate_site(
            project, site=site, bench=bench, auto_start=False, on_event=_bench_op_narrate
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a BenchOpReport
    report = result.data
    emit_result(report, warnings=result.warnings)
    if not report.ok:
        # Contextual disclosure (AXI section 9) on failure only: a successful migrate
        # fully answers the query, and a help line there would be noise.
        typer.echo(toon.kv("help", f"read the failure with 'cwcli axi logs {project}'"))
    # The exit code reads report.ok, NOT result.status: a failed step is a
    # WARNING-shaped envelope, and WARNING maps to exit 0 everywhere else.
    raise typer.Exit(0 if report.ok else 1)


@app.command("run-tests")
def axi_run_tests(
    project: str = typer.Argument(..., help="The Docker Compose project name."),
    site: str = typer.Option(
        None, "--site", "-s", help="Site to run the tests against. REQUIRED: no default."
    ),
    app_name: str = typer.Option(
        None, "--app", help="App whose test suite to run. REQUIRED: no default."
    ),
    bench: str = typer.Option(None, "--bench", help="Which bench: numeric index or label."),
) -> None:
    """Run 'bench run-tests' for ONE app against ONE named site; emit the report as TOON.

    BLAST RADIUS, stated plainly: this imports and executes the app's OWN test
    modules inside the container, against the named site's live database. cwcli
    cannot bound what that code does, because it IS the repository's code - a Frappe
    test suite creates, modifies and deletes records. Honestly: arbitrary Python from
    the repository under test, executed against a live site. Point it at a dedicated
    test site; cwcli cannot tell one from a site holding real data, so that is a
    practice it states and cannot enforce.

    This is NOT the `axi run`/`axi exec` passthrough that stays deferred, and the
    distinction is who AUTHORS the command: there, the agent supplies an unbounded
    command string; here it SELECTS an app whose tests already exist in the bench,
    put there by a human's `apps install` or `apps checkout`. No parameter on this
    verb can express a second command.

    Safety posture, each guard against a named threat:

    - --site is REQUIRED with NO default-site fallback (an agent running a
      destructive suite against whatever site happened to be the bench default,
      having never named it). This deliberately diverges from `axi backup`,
      `axi unlock` and `axi migrate`, which all default: for those, cwcli can state
      exactly what the operation does to the site, and here it cannot. When the
      effect is unbounded, defaulting the target is the wrong default.
    - --app is REQUIRED (a bare `bench run-tests` running every installed app's
      suite, including frappe's own, against that site).
    - NO --yes and no auto-start: a stopped project is a usage error naming
      `cwcli start`.

    The runner's output goes to stderr in FULL and UNPARSED - cwcli does not own
    that format, so it reports only the honest pass/fail and forwards the rest.
    """
    missing = [flag for flag, value in (("--site", site), ("--app", app_name)) if not value]
    if missing:
        emit_axi_error(
            CwcliError(
                ErrorKind.USAGE,
                "run_tests.target_required",
                f"Missing required flag(s): {', '.join(missing)}.",
                hint=(
                    "run-tests executes the app's own test code against a live site, so "
                    "both the site and the app must be named explicitly; there is no default"
                ),
            )
        )
        raise typer.Exit(exit_for(ErrorKind.USAGE))

    try:
        result = core_bench_ops.run_tests(
            project,
            site=site,
            app=app_name,
            bench=bench,
            auto_start=False,
            on_event=_bench_op_narrate,
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if result.status is CoreStatus.NEEDS_CHOICE:
        assert result.choice is not None  # NEEDS_CHOICE always carries a Choice
        emit_axi_choice_as_usage_error(result.choice)
        raise typer.Exit(2)

    assert result.data is not None  # OK/WARNING always carries a BenchOpReport
    report = result.data
    emit_result(report, warnings=result.warnings)
    if not report.ok:
        typer.echo(toon.kv("help", "the failing test's own output is on stderr"))
    raise typer.Exit(0 if report.ok else 1)


# ---------------------------------------------------------------------------- config


@app.command("config")
def axi_config() -> None:
    """Report the effective cwcli configuration; emit it as one TOON document. READ-ONLY.

    The aggregate the AXI standard asks a read verb to be: search paths,
    auto-inspect state (config, live daemon, boot hook - three stores,
    reported separately), tips, and the config-file/cache-DB locations in ONE
    call, because the follow-up call is the expensive token cost.

    Deliberately carries NO mutating flags, and no config-mutating axi verb
    exists (paths add/remove, cache clear, auto-inspect enable/disable): an
    agent rewriting the user's search paths or wiping the cache is a product
    decision on its own evidence, not a consequence of the config migration -
    the `axi apps install`/`uninstall` deferral discipline. A test asserts the
    registry absence so "deliberately not built" cannot be misread as
    "forgotten".
    """
    try:
        result = core_config.show_config()
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    assert result.data is not None  # show_config always returns a ConfigReport
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0)


# ------------------------------------------------------------------------------- init


_INIT_PHASE_LABELS = {
    "bench_init": "Initializing bench",
    "new_site": "Creating site",
}


def _init_narrate(event) -> None:
    """Coarse phase-level progress to STDERR (design question 1).

    Writes only a label derived from ``InitStepStart`` and ``InitNotice.text``
    - never ``InitOutput`` (raw bench exec bytes, noise an agent does not
    parse; that is what ``cwcli logs`` is for) and never ``InitTrace``
    (verbose diagnostics). The core guarantees no event field carries a secret
    value, and neither of the fields used here is ever a secret.

    Some of the longest-running phases (``bench_init``, ``new_site``) carry
    only ``item`` and no ``message``; those must still narrate so an agent
    watching stderr for liveness doesn't see a silent gap during the bulk of
    init's wall-clock time.
    """
    if isinstance(event, core_init.InitStepStart):
        if event.message:
            print(event.message, file=sys.stderr, flush=True)
        elif event.item:
            label = _INIT_PHASE_LABELS.get(event.phase, event.phase)
            print(f"{label}: {event.item}", file=sys.stderr, flush=True)
    elif isinstance(event, core_init.InitNotice):
        print(event.text, file=sys.stderr, flush=True)


@app.command("init")
def axi_init(
    project: str = typer.Argument(..., help="The Docker Compose project name to create."),
    port: int = typer.Option(
        8000, "--port", help="Starting port; creates {port}-{port+5} (web) and +1000 (socketio)."
    ),
    bench: str = typer.Option(
        "frappe-bench",
        "--bench",
        help="NAME of the new bench to create (NOT the --bench <index|label> selector other "
        "verbs use; init creates a bench, it does not select one).",
    ),
    site: str = typer.Option(
        "development.localhost", "--site", help="Primary site to create (must end with .localhost)."
    ),
    bench_parent: str = typer.Option(
        "/workspace",
        "--bench-parent",
        help="Directory inside the container to create the bench in.",
    ),
    frappe_branch: str = typer.Option(
        None,
        "--frappe-branch",
        help="Frappe branch/tag for bench init (e.g. version-16 or v16.26.3). "
        "Mutually exclusive with --version.",
    ),
    version: str = typer.Option(
        None,
        "--version",
        help="Frappe version resolved by shape: a bare major (16 -> version-16) or a full "
        "semantic version (16.26.3 -> v16.26.3). Mutually exclusive with --frappe-branch.",
    ),
    db_root_password: str = typer.Option(
        None,
        "--db-root-password",
        help="MariaDB root password. Falls back to the CWCLI_DB_ROOT_PASSWORD env var, then '123'.",
    ),
    admin_password: str = typer.Option(
        None,
        "--admin-password",
        help="Site administrator password (used verbatim). RECOMMENDED: set the "
        "CWCLI_ADMIN_PASSWORD env var instead (the flag lands on the argv, visible in "
        "process listings and shell history). Required: this surface never generates one.",
    ),
    reuse_bench: bool = typer.Option(
        None,
        "--reuse-bench/--no-reuse-bench",
        help="When the bench directory already exists: --reuse-bench reuses it, --no-reuse-bench "
        "requires a fresh --bench name. Omit both and an existing bench is a usage error.",
    ),
    install_erpnext: bool = typer.Option(
        False, "--install-erpnext", help="Install ERPNext onto the created site."
    ),
    erpnext_branch: str = typer.Option(
        "version-16", "--erpnext-branch", help="ERPNext branch (used with --install-erpnext)."
    ),
    start_services: bool = typer.Option(
        True,
        "--start/--no-start",
        help="After creating the bench+site, start its dev services (supervisord over "
        "'bench start') so init leaves a running bench. --no-start creates without "
        "starting, for automation/CI. Distinct from container startup, which stage 1 "
        "always brings up regardless of this flag.",
    ),
) -> None:
    """Provision a new instance, bench, and site; emit the report as TOON (never prompts).

    BLOCKS for the full 10-20 minute provisioning run and emits ONE terminal
    `InitReport` TOON document on stdout when it finishes, exactly as `axi apps
    update` blocks on a long update - streaming N documents would break the
    one-TOON-document contract. Coarse phase progress goes to STDERR; for live or
    deeper progress, run `cwcli logs <project>` / `cwcli status <project>` from a
    second shell.

    After the bench+site is created, dev services start by default (reusing the
    same `core.start` behind `cwcli start`/`axi start`), so this leaves a running
    bench; `--no-start` skips it. A start failure degrades to a stderr warning and
    does NOT fail the verb or change its exit code - the bench was already created.

    The site admin password comes from the CWCLI_ADMIN_PASSWORD env var
    (recommended) or --admin-password (the flag lands on the argv - see its help);
    the flag wins if both are set. With NEITHER set the verb refuses (exit 2)
    naming both - it never generates a password and never prompts. The MariaDB
    root password takes the same shape via CWCLI_DB_ROOT_PASSWORD / --db-root-password
    (default '123').

    The interactive decisions the human `cwcli init` prompts for become
    non-prompting errors: an existing bench with neither --reuse-bench nor
    --no-reuse-bench is a usage error (exit 2) naming both; a port conflict names
    --port (exit 1); containers that do not come up point at `cwcli status` /
    `cwcli logs` (exit 1). There is no --auto-start (compose `cwcli axi start`
    then re-run) and no --verbose (stdout is always TOON).
    """
    resolved_admin = admin_password or os.environ.get("CWCLI_ADMIN_PASSWORD")
    if not resolved_admin:
        emit_axi_error(
            CwcliError(
                ErrorKind.USAGE,
                "init.admin_password_required",
                "No administrator password supplied. Set the CWCLI_ADMIN_PASSWORD environment "
                "variable or pass --admin-password.",
                hint="the agent surface never generates or prompts for a password",
            )
        )
        raise typer.Exit(exit_for(ErrorKind.USAGE))
    db_root = db_root_password or os.environ.get("CWCLI_DB_ROOT_PASSWORD") or "123"

    # Resolve the Frappe ref (flag fusion is frontend UX, the human-init precedent).
    if frappe_branch is not None and version is not None:
        emit_axi_error(
            CwcliError(
                ErrorKind.USAGE,
                "init.flag_conflict",
                "--frappe-branch and --version are mutually exclusive; pass only one.",
            )
        )
        raise typer.Exit(exit_for(ErrorKind.USAGE))
    try:
        if version is not None:
            frappe_ref = core_init.resolve_frappe_ref(version)
        elif frappe_branch is not None:
            frappe_ref = frappe_branch
        else:
            frappe_ref = core_init.DEFAULT_FRAPPE_BRANCH
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    # Stage 1: the instance. A NEEDS_CHOICE here is the readiness-poll timeout;
    # the containers are init's OWN, so it is an operational error (exit 1)
    # pointing at status/logs, not the generic "start it first" usage error.
    try:
        instance_result = core_init.init_instance(
            project, port=port, bench_parent=bench_parent, on_event=_init_narrate
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None
    if instance_result.status is CoreStatus.NEEDS_CHOICE:
        emit_axi_error(
            CwcliError(
                ErrorKind.NOT_RUNNING,
                "init.not_ready",
                f"Containers for '{project}' did not become ready.",
                hint=f"check 'cwcli status {project}' and 'cwcli logs {project}'",
            )
        )
        raise typer.Exit(1)

    # Stage 2: the bench + site. No re-invoke loop - an unresolved choice is an
    # error and the process exits; an agent re-runs with the missing flag.
    try:
        bench_result = core_init.init_bench(
            project,
            bench_name=bench,
            site_name=site,
            bench_parent=bench_parent,
            frappe_ref=frappe_ref,
            db_root_password=db_root,
            admin_password=resolved_admin,
            reuse_bench=reuse_bench,
            install_erpnext=install_erpnext,
            erpnext_branch=erpnext_branch,
            on_event=_init_narrate,
        )
    except CwcliError as error:
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    if bench_result.status is CoreStatus.NEEDS_CHOICE:
        choice = bench_result.choice
        assert choice is not None  # NEEDS_CHOICE always carries a Choice
        if choice.kind == "confirm_start":
            # A stage-2 race: the container stopped between the two stages.
            emit_axi_error(
                CwcliError(
                    ErrorKind.NOT_RUNNING,
                    "init.container_stopped",
                    f"The Frappe container for '{project}' is not running.",
                    hint=f"check 'cwcli status {project}' and 'cwcli logs {project}'",
                )
            )
            raise typer.Exit(1)
        # confirm_reuse_bench: a usage error naming --reuse-bench / --no-reuse-bench.
        emit_axi_choice_as_usage_error(choice)
        raise typer.Exit(2)

    assert bench_result.data is not None  # OK/WARNING always carries an InitReport
    report = bench_result.data

    if not cache.recache_project(project):
        print(
            "Warning: bench created, but caching its bench path failed; "
            "run 'cwcli inspect --update' to refresh.",
            file=sys.stderr,
            flush=True,
        )

    if start_services:
        print("Starting dev services...", file=sys.stderr, flush=True)
        try:
            start_result = core_start.start(project, bench_path=report.bench_path)
        except Exception as e:
            print(
                f"Warning: bench created, but its dev services could not be started: "
                f"{getattr(e, 'message', str(e))}",
                file=sys.stderr,
                flush=True,
            )
        else:
            for warning in start_result.warnings:
                if warning.code == "start.web_not_ready":
                    print(f"Warning: {warning.text}", file=sys.stderr, flush=True)

    emit_result(bench_result.data, warnings=bench_result.warnings)
    raise typer.Exit(0 if bench_result.status in (CoreStatus.OK, CoreStatus.WARNING) else 1)


# ------------------------------------------------------------------------ self-update


@app.command("self-update")
def axi_self_update(
    check: bool = typer.Option(
        ..., "--check", help="Required. Report current vs latest; never upgrades."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Force a fresh PyPI lookup, ignoring the shared version cache."
    ),
) -> None:
    """Report whether a newer cwcli is available; emit the check as TOON. READ-ONLY.

    ``--check`` is REQUIRED: the mutating form is deliberately deferred, so this
    verb never upgrades anything. An agent upgrading the tool it is currently
    executing from, mid-session, is a question nobody has answered, and the
    original deferral's rationale was never recorded.

    Exits 0 on ANY successful read, including when an update IS available, and
    carries that fact in ``is_outdated``. This deliberately DIVERGES from the human
    `cwcli self-update --check`, which exits 1 so shell scripts can gate on it: on
    the agent surface a non-zero exit means an error, and a read verb that
    successfully answers "you are outdated" has not failed. The precedent is `axi
    status`, which exits 0 while reporting a fully offline project. A fail-open
    PyPI lookup is also a success (`latest: null` + a `pypi.unreachable` warning),
    because a read-only check must not punish a flaky network.
    """
    try:
        result = core_version.check(use_cache=not no_cache)
    except CwcliError as error:  # pragma: no cover - check is fail-open by contract
        emit_axi_error(error)
        raise typer.Exit(exit_for(error.kind)) from None

    assert result.data is not None  # check always returns data
    emit_result(result.data, warnings=result.warnings)
    raise typer.Exit(0)


# ----------------------------------------------------------------------------------- setup


@app.command("setup")
def axi_setup() -> None:
    """Install the SessionStart hook into every detected agent harness; emit TOON.

    The ambient-context half of the AXI surface: without it, an agent only finds
    `cwcli axi` if a human names it. The hook runs the home view once per session,
    so the verb list and the live instances are already in the agent's opening
    context.

    Explicit opt-in by design - nothing installs a hook off an ordinary command,
    so this is the ONLY thing here that writes to the user's agent config. It is
    idempotent (an already-correct hook reports `unchanged` and is not rewritten)
    and repairs a stale executable path in place, so re-running it after a
    reinstall or a move is the fix rather than a duplicate entry.
    """
    outcomes = agent_hooks.install()
    rows = [
        {"agent": o.agent, "status": o.status, "path": _collapse_home(o.path)} for o in outcomes
    ]
    lines = [toon.table("agents", rows, ["agent", "status", "path"])]

    notes = [f"{o.agent}: {o.detail}" for o in outcomes if o.detail]
    if notes:
        lines.append(toon.block("notes", notes))

    lines.append(
        toon.block(
            "help",
            [
                "Restart the agent session to pick up the hook",
                "Run `cwcli axi` to see the same context the hook injects",
            ],
        )
    )
    typer.echo("\n".join(lines))
    raise typer.Exit(0)
