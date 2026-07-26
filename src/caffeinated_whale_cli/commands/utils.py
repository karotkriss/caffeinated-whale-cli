"""
Shared utility functions for command implementations.

This module provides general utilities for ensuring containers are running
before executing commands, resolving a ``--bench`` selector to a bench path,
handling ``--yes`` confirmations consistently, and splitting the options that
trail a variadic project argument back out of the project-name list
(:func:`split_trailing_options`).
"""

import sys
from collections.abc import Mapping, Sequence
from typing import NoReturn

import click
import questionary
import typer

from ..core import resolvers
from ..core.envelope import Status
from ..core.errors import CwcliError, ErrorKind
from ..utils import bench_labels
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_frappe_container


def ensure_containers_running(
    project_name: str,
    require_running: bool = False,
    verbose: bool = False,
    auto_start: bool = False,
    prompt: bool = True,
) -> bool:
    """
    Check if containers for a project are running and optionally prompt to start them.

    Note: This function does NOT perform port conflict checks. Port conflicts are only
    checked by the `start` command. This is intentional - other commands (logs, run, etc.)
    need a quick way to ensure containers are running without the interactive port conflict
    resolution workflow.

    Args:
        project_name: The name of the docker-compose project.
        require_running: If True, containers must be running for the operation to proceed.
        verbose: Enable verbose output.
        auto_start: If True, automatically start containers without prompting.
        prompt: If True (default), interactively ask the user to start stopped
            containers. If False, never prompt: when the containers are not running
            (and ``auto_start`` is False) the function returns False instead of
            asking a question. This non-interactive mode is required for callers that
            run inside a Rich ``console.status`` spinner (e.g. the ``rm`` recache
            path), where an interactive prompt would be painted over and could never
            receive input, deadlocking the command.

    Returns:
        True if containers are running (or were started), False otherwise.

    Raises:
        typer.Exit: If containers are not running and the user is prompted but chooses
                   not to start them (decline or Ctrl-C), if the terminal is non-TTY
                   and ``auto_start`` is False (so no prompt is possible), or if
                   starting containers fails. All of these exit with code 1.
    """
    if not require_running:
        return True

    # Resolve the container (this CLI wrapper prints + Exit(1) on not-found) and
    # ask the pure core resolver for its run-state decision. All prompting,
    # printing, and the actual (UI-coupled) container start stay here in the CLI.
    frappe_container = get_frappe_container(project_name)
    state = resolvers.resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )

    if state.status is Status.OK:
        assert state.data is not None  # OK always carries a ContainerState
        if state.data.running:
            if verbose:
                stderr_console.print("[dim]VERBOSE: Frappe container is running[/dim]")
            return True
        # start_requested: auto-start was asked for and the container was down.
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Auto-starting containers for '{project_name}'[/dim]"
            )
        _start_containers_for_command(project_name, verbose)
        return True

    # NEEDS_CHOICE confirm_start: the container is down and no auto-start was asked.
    if not prompt:
        # Non-interactive caller (e.g. running under a spinner). Do NOT prompt;
        # report that the containers are not running and let the caller degrade.
        if verbose:
            stderr_console.print(
                f"[dim]VERBOSE: Containers for '{project_name}' are not running "
                "(non-interactive mode; not prompting to start)[/dim]"
            )
        return False

    # Prompt the user to start. Mirrors confirm_or_exit's three-branch contract:
    # a non-TTY without auto_start refuses (Exit 1) rather than hanging on
    # questionary (open-but-idle stdin) or crashing with an uncaught EOFError
    # (closed stdin - .ask() only catches KeyboardInterrupt); an interactive
    # decline or Ctrl-C is a refusal, so it exits non-zero too.
    if not sys.stdin.isatty():
        stderr_console.print(
            f"[bold red]Error:[/bold red] Frappe container for project "
            f"'{project_name}' is not running. Pass --yes to auto-start it, or "
            f"start it first with 'cwcli start {project_name}'."
        )
        raise typer.Exit(code=1)

    stderr_console.print(
        f"[yellow]Warning:[/yellow] Frappe container for project '{project_name}' is not running."
    )

    try:
        answer = questionary.confirm(
            f"Would you like to start the containers for '{project_name}'?",
            default=True,
            auto_enter=False,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1) from None

    if answer:
        _start_containers_for_command(project_name, verbose)
        return True

    stderr_console.print("[yellow]Operation cancelled.[/yellow]")
    stderr_console.print(f"[dim]Start containers with: cwcli start {project_name}[/dim]")
    raise typer.Exit(code=1)


