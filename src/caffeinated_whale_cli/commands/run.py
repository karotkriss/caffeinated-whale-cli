"""``cwcli run`` - the thin frontend over ``core.run_plan`` + ``core.exec_stream``.

Typer signature, the auto-start prompt, rendering, and the exit code live here;
everything else is the core's. See ``core/run.py`` for why the resolve and the
stream are two calls (generators are lazy), and ``core/exec_stream.py`` for why
the exit code is polled rather than read once.

``--interactive`` is a SECOND, dedicated mechanism rather than a widening of the
first - see :func:`_exec_interactive` for why the exec-stream contract cannot
carry stdin without giving up the decisions that contract exists to own.
"""

import os
import shlex
import subprocess
import sys
import uuid

import typer
from rich.console import Console

from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.exec_stream import ExecChunk
from ..core.run import RunPlan, run_plan, run_stream
from ..utils.completion_utils import complete_project_names
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running

stderr_console = Console(stderr=True)


def _exec_interactive(plan: RunPlan, *, verbose: bool) -> int:
    """Run the planned bench command through ``docker exec``, stdin attached.

    A DEDICATED interactive primitive, deliberately NOT a widening of
    ``core.exec_stream``. That contract owns the decode and pins ``demux=True``
    (``init`` routes container stderr to ``sys.stderr``; ``axi`` needs stdout
    purity), and it starts the exec with ``stream=True``, which is one-way by
    construction: docker-py returns a read-only generator, so forwarding stdin
    through it would mean ``exec_create(stdin=True)`` plus a raw ``socket=True``
    start, a caller-side frame demuxer and a pump thread. A genuinely
    interactive prompt additionally wants ``tty=True``, which is mutually
    exclusive with that locked ``demux=True`` stream tag - the same collision
    ``core/logs.py`` records for ``--follow``.

    So this reuses the mechanism ``logs --follow`` already ships: hand the exec
    to the ``docker`` CLI, which does the stdin pump and raw mode itself. The
    non-TTY path also gives the command its own process group so an interrupt can
    reap the container-side command before returning. ``core/exec_stream.py`` is
    untouched, and the default non-interactive ``cwcli run`` still streams
    through it.

    The ``docker`` binary is guaranteed present: ``@handle_docker_errors``
    already refuses without it.
    """
    # `-t` only when a real terminal is on BOTH ends. It is what puts docker in
    # raw mode, so a prompt can be answered keystroke-by-keystroke and ^C reaches
    # bench - but it also makes the container see a TTY, so bench emits colour and
    # CRLF line endings. Requesting it when stdout is a pipe would corrupt
    # captured output, and requesting it without a TTY on stdin fails outright
    # ("the input device is not a TTY"). `-i` alone still carries a pipe's or a
    # file's bytes into the command, which is the non-interactive half of the
    # both-modes contract - an agent answers by piping the same lines a human
    # types.
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    flags = ["-i"]
    if is_tty:
        flags.append("-t")

    # `shlex.split` undoes the quoting `core.run_plan` applied for docker-py,
    # which splits a command STRING itself where the docker CLI takes an argv.
    # Round-tripping the plan's own command keeps the assembly decision in the
    # core rather than re-deriving it here from the raw args.
    command = shlex.split(plan.command)
    argv = [
        "docker",
        "exec",
        *flags,
        "-w",
        plan.bench_path,
        plan.container_id,
    ]
    cleanup_pidfile = None
    if is_tty:
        argv.extend(command)
    else:
        cleanup_pidfile = f"/tmp/cwcli-run-{os.getpid()}-{uuid.uuid4().hex}.pid"
        script = (
            'pidfile="$1"; shift; echo $$ > "$pidfile"; '
            '"$@"; status=$?; rm -f "$pidfile"; exit "$status"'
        )
        argv.extend(
            ["setsid", "sh", "-c", script, "cwcli-run", cleanup_pidfile, *command]
        )

    if verbose:
        stderr_console.print(f"[dim]$ {' '.join(argv)}[/dim]")

    completed = False
    try:
        result = subprocess.run(argv)
        completed = True
        return result.returncode
    except KeyboardInterrupt:
        return 130
    finally:
        if cleanup_pidfile is not None and not completed:
            _kill_interactive_process_group(plan.container_id, cleanup_pidfile)


def _kill_interactive_process_group(container_id: str, pidfile: str) -> None:
    quoted = shlex.quote(pidfile)
    script = (
        f'pid=$(cat {quoted} 2>/dev/null) || exit 0; '
        'kill -TERM -- "-$pid" 2>/dev/null; sleep 1; '
        'kill -KILL -- "-$pid" 2>/dev/null; '
        f"rm -f {quoted}"
    )
    try:
        subprocess.run(
            ["docker", "exec", container_id, "sh", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass


@handle_docker_errors
def run(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench_args: list[str] = typer.Argument(..., help="Bench command and arguments to run."),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench to target: its numeric index or label (from 'cwcli inspect').",
    ),
    bench_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Explicit bench directory inside the container (lower-level alternative to --bench).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Forward stdin to the bench command, for commands that prompt (e.g. new-app).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Execute `bench <command>` inside the specified project's frappe container.

    Flags this command does not define are passed through to bench, so
    `cwcli run my-project --site example.com migrate` works. The options below
    stay this command's wherever they appear; to send bench a flag that collides
    with one of them, put it after a `--` separator, as in
    `cwcli run my-project -- build --verbose`.

    Use `-i` for a bench command that prompts: it attaches stdin, so a human can
    answer at a terminal and automation can pipe the answers in
    (`printf 'a\\nb\\n' | cwcli run my-project -i new-app my_app`).
    """
    # Interactive prologue: prompts happen HERE, before the core call. The core
    # then re-checks and only returns confirm_start on the (rare) race.
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    started = False
    while True:
        try:
            result = run_plan(
                project_name, bench_args, bench=bench, bench_path=bench_path, auto_start=yes
            )
        except CwcliError as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
            if e.hint:
                stderr_console.print(f"[dim]{e.hint}[/dim]")
            raise typer.Exit(code=1) from e

        if (
            result.status is Status.NEEDS_CHOICE
            and result.choice is not None
            and result.choice.kind == "confirm_start"
        ):
            # Mirrors backup.py/unlock.py: re-invoke at most ONCE after an attempted
            # start. A second confirm_start after ensure_containers_running already
            # claimed success means the start didn't take - fail closed, don't spin.
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
        break

    if result.choice is not None:
        stderr_console.print(f"[bold red]Error:[/bold red] {result.choice.prompt}")
        for option in result.choice.options or []:
            stderr_console.print(f"  [dim]{option['value']}[/dim]  {option['label']}")
        stderr_console.print("[dim]Pass --bench <index|label> to choose one.[/dim]")
        raise typer.Exit(code=1)

    plan = result.data
    assert plan is not None  # OK always carries a RunPlan

    if verbose:
        for warning in result.warnings:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
        stderr_console.print(f"[dim]$ {plan.command}  (in {plan.bench_path})[/dim]")

    if interactive:
        raise typer.Exit(code=_exec_interactive(plan, verbose=verbose))

    exit_code = 1
    try:
        for event in run_stream(plan):
            if isinstance(event, ExecChunk):
                # Both tags to stdout, reproducing the combined stream this
                # command has always shown.
                typer.echo(event.text, nl=False)
            else:
                exit_code = event.exit_code
    except CwcliError as e:
        # An unknown exit code lands here rather than being reported as success.
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        if e.hint:
            stderr_console.print(f"[dim]{e.hint}[/dim]")
        raise typer.Exit(code=1) from e

    raise typer.Exit(code=exit_code)
