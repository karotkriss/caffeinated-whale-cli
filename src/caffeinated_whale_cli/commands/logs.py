import shlex
import subprocess
import sys

import typer

from ..core import supervision
from ..core.resolvers import DEFAULT_BENCH_PATH
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_project_containers, handle_docker_errors
from .utils import ensure_containers_running, resolve_bench_path


def _existing_files(container_name: str, files: list[str]) -> list[str]:
    """The subset of ``files`` that exist in the container (one exec), order preserved."""
    if not files:
        return []
    checks = "".join(f"if [ -f {shlex.quote(f)} ]; then echo {shlex.quote(f)}; fi;" for f in files)
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-c", checks],
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _discover_bench_log_files(container_name: str, bench_path: str) -> list[str]:
    """The real ``*.log`` files a bench writes under ``logs/`` (one exec, sorted).

    The not-cwcli-supervised fallback: a bench running under honcho / ``bench start``
    (a pre-v3 instance, or a plain ``bench start``) writes differently-named log
    files than supervisord's ``<program>.supervisor.log``, so DISCOVER what is
    actually present rather than assume supervisord names. Excludes supervisord's
    own per-process logs (the supervised path stays the sole source for those); the
    ``*.log`` glob already skips cwcli's dotfile state (``.cwcli-*``).
    """
    logs = shlex.quote(f"{bench_path}/logs")
    result = subprocess.run(
        ["docker", "exec", container_name, "sh", "-c", f"ls -1 {logs}/*.log 2>/dev/null"],
        capture_output=True,
        text=True,
    )
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return sorted(f for f in files if not f.endswith(supervision._PROC_LOG_SUFFIX))


def _program_log_matches(file_path: str, program: str) -> bool:
    """Whether a discovered log file belongs to Procfile ``program`` (honcho naming).

    honcho/``bench start`` log names are not the supervisord ``<program>.supervisor.log``
    form, so ``--process`` filtering in the fallback matches on the file stem:
    ``web`` -> ``web.log``/``web.error.log``, ``worker_default`` -> ``worker.log``,
    ``schedule`` -> ``schedule.log``/``scheduler.log``. Underscores and dashes are
    treated alike (``redis_cache`` -> ``redis-cache.log``).
    """
    stem = file_path.rsplit("/", 1)[-1]
    if stem.endswith(".log"):
        stem = stem[:-4]
    base = "worker" if program.startswith(("worker_", "worker:")) else program

    def _norm(s: str) -> str:
        return s.lower().replace("_", "-")

    return _norm(stem).startswith(_norm(base))


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
    # Ensure containers are running, prompt user if not (auto-start with --yes)
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)

    containers = get_project_containers(project_name)

    if not containers:
        console.print(f"[bold red]Error: Project '{project_name}' not found.[/bold red]")
        raise typer.Exit(code=1)

    # Find the frappe container
    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if not frappe_container:
        stderr_console.print(
            f"[bold red]Error: No 'frappe' service found for project '{project_name}'.[/bold red]"
        )
        raise typer.Exit(code=1)

    container_name = frappe_container.name
    # Resolve which bench's logs to tail, via the shared wrapper (single bench
    # unchanged; multi-bench uses the --bench / select_bench contract). The log
    # paths come from the ONE source of truth in the supervision substrate.
    bench_path = (
        resolve_bench_path(project_name, bench, None, verbose=verbose) or DEFAULT_BENCH_PATH
    )

    # supervisord writes one log file per Procfile program. --process tails just
    # that one; otherwise tail every program's file for a combined view.
    programs = supervision.procfile_programs(frappe_container, bench_path)
    program: str | None = None
    if process:
        program = supervision.program_for_label(programs, process)
        if program is None:
            valid = [supervision._normalize_procfile_key(p) for p in programs]
            stderr_console.print(
                f"[bold red]Error: No process '{process}' in bench '{bench_path}'.[/bold red]"
            )
            if valid:
                stderr_console.print(f"[dim]Valid processes: {', '.join(valid)}[/dim]")
            raise typer.Exit(code=1)
        log_files = [supervision.process_log_path(bench_path, program)]
    else:
        log_files = [supervision.process_log_path(bench_path, p) for p in programs]

    # Keep only the log files that actually exist in the container (a program that
    # has produced no output yet has no file); tail errors on a missing path.
    existing = _existing_files(container_name, log_files)
    if not existing:
        # No cwcli-supervisord per-process logs for this bench. It may instead be
        # running under honcho / `bench start` (a pre-v3 instance, or a plain
        # `bench start`), whose real log files under {bench}/logs/ are named
        # differently. Fall back to discovering and tailing those - a PURE READ that
        # never launches, installs, or restarts supervisord or the bench.
        fallback = supervision.discover_unsupervised_stack(frappe_container, bench_path)
        if fallback.manager_up:
            real = _discover_bench_log_files(container_name, bench_path)
            if program is not None:
                real = [f for f in real if _program_log_matches(f, program)]
            existing = real
            if existing:
                stderr_console.print(
                    "[dim](bench not under cwcli supervision - tailing its raw log files)[/dim]"
                )
        if not existing:
            if process:
                stderr_console.print(
                    f"[bold red]Error: No logs found for process '{process}' under "
                    f"'{bench_path}/logs'.[/bold red]"
                )
            else:
                stderr_console.print(
                    f"[bold red]Error: No process logs found under '{bench_path}/logs'.[/bold red]"
                )
            if fallback.manager_up:
                stderr_console.print(
                    "[dim]The bench is running but has not written those logs yet.[/dim]"
                )
            else:
                stderr_console.print(
                    "[dim]The bench may not be running. "
                    f"Start it with: cwcli start {project_name}[/dim]"
                )
            raise typer.Exit(code=1)

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: Tailing {', '.join(existing)}[/dim]")

    console.print(f"[bold green]Viewing bench logs for '{project_name}'...[/bold green]")
    console.print("[dim]Press Ctrl+c to exit[/dim]\n")

    # Only request an interactive TTY (`docker exec -it`) when we actually have one:
    # under a pipe/agent (non-TTY) `-it` errors "the input device is not a TTY".
    exec_flags = ["-it"] if sys.stdin.isatty() else []
    tail_flags = ["-F", "-n", str(lines)] if follow else ["-n", str(lines)]
    tail_cmd = [
        "docker",
        "exec",
        *exec_flags,
        container_name,
        "tail",
        *tail_flags,
        *existing,
    ]

    if verbose:
        stderr_console.print(f"[dim]VERBOSE: $ {' '.join(tail_cmd)}[/dim]")

    try:
        result = subprocess.run(tail_cmd)
    except KeyboardInterrupt:
        # Ctrl+C on the non-TTY path (no `-it`): SIGINT reaches this process.
        console.print("\n[yellow]Stopped viewing logs.[/yellow]")
        return

    # 130 is `tail` killed by SIGINT, i.e. the user's own Ctrl+C: on the `-it`
    # path docker puts the terminal in raw mode and forwards ^C into the
    # container, so the stop arrives as an exit code rather than as the
    # KeyboardInterrupt above. Treating it as a failure would make every
    # interactive `cwcli logs -f` exit non-zero.
    if result.returncode == 130:
        console.print("\n[yellow]Stopped viewing logs.[/yellow]")
        return

    if result.returncode != 0:
        # The returncode is propagated, not flattened to 1: `subprocess.run`
        # without check= silently discarded it, so `cwcli logs` reported success
        # for every failed tail.
        stderr_console.print(
            f"[bold red]Error:[/bold red] Failed to tail log file "
            f"(tail exited {result.returncode})."
        )
        raise typer.Exit(code=result.returncode)