def resolve_bench_path(
    project_name: str,
    bench_selector: str | None,
    path_override: str | None,
    *,
    verbose: bool = False,
    on_ambiguous: str = "error",
) -> str | None:
    """Resolve which bench a command should operate on, honoring ``--bench``/``--path``.

    This is the single, shared replacement for the ad-hoc "use ``--path`` if given,
    else cached ``bench_instances[0]``, else a hardcoded default" logic that used to
    be copy-pasted across the bench-operating commands (and which silently picked
    an arbitrary bench in a multi-bench project).

    Precedence:
      1. ``path_override`` (``--path``) - the explicit low-level escape hatch. Using
         it together with ``--bench`` is an error (they specify the same thing two
         ways).
      2. ``bench_selector`` (``--bench <number|label>``) - resolved against the
         cached benches via :func:`bench_labels.resolve_bench`. No match -> a clear
         error listing every bench.
      3. Neither given -> the *default* bench:
         - single-bench project: that bench's path,
         - multi-bench project: with ``on_ambiguous="error"`` (the data-op default)
           this errors and lists the benches so the user picks one with ``--bench``;
           with ``on_ambiguous="first"`` (used by the internal ``_start_project``
           wrapper - ``restart``/auto-start/post-restore callers) it returns the
           first bench and prints a note; with ``on_ambiguous="prompt"`` (used by
           the interactive ``cwcli start``/``status`` commands) it prompts on a
           TTY and refuses naming ``--bench`` on a non-TTY,
         - no cached benches at all: returns ``None`` so the caller can fall back to
           its own behavior (run inspect / use a hardcoded default).

    Returns the resolved bench path, or ``None`` only in the no-cache case. Raises
    ``typer.Exit(1)`` on a conflict, an unresolved selector, or an ambiguous
    multi-bench default under ``on_ambiguous="error"``.

    This is the CLI wrapper: the pure resolution (precedence, cache read,
    selector match, ambiguity) lives in ``core.resolvers.resolve_bench``; this
    wrapper only renders the bench list and maps the typed outcome to today's
    prints and exit codes.
    """
    try:
        result = resolvers.resolve_bench(project_name, bench_selector, path_override)
    except CwcliError as e:
        if e.kind is ErrorKind.USAGE:
            stderr_console.print(
                "[bold red]Error:[/bold red] Use either --bench or --path, not both."
            )
            raise typer.Exit(code=1) from None
        # NOT_FOUND: an explicit --bench selector matched no cached bench.
        benches = resolvers.cached_benches(project_name)
        stderr_console.print(
            f"[bold red]Error:[/bold red] No bench '{bench_selector}' in project "
            f"'{project_name}'."
        )
        if benches:
            stderr_console.print("Available benches (address with --bench <index|label>):")
            stderr_console.print(bench_labels.format_bench_list(benches))
        else:
            stderr_console.print(
                f"[dim]No benches are cached. Run 'cwcli inspect {project_name}' first.[/dim]"
            )
        raise typer.Exit(code=1) from None

    if result is None:
        # No cache to resolve against; let the caller fall back (inspect/default).
        return None

    if result.status is Status.OK:
        assert result.data is not None  # OK always carries the resolved path
        path: str = result.data
        if verbose:
            for warning in result.warnings:
                if warning.code == "bench.sole":
                    stderr_console.print(f"[dim]{warning.text}[/dim]")
        return path

    # NEEDS_CHOICE select_bench: multiple benches, no selector.
    benches = resolvers.cached_benches(project_name)
    if on_ambiguous == "first":
        first_path: str = benches[0]["path"]
        stderr_console.print(
            f"[yellow]Note:[/yellow] project '{project_name}' has multiple benches; "
            f"using [green]{first_path}[/green]. Select another with --bench <index|label>:"
        )
        stderr_console.print(bench_labels.format_bench_list(benches))
        return first_path

    if on_ambiguous == "prompt":
        # start/status: prompt which bench on a TTY, refuse on a non-TTY. Mirrors
        # the "support BOTH interactive and non-interactive" contract - a non-TTY
        # without --bench must refuse (Exit 1), never silently pick one.
        return _prompt_select_bench(project_name, benches)

    stderr_console.print(
        f"[bold red]Error:[/bold red] project '{project_name}' has multiple benches; "
        "specify one with --bench <index|label>:"
    )
    stderr_console.print(bench_labels.format_bench_list(benches))
    raise typer.Exit(code=1)


