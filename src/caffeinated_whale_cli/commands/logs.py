"""``cwcli logs`` - the thin frontend over ``core.logs_plan`` plus the tail it owns.

``core.logs_plan`` resolves WHICH log files to tail (container, bench, program
selection, the existence probe, and the honcho/``bench start`` fallback); this
frontend renders its choices and errors, then performs the ``docker exec -it ...
tail`` itself. The tail stays HERE, not on ``core.exec_stream``: see
``core/logs.py``'s module docstring for the measured reasons (an orphan ``tail
-F`` per Ctrl+C, ``exec.stream_lost`` on a routine stop, and the ``tty``/``demux``
conflict). ``logs`` is a PURE READ; nothing on any path launches or mutates the
bench.

The non-TTY ``--follow`` path used to leak that same orphan ``tail -F`` on its
own (the ``docker exec`` client dies on Ctrl+C, but the exec'd tail keeps
running, and Docker has no kill-exec API): it now wraps the tail so it records
its own PID and reaps that PID on exit (``_kill_container_tail``). The ``-it``
path is orphan-free without help - docker forwards ^C into the container.
"""

import os
import shlex
import subprocess
import sys
import uuid
from typing import NoReturn

import typer

from ..core import logs as core_logs
from ..core.envelope import Status
from ..core.errors import CwcliError
from ..utils import bench_labels
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from .utils import ensure_containers_running


