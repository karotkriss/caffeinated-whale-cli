"""``cwcli open`` - the thin frontend over ``core.open_plan``.

The four editor boolean flags fuse to one ``editor`` value HERE and their
mutual-exclusion error stays with them (flag UX is one frontend's choice, the
``apps`` fused-``--yes`` precedent); the interactive prologue, the prompts, and
the rendering live here; and the handover is this module's final lines -
``docker`` execs into the container (``exec_into_container`` -> ``os.execvp``,
correct BECAUSE it hands over), the editors launch via
``vscode_utils.open_in_vscode`` and return. Everything else - container,
run-state, bench, the no-cache fallback populate, ``--app``, editor detection -
is ``core.open_plan``'s. See ``core/open.py`` for the boundary and for why
there is deliberately no ``axi open`` verb.
"""

import sys

import typer
from rich.console import Console

from ..core import open as core_open
from ..core.envelope import Choice, Status
from ..core.errors import CwcliError
from ..utils import vscode_utils
from ..utils.completion_utils import complete_app_names, complete_project_names
from ..utils.docker_utils import exec_into_container, handle_docker_errors
from .utils import ensure_containers_running

stderr_console = Console(stderr=True)


def _resolve_editor_choice(choice: Choice) -> str:
    """Resolve ``select_editor`` the way this frontend always has: a questionary
    select on a TTY (cancel -> "Operation cancelled.", exit 1), and on a non-TTY
    the refusal naming the flags (exit 1)."""
    if not sys.stdin.isatty():
        stderr_console.print(
            "[bold red]Error:[/bold red] Multiple editors are available and no editor "
            "flag was given. Re-run with one of --code, --code-insiders, --cursor, or "
            "--docker to select non-interactively."
        )
        raise typer.Exit(code=1)

    import questionary
    from questionary import Style

    custom_style = Style(
        [
            ("qmark", "fg:#00ff00 bold"),  # Bright green question mark
            ("question", "fg:#00ffff bold"),  # Bright cyan question text
            ("answer", "fg:#00ff00 bold"),  # Bright green answer
            ("pointer", "fg:#ffff00 bold"),  # Bright yellow pointer
            ("highlighted", "fg:#ffff00 bold"),  # Bright yellow highlighted option
            ("selected", "fg:#00ff00"),  # Green for selected
            ("separator", "fg:#666666"),  # Gray separator
            ("instruction", "fg:#888888"),  # Gray instructions
            ("text", "fg:#ffffff"),  # White text
        ]
    )

    options = choice.options or []
    value_by_label: dict[str, str] = {option["label"]: option["value"] for option in options}

    answer = questionary.select(
        choice.prompt,
        choices=list(value_by_label),
        style=custom_style,
        pointer=">",
    ).ask()

    if answer is None:
        stderr_console.print("[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=1)

    return value_by_label.get(answer, "docker")


@handle_docker_errors
def open_bench(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name to open.", autocompletion=complete_project_names
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to open: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str | None = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    app: str = typer.Option(
        None,
        "--app",
        "-a",
        help="App name to open (opens the app's directory within the bench)",
        autocompletion=complete_app_names,
    ),
    code: bool = typer.Option(
        False,
        "--code",
        help="Open with VS Code (skips interactive prompt)",
    ),
    code_insiders: bool = typer.Option(
        False,
        "--code-insiders",
        help="Open with VS Code Insiders (skips interactive prompt)",
    ),
    cursor: bool = typer.Option(
        False,
        "--cursor",
        help="Open with Cursor (skips interactive prompt)",
    ),
    docker: bool = typer.Option(
        False,
        "--docker",
        help="Open with Docker exec (skips interactive prompt)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose diagnostic output.",
    ),
):
    """
    Open a project's frappe container in VS Code/Cursor (with Dev Containers) or exec into it.
    """
    # Validate that only one editor flag is specified, then fuse the four
    # booleans into the core's single editor param.
    editor_flags = [code, code_insiders, cursor, docker]
    if sum(editor_flags) > 1:
        stderr_console.print(
            "[bold red]Error:[/bold red] Only one of --code, --code-insiders, --cursor, or --docker can be specified."
        )
        raise typer.Exit(code=1)

    editor: str | None = None
    if code:
        editor = "code"
    elif code_insiders:
        editor = "code-insiders"
    elif cursor:
        editor = "cursor"
    elif docker:
        editor = "docker"

    # Interactive prologue: prompts happen HERE, before the core call. The core
    # then re-checks and only returns confirm_start on the (rare) race.
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    def on_event(event: core_open.OpenEvent) -> None:
        if isinstance(event, core_open.OpenNotice):
            stderr_console.print(f"[yellow]{event.text}[/yellow]")
        elif verbose:
            stderr_console.print(f"[dim]VERBOSE: {event.text}[/dim]")

    started = False
    while True:
        try:
            with stderr_console.status(
                f"[bold green]Preparing to open '{project_name}'...[/bold green]",
                spinner="dots",
            ):
                result = core_open.open_plan(
                    project_name,
                    bench=bench,
                    bench_path=bench_path,
                    app=app,
                    editor=editor,
                    auto_start=yes,
                    on_event=on_event,
                )
        except CwcliError as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            if e.hint:
                stderr_console.print(f"[dim]{e.hint}[/dim]")
            raise typer.Exit(code=1) from e

        if result.status is Status.NEEDS_CHOICE and result.choice is not None:
            choice = result.choice
            if choice.kind == "confirm_start":
                # Mirrors run.py: re-invoke at most ONCE after an attempted
                # start. A second confirm_start after ensure_containers_running
                # already claimed success means the start didn't take - fail
                # closed, don't spin.
                if started:
                    stderr_console.print(
                        "[bold red]Error:[/bold red] Frappe container for project "
                        f"'{project_name}' failed to start."
                    )
                    raise typer.Exit(code=1)
                ensure_containers_running(
                    project_name, require_running=True, verbose=verbose, auto_start=yes
                )
                started = True
                continue
            if choice.kind == "select_editor":
                # Prompt (or refuse on a non-TTY), then re-invoke ONCE with the
                # editor filled; the second pass resolves from the now-populated
                # cache, preserving today's prompt-last error ordering.
                editor = _resolve_editor_choice(choice)
                continue
            # select_bench: multiple benches, no selector - rendered as run.py
            # renders it (the one named wording drift off resolve_bench_path).
            stderr_console.print(f"[bold red]Error:[/bold red] {choice.prompt}")
            for option in choice.options or []:
                stderr_console.print(f"  [dim]{option['value']}[/dim]  {option['label']}")
            stderr_console.print("[dim]Pass --bench <index|label> to choose one.[/dim]")
            raise typer.Exit(code=1)
        break

    # bench.default_used renders unconditionally (today's yellow "Using default"
    # lines); the bench.sole note stays verbose-only, exactly as the old
    # resolve_bench_path wrapper rendered it.
    for warning in result.warnings:
        if warning.code == "bench.default_used":
            stderr_console.print(f"[yellow]Warning: {warning.text}[/yellow]")
        elif verbose:
            stderr_console.print(f"[dim]{warning.text}[/dim]")

    target = result.data
    assert target is not None  # OK always carries a LaunchTarget

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Selected editor: {target.editor}[/dim]")

    # The bench's real address, printed before the handover (the docker branch execs
    # away and never returns here). Omitted entirely when it could not be read - a
    # guessed port in a line the user is meant to click is worse than no line.
    if target.web_url:
        stderr_console.print(f"[dim]Web: {target.web_url}[/dim]")

    # The handover switch: the frontend's own final lines (design Decision 7).
    if target.editor == "docker":
        stderr_console.print(
            f"[bold green]Opening shell in {target.container_name}...[/bold green]"
        )
        exec_into_container(target.container_name, working_dir=target.working_dir)
    else:
        vscode_utils.open_in_vscode(
            target.editor, target.container_name, target.working_dir, verbose=verbose
        )