def _prompt_select_bench(project_name: str, benches: list[dict]) -> str:
    """Interactively pick a bench (TTY), or refuse naming ``--bench`` (non-TTY)."""
    if not sys.stdin.isatty():
        stderr_console.print(
            f"[bold red]Error:[/bold red] project '{project_name}' has multiple benches; "
            "specify one with --bench <index|label>:"
        )
        stderr_console.print(bench_labels.format_bench_list(benches))
        raise typer.Exit(code=1)

    choice_map: dict[str, str] = {}
    choices: list[str] = []
    for position, b in enumerate(benches):
        index = b.get("index", position)
        label = b.get("label")
        prefix = f"'{label}' " if label else ""
        text = f"[{index}] {prefix}{b.get('path', '?')}"
        choices.append(text)
        choice_map[text] = str(b["path"])

    try:
        answer = questionary.select(
            f"Project '{project_name}' has multiple benches; select one:",
            choices=choices,
        ).ask()
    except (KeyboardInterrupt, EOFError):
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1) from None

    if answer is None:
        stderr_console.print("[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1)
    return choice_map[answer]


def confirm_or_exit(
    prompt: str,
    *,
    assume_yes: bool,
    refuse_message: str,
    default: bool = False,
    cancel_message: str = "Operation cancelled.",
) -> None:
    """Gate a destructive action behind a confirmation, honoring ``--yes``.

    Mirrors the established ``restore``/``rm`` contract so every ``--yes`` behaves
    the same way:
      - ``assume_yes`` -> proceed without prompting;
      - no TTY and not ``assume_yes`` -> refuse and ``exit(1)`` (never silently
        proceed on a destructive op driven non-interactively);
      - interactive TTY -> ask; a declined confirm or Ctrl-C/EOF exits non-zero.

    Returns normally only when the action is approved.
    """
    if assume_yes:
        console.print("[dim]Proceeding without confirmation (--yes).[/dim]")
        return

    if not sys.stdin.isatty():
        stderr_console.print(f"[bold red]Error:[/bold red] {refuse_message}")
        raise typer.Exit(code=1)

    try:
        answer = questionary.confirm(prompt, default=default).ask()
    except (KeyboardInterrupt, EOFError):
        stderr_console.print(f"\n[yellow]{cancel_message}[/yellow]")
        raise typer.Exit(code=1) from None

    if not answer:
        stderr_console.print(f"[yellow]{cancel_message}[/yellow]")
        raise typer.Exit(code=1)


def _start_containers_for_command(project_name: str, verbose: bool = False):
    """
    Start containers for a project. Used by ensure_containers_running.

    This function performs the same port conflict detection as the `start` command
    to prevent Docker errors when ports are already in use. It will:
    - Check if required ports are available
    - Identify and offer to stop conflicting Frappe projects
    - Report non-Frappe processes using the ports
    - Provide helpful error messages

    Args:
        project_name: The name of the docker-compose project.
        verbose: Enable verbose output.

    Raises:
        typer.Exit: If starting containers fails or port conflicts cannot be resolved.
    """
    from .start import _check_port_conflicts, _start_project

    # Check for port conflicts BEFORE attempting to start
    try:
        _check_port_conflicts(project_name, verbose)
    except typer.Exit:
        # Port conflict couldn't be resolved
        stderr_console.print(
            f"[yellow]Cannot start '{project_name}' due to port conflicts.[/yellow]"
        )
        stderr_console.print(
            f"[dim]Resolve conflicts manually or use 'cwcli start {project_name}' for more options.[/dim]"
        )
        raise

    # Port conflicts resolved or no conflicts - safe to start
    try:
        with stderr_console.status(
            f"[bold green]Starting containers for '{project_name}'...[/bold green]",
            spinner="dots",
        ) as status:
            log_file = _start_project(project_name, verbose=verbose, status=status)

        console.print(f"[bold green]✓[/bold green] Containers started for '{project_name}'")
        if log_file:
            console.print(f"[dim]View logs with: cwcli logs {project_name}[/dim]")
    except Exception as e:
        stderr_console.print(f"[bold red]Error:[/bold red] Failed to start containers: {e}")
        raise typer.Exit(code=1) from None


# ------------------------------------------------- trailing-option recovery
#
# ``start``, ``stop``, ``restart`` and ``rm`` all take a VARIADIC project
# argument, which greedily eats every token that follows it - options included.
# Each grew its own hand-rolled recovery loop, and every one of them ended in the
# same ``else: names.append(token)``: an option the command does NOT define was
# silently swallowed as another project name. ``cwcli stop myproj --benhc 1``
# therefore stopped the WHOLE instance (plus two "not found" lines) at exit 0.
# One splitter, used by all four, so the swallow cannot come back one command at
# a time.

_HELP_OPTIONS = frozenset({"-h", "--help"})


def _inline_value(token: str, values: Mapping[str, str]) -> tuple[str, str] | None:
    """Split a ``--option=value`` token into its option and value, if it is one."""
    for option in values:
        if option.startswith("--") and token.startswith(f"{option}="):
            return option, token.split("=", 1)[1]
    return None


def _parse_short_cluster(
    token: str, flags: Mapping[str, tuple[str, bool]], values: Mapping[str, str]
) -> tuple[list[tuple[str, bool]], tuple[str, str] | None, bool] | None:
    """Resolve an attached or clustered short-option token: ``-pweb``, ``-vy``, ``-vpweb``.

    Returns the ``(destination, value)`` pairs the cluster sets, followed by the
    value-taking option it ends on as ``(option, attached_value)`` - an EMPTY
    attached value meaning the value is the next token - and whether the cluster
    requests eager help. Click's own grammar, reproduced: a value-taking short
    swallows the rest of the token as its value and stops the cluster, so
    ``-pv web`` is ``--process v`` with ``web`` still a project name.

    Returns ``None`` for anything that is not a short cluster THIS command fully
    defines - a long option, a bare ``-``, ``--``, or any cluster holding a
    character the command does not know.

    That last part is ALL-OR-NOTHING on purpose, and it is the whole safety
    property: a cluster is applied only when EVERY character in it resolves, so
    a typo, a truncation, or a flag borrowed from a sibling subcommand can never
    synthesise the ``-y`` consent it happens to contain. ``cwcli rm proj -yq``
    must not become ``cwcli rm proj -y``. Returning ``None`` rather than raising
    keeps the decision POSITIONAL, like the rest of this splitter: in option
    position the caller refuses it, in value position it is simply the value, so
    a bench label such as ``-staging`` still resolves. The eager-help exception
    is a malformed cluster that reaches ``-h`` before an unknown short, such as
    ``-vhq``: it is refused in either position so the unknown short cannot be
    hidden by help or preserved as a value.
    """
    if len(token) < 2 or not token.startswith("-") or token[1] == "-":
        return None
    recovered: list[tuple[str, bool]] = []
    eager_help = False
    for index, char in enumerate(token[1:], start=1):
        option = f"-{char}"
        if option in _HELP_OPTIONS:
            eager_help = True
        elif option in flags:
            recovered.append(flags[option])
        elif option in values:
            return recovered, (option, token[index + 1 :]), eager_help
        else:
            return None
    return recovered, None, eager_help


def _has_help_before_unknown_short(
    token: str, flags: Mapping[str, tuple[str, bool]], values: Mapping[str, str]
) -> bool:
    if len(token) < 2 or not token.startswith("-") or token[1] == "-":
        return False
    saw_help = False
    for char in token[1:]:
        option = f"-{char}"
        if option in _HELP_OPTIONS:
            saw_help = True
        elif option in flags:
            continue
        elif option in values:
            return False
        else:
            return saw_help
    return False


def _is_recognised_option(
    token: str, flags: Mapping[str, tuple[str, bool]], values: Mapping[str, str]
) -> bool:
    """True when the token is an option THIS command defines."""
    return (
        token in _HELP_OPTIONS
        or token in flags
        or token in values
        or _inline_value(token, values) is not None
        or _parse_short_cluster(token, flags, values) is not None
    )


def _show_command_help() -> NoReturn:
    context = click.get_current_context()
    click.echo(context.get_help())
    raise typer.Exit()


def _value_from_next_token(
    items: Sequence[str],
    index: int,
    option: str,
    flags: Mapping[str, tuple[str, bool]],
    values: Mapping[str, str],
) -> str:
    """The value a value-taking option takes from the token after it.

    Shared by the standalone (``-p web``) and clustered (``-vp web``) forms so the
    two cannot drift on what counts as a missing value.
    """
    following = items[index + 1] if index + 1 < len(items) else None
    if following in _HELP_OPTIONS:
        _show_command_help()
    if following is not None:
        cluster = _parse_short_cluster(following, flags, values)
        if cluster is not None and cluster[2]:
            _show_command_help()
        if _has_help_before_unknown_short(following, flags, values):
            _usage_error(f"No such option: {following}")
    if following is None or _is_recognised_option(following, flags, values):
        _usage_error(f"Option '{option}' requires a value.")
    return following


def _usage_error(message: str, hint: str | None = None) -> NoReturn:
    stderr_console.print(f"[bold red]Error:[/bold red] {message}")
    if hint:
        stderr_console.print(f"[dim]{hint}[/dim]")
    raise typer.Exit(code=2)


def split_trailing_options(
    tokens: Sequence[str] | None,
    *,
    command: str,
    flags: Mapping[str, tuple[str, bool]],
    values: Mapping[str, str],
) -> tuple[list[str], dict[str, bool], dict[str, str]]:
    """Split a variadic project argument into project names and trailing options.

    ``flags`` maps a boolean option token to the ``(destination, value)`` it sets
    (so ``--no-volumes`` and ``--volumes`` can share one destination); ``values``
    maps a value-taking option token to its destination.

    Returns the project names, then the recovered boolean flags and the recovered
    option values, each keyed by destination and holding ONLY what actually
    trailed the names - so a caller applies it as ``recovered.get(dest, parsed)``
    and an option written BEFORE the names keeps the value Typer parsed for it.

    An unrecognised option token is a USAGE ERROR (exit 2), never a project name.
    That is the whole point of this function: swallowed as a name, ``--benhc`` on
    a destructive verb becomes an action on the wrong target at exit 0.

    A leading dash alone does NOT make a token an option: cwcli's bench labels may
    start with one, so ``--bench -1`` and ``--bench=-1`` must keep working. The
    distinction is POSITIONAL - the token after a value-taking option is that
    option's value unless it is an option this command defines - which is why the
    unrecognised-option check below can only ever see a token in option position.

    Attached and clustered short options (``-pweb``, ``-vy``, ``-vpweb``) are
    resolved by :func:`_parse_short_cluster`, which reproduces Click's grammar and
    refuses a cluster WHOLE when any character in it is unknown - see that
    function for why the all-or-nothing rule is load-bearing here.
    """
    names: list[str] = []
    recovered_flags: dict[str, bool] = {}
    recovered_values: dict[str, str] = {}
    items = list(tokens or [])
    i = 0
    while i < len(items):
        token = items[i]
        if token in _HELP_OPTIONS:
            _show_command_help()
        elif token in flags:
            destination, value = flags[token]
            recovered_flags[destination] = value
        elif token in values:
            recovered_values[values[token]] = _value_from_next_token(items, i, token, flags, values)
            i += 1
        elif (inline := _inline_value(token, values)) is not None:
            option, inline_value = inline
            if not inline_value:
                _usage_error(f"Option '{option}' requires a value.")
            recovered_values[values[option]] = inline_value
        elif (cluster := _parse_short_cluster(token, flags, values)) is not None:
            cluster_flags, value_option, eager_help = cluster
            if eager_help:
                _show_command_help()
            for destination, value in cluster_flags:
                recovered_flags[destination] = value
            if value_option is not None:
                option, attached = value_option
                if attached:
                    recovered_values[values[option]] = attached
                else:
                    recovered_values[values[option]] = _value_from_next_token(
                        items, i, option, flags, values
                    )
                    i += 1
        elif len(token) > 1 and token.startswith("-"):
            _usage_error(
                f"No such option: {token}",
                f"Run 'cwcli {command} --help' to see the available options. "
                "Nothing was changed.",
            )
        else:
            names.append(token)
        i += 1
    return names, recovered_flags, recovered_values