@handle_docker_errors
def logs(
    project_name: str = typer.Argument(
        ...,
        help="The name of the Frappe project to view logs for.",
        autocompletion=complete_project_names,
    ),
    follow: bool = typer.Option(
        False,
        "--follow/--no-follow",
        "-f",
        help="Follow log output in real-time.",
    ),
    lines: int = typer.Option(
        100,
        "--lines",
        "-n",
        help="Number of lines to show from the end of the logs.",
    ),
    bench: str = typer.Option(
        None,
        "--bench",
        help="Which bench's logs to view: its numeric index or label (multi-bench projects).",
    ),
    process: str = typer.Option(
        None,
        "--process",
        "-p",
        help="Tail ONE process's log (e.g. web, worker, socketio). Omit to see a "
        "combined view of every process.",
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
    View bench logs from supervisord's per-process log files.

    With --process, tail that one program's log; without it, a combined view of
    every process's log. When cwcli's supervisord is not managing the bench (a
    pre-v3 instance, or a bench brought up with honcho / ``bench start``), fall back
    to the bench's real log files under ``logs/`` so a genuinely-running bench still
    shows its logs. Always a PURE READ - never launches or mutates the bench.
    """
    # Interactive prologue: the auto-start prompt happens HERE, before the core call
    # (auto-start with --yes). core.logs_plan then re-checks and only returns
    # confirm_start on the rare race that the container died in between.
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    started = False
    while True:
        try:
            result = core_logs.logs_plan(
                project_name,
                bench=bench,
                process=process,
                follow=follow,
                lines=lines,
                auto_start=yes,
            )
        except CwcliError as e:
            _render_error_and_exit(e)

        if result.status is Status.NEEDS_CHOICE and result.choice is not None:
            choice = result.choice
            if choice.kind == "confirm_start":
                # Re-invoke at most ONCE after an attempted start (mirrors run.py): a
                # second confirm_start after the prologue claimed success means the
                # start didn't take - fail closed, don't spin.
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
            if choice.kind == "select_bench":
                _render_select_bench_error(project_name, choice)
            if choice.kind == "select_process":
                _render_select_process_error(choice)
        break

    plan = result.data
    assert plan is not None  # OK always carries a LogsPlan

    if plan.not_cwcli_supervised:
        stderr_console.print(
            "[dim](bench not under cwcli supervision - tailing its raw log files)[/dim]"
        )

    if verbose:
        for warning in result.warnings:
            stderr_console.print(f"[dim]{warning.text}[/dim]")
        stderr_console.print(f"[dim]VERBOSE: Tailing {', '.join(plan.log_files)}[/dim]")

    console.print(f"[bold green]Viewing bench logs for '{project_name}'...[/bold green]")
    console.print("[dim]Press Ctrl+c to exit[/dim]\n")

    # Only request an interactive TTY (`docker exec -it`) when we actually have one:
    # under a pipe/agent (non-TTY) `-it` errors "the input device is not a TTY".
    is_tty = sys.stdin.isatty()
    exec_flags = ["-it"] if is_tty else []
    tail_flags = ["-F", "-n", str(plan.lines)] if plan.follow else ["-n", str(plan.lines)]

    # Orphan-tail leak on the non-TTY `--follow` path: Ctrl+C kills the `docker
    # exec` client here, but its exec'd `tail -F` keeps running INSIDE the
    # container (Docker has no kill-exec API), so every Ctrl+C accumulates another
    # orphan. The `-it` path is verified clean - docker forwards ^C into the
    # container, tail exits 130, zero orphans (see core/logs.py's docstring) - so
    # only the non-TTY follow needs help. Wrap the tail so it records its own PID,
    # then kill that PID on exit. A bounded `tail -n N` (no follow) exits on its
    # own and cannot orphan, so it stays a plain exec.
    cleanup_pidfile = None
    if plan.follow and not is_tty:
        cleanup_pidfile = f"/tmp/cwcli-logs-{os.getpid()}-{uuid.uuid4().hex}.pid"
        # `echo $$` records the shell's PID; `exec tail` then REPLACES the shell,
        # so the exec'd tail inherits that exact PID - the one to kill on cleanup.
        tail_argv = ["tail", *tail_flags, *plan.log_files]
        script = "echo $$ > {pf}; exec {t}".format(
            pf=shlex.quote(cleanup_pidfile),
            t=" ".join(shlex.quote(a) for a in tail_argv),
        )
        tail_cmd = ["docker", "exec", plan.container_name, "sh", "-c", script]
    else:
        tail_cmd = [
            "docker",
            "exec",
            *exec_flags,
            plan.container_name,
            "tail",
            *tail_flags,
            *plan.log_files,
        ]

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: $ {' '.join(tail_cmd)}[/dim]")

    try:
        completed = subprocess.run(tail_cmd)
    except KeyboardInterrupt:
        # Ctrl+C on the non-TTY path (no `-it`): SIGINT reaches this process.
        console.print("\n[yellow]Stopped viewing logs.[/yellow]")
        return
    finally:
        if cleanup_pidfile is not None:
            _kill_container_tail(plan.container_name, cleanup_pidfile)

    # 130 is `tail` killed by SIGINT, i.e. the user's own Ctrl+C: on the `-it`
    # path docker puts the terminal in raw mode and forwards ^C into the
    # container, so the stop arrives as an exit code rather than as the
    # KeyboardInterrupt above. Treating it as a failure would make every
    # interactive `cwcli logs -f` exit non-zero.
    if completed.returncode == 130:
        console.print("\n[yellow]Stopped viewing logs.[/yellow]")
        return

    if completed.returncode != 0:
        # The returncode is propagated, not flattened to 1: `subprocess.run`
        # without check= silently discarded it, so `cwcli logs` reported success
        # for every failed tail.
        stderr_console.print(
            f"[bold red]Error:[/bold red] Failed to tail log file "
            f"(tail exited {completed.returncode})."
        )
        raise typer.Exit(code=completed.returncode)


def _kill_container_tail(container_name: str, pidfile: str) -> None:
    """Reap the orphaned container-side ``tail -F`` a non-TTY ``--follow`` leaves.

    The wrapped tail wrote its own PID to ``pidfile`` inside the container. The
    ``docker exec`` client dies on Ctrl+C, but the exec'd tail keeps running and
    Docker exposes no kill-exec API, so we reap it by PID with a fresh exec. Uses
    only ``kill``/``cat``/``rm`` - shell builtins/coreutils present everywhere, no
    ``procps``/``pkill`` dependency. Best-effort: a missing PID or already-dead
    tail is a harmless no-op, and any failure here must not mask the real exit.
    """
    quoted = shlex.quote(pidfile)
    try:
        subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "sh",
                "-c",
                f"kill $(cat {quoted} 2>/dev/null) 2>/dev/null; rm -f {quoted}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _render_error_and_exit(e: CwcliError) -> NoReturn:
    """Render a core.logs_plan failure as today's message + hint, and exit 1."""
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.hint:
        stderr_console.print(f"[dim]{e.hint}[/dim]")
    raise typer.Exit(code=1)


def _render_select_bench_error(project_name: str, choice) -> NoReturn:
    """Multi-bench with no selector: error and list the benches (logs' historical
    ``on_ambiguous='error'`` behaviour - a data op refuses rather than guessing)."""
    stderr_console.print(
        f"[bold red]Error:[/bold red] project '{project_name}' has multiple benches; "
        "specify one with --bench <index|label>:"
    )
    from ..core import resolvers

    stderr_console.print(bench_labels.format_bench_list(resolvers.cached_benches(project_name)))
    raise typer.Exit(code=1)


def _render_select_process_error(choice) -> NoReturn:
    """Unknown --process: today's error naming the process and the valid labels."""
    stderr_console.print(f"[bold red]Error: {choice.prompt}[/bold red]")
    valid = [o["value"] for o in choice.options or []]
    if valid:
        stderr_console.print(f"[dim]Valid processes: {', '.join(valid)}[/dim]")
    raise typer.Exit(code=1)
