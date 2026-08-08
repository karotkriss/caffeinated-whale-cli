"""``cwcli run`` - the thin frontend over ``core.run_plan`` + ``core.exec_stream``.

Typer signature, the auto-start prompt, rendering, and the exit code live here;
everything else is the core's. See ``core/run.py`` for why the resolve and the
stream are two calls (generators are lazy), and ``core/exec_stream.py`` for why
the exit code is polled rather than read once.

``--interactive`` is a SECOND, dedicated mechanism rather than a widening of the
first - see :func:`_exec_interactive` for why the exec-stream contract cannot
carry stdin without giving up the decisions that contract exists to own.

Both paths run inside :func:`_credential_bridged`, so a private-repo git fetch
the bench subcommand makes (``run <p> get-app <private-url>``,
``run <p> update --pull``; every ``run`` executes ``bench <args>``, never raw
git) authenticates through the host's ``gh``/``glab`` exactly as ``apps
install`` does. The wrapping lives HERE, not in ``core.run_stream``: a context
manager inside an abandoned generator never runs its teardown (the
``core.update`` try/finally lesson), while a plain ``with`` around the
consuming loop always does.
"""

import contextlib
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterator

import typer
from rich.console import Console

from ..core import docker as core_docker
from ..core.credbridge import credential_bridge
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..core.exec_stream import ExecChunk
from ..core.run import RunPlan, run_plan, run_stream
from ..utils import cred_daemon
from ..utils.completion_utils import complete_project_names
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running

stderr_console = Console(stderr=True)


@contextlib.contextmanager
def _credential_bridged(plan: RunPlan) -> Iterator[None]:
    """Authenticate the planned bench command's git fetches like ``apps install``.

    Both ``run`` paths (streamed and ``-i``) stay in-process, so the same
    per-invocation :func:`~..core.credbridge.credential_bridge` the ``apps``/
    ``init``/``update`` git ops wrap works here - and it is inert for public
    repos and for bench commands that fetch nothing (git only calls a credential
    helper on a 401), so it wraps EVERY run without pre-detecting a fetch.
    Before falling back to it,
    ``ensure_bridge`` wires this instance into the PERSISTENT bridge when
    that feature is enabled (``run`` is one of its ensure call sites, next to
    ``open``/``core.start``); a non-``None`` outcome means the stable helper
    already answers for this instance, so standing up a second, per-invocation
    helper line for the duration would be redundant - the daemon-skip.
    ``ensure_bridge`` never raises and returns ``None`` when the feature is
    disabled, so the common opt-out case goes straight to the ephemeral bridge.
    """
    container = core_docker.get_container(plan.container_id)
    if cred_daemon.ensure_bridge(container, plan.bench_path, plan.project) is not None:
        yield
        return
    with credential_bridge(container, plan.bench_path):
        yield


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
        argv.extend(["setsid", "sh", "-c", script, "cwcli-run", cleanup_pidfile, *command])

    if verbose:
        stderr_console.print(f"[dim]$ {' '.join(argv)}[/dim]")

    try:
        result = subprocess.run(argv)
        return result.returncode
    except KeyboardInterrupt:
        if cleanup_pidfile is not None and not _kill_interactive_process_group(
            plan.container_id, cleanup_pidfile
        ):
            stderr_console.print(
                "[bold red]Error:[/bold red] Interrupted command termination "
                "could not be verified; it may still be running in the container."
            )
            return 1
        return 130
    except BaseException:
        if cleanup_pidfile is not None:
            _kill_interactive_process_group(plan.container_id, cleanup_pidfile)
        raise


def _kill_interactive_process_group(container_id: str, pidfile: str) -> bool:
    quoted = shlex.quote(pidfile)
    script = (
        f'i=0; while [ ! -s {quoted} ] && [ "$i" -lt 20 ]; '
        "do i=$((i + 1)); sleep 0.05; done; "
        f"pid=$(cat {quoted} 2>/dev/null) || exit 2; "
        'case "$pid" in ""|*[!0-9]*) exit 2;; esac; '
        'if kill -0 -- "-$pid" 2>/dev/null; then '
        'kill -TERM -- "-$pid" 2>/dev/null; '
        'i=0; while kill -0 -- "-$pid" 2>/dev/null && [ "$i" -lt 20 ]; '
        "do i=$((i + 1)); sleep 0.05; done; "
        'if kill -0 -- "-$pid" 2>/dev/null; then '
        'kill -KILL -- "-$pid" 2>/dev/null || exit 3; '
        'i=0; while kill -0 -- "-$pid" 2>/dev/null && [ "$i" -lt 20 ]; '
        "do i=$((i + 1)); sleep 0.05; done; "
        "fi; fi; "
        f"rm -f {quoted}; "
        'if kill -0 -- "-$pid" 2>/dev/null; then exit 4; fi'
    )
    try:
        result = subprocess.run(
            ["docker", "exec", container_id, "sh", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        return False
    return result.returncode == 0


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
        with _credential_bridged(plan):
            code = _exec_interactive(plan, verbose=verbose)
        raise typer.Exit(code=code)

    exit_code = 1
    try:
        with _credential_bridged(plan):
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
