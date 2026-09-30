"""``core.stop`` - stop a project's containers, or ONE bench's dev processes.

Replaces ``commands/stop.py:_stop_project``, which four modules depended on
(`restart`, `start`, `rm`, and `axi`) even though it printed ``rich`` markup to
STDOUT. That coupling was a live defect on the agent surface: ``cwcli axi start
--yes`` stops conflicting Frappe projects, and a teardown race between conflict
detection and the stop reached `_stop_project`'s not-found / already-stopped
prints, putting rich markup onto stdout and corrupting axi's one-TOON-document
contract. A core function that cannot print closes that structurally.

``_stop_project``'s ``None`` sentinel (its way of telling "project absent" apart
from a legitimate zero count) retires into the taxonomy it was approximating:
absent is ``CwcliError(NOT_FOUND)``, a legitimate zero is
``StopOutcome(already_stopped=True)``.

:func:`stop_bench` is the per-bench half, and it is a DIFFERENT operation, not a
narrowed one. One instance is one container holding sibling benches, so there is
no container to stop for a single bench: it shuts down that bench's own
supervisord, leaving its siblings - and every container - running. It is the
inverse of ``core.start``, which has always been per-bench, and it closes the
asymmetry that made ``start``/``status``/``restart --process``/``logs`` all
bench-scoped while stopping one bench meant reaching past cwcli into the
container with ``docker exec ... supervisorctl``.
"""

from __future__ import annotations

from dataclasses import dataclass

from docker.errors import APIError, DockerException, NotFound

from . import resolvers, supervision
from .docker import get_project_containers
from .envelope import Message, Result, Status
from .errors import DOCKER_UNREACHABLE_HINT, CwcliError, ErrorKind

# The database gets a REAL grace period. `container.stop()` with no timeout uses the
# daemon's default - 10s on Docker Engine, and 1s for every container Docker
# Desktop creates - so a MariaDB still flushing InnoDB was SIGKILLed mid-shutdown
# while cwcli reported a clean stop. 60s covers a large buffer pool on a slow disk;
# a DB that is done sooner returns sooner.
DB_SERVICE = "mariadb"
DB_STOP_TIMEOUT = 60
# The line MariaDB (and MySQL) logs LAST on a completed shutdown.
DB_SHUTDOWN_COMPLETE = "Shutdown complete"


def is_database(container) -> bool:
    """True for the project's database container (the compose ``mariadb`` service)."""
    return bool(container.labels.get("com.docker.compose.service") == DB_SERVICE)


def stop_database(container) -> bool:
    """Stop the database with :data:`DB_STOP_TIMEOUT` grace; True iff it shut down cleanly.

    Clean means BOTH a zero exit code (a SIGKILL after the grace ran out is 137) and a
    last log line reading :data:`DB_SHUTDOWN_COMPLETE`. The log is read by position,
    not by a ``since`` timestamp, because the Docker Desktop VM's clock can drift
    from the host's. When the log cannot be read, the exit code alone decides.
    """
    container.stop(timeout=DB_STOP_TIMEOUT)
    container.reload()
    if (container.attrs.get("State") or {}).get("ExitCode") != 0:
        return False
    try:
        tail = container.logs(tail=5).decode("utf-8", "replace")
    except DockerException:
        return True
    lines = [line for line in tail.splitlines() if line.strip()]
    return bool(lines) and DB_SHUTDOWN_COMPLETE in lines[-1]


@dataclass(frozen=True, slots=True, kw_only=True)
class StopOutcome:
    """The typed outcome of a stop (serializable, no live objects)."""

    project: str
    stopped: int  # containers THIS call stopped
    already_stopped: bool  # found, but nothing was running
    containers: list[str]  # names of the containers stopped, never Container objects
    # None when this call stopped no database; False when the database was killed
    # (or crashed) before logging a completed shutdown - a stop that is NOT clean.
    db_clean_shutdown: bool | None = None


def stop(project_name: str) -> Result[StopOutcome]:
    """Stop a project's running containers. See the module docstring.

    Raises :class:`~.errors.CwcliError` (``NOT_FOUND`` when the project has no
    containers, ``DOCKER`` when the daemon is unreachable). Prints nothing.
    """
    # Resolved directly rather than through `core.docker.get_frappe_container`: stop
    # is project-wide (db, redis, frappe) and does NOT require a frappe service, so
    # routing through the frappe accessor would newly fail a project that has
    # containers but no frappe - which `_stop_project` stopped happily.
    containers = get_project_containers(project_name)

    if containers is None:
        # get_project_containers returns None ONLY on a Docker connection error.
        # `_stop_project` reported this as "project not found"; the typed taxonomy
        # tells the two apart, which the frontends render honestly.
        raise CwcliError(
            ErrorKind.DOCKER,
            "docker.unreachable",
            "Could not connect to Docker daemon.",
            hint=DOCKER_UNREACHABLE_HINT,
        )

    if not containers:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "project.not_found",
            f"Project '{project_name}' not found.",
        )

    running = [c for c in containers if c.status == "running"]
    if not running:
        return Result(
            status=Status.OK,
            data=StopOutcome(project=project_name, stopped=0, already_stopped=True, containers=[]),
        )

    # The database stops LAST, once nothing is left connected to it, and is the one
    # container given a real grace period (see DB_STOP_TIMEOUT).
    running.sort(key=is_database)
    stopped_names = []
    db_clean: bool | None = None
    warnings: list[Message] = []
    for container in running:
        if is_database(container):
            db_clean = stop_database(container)
            if not db_clean:
                warnings.append(
                    Message(
                        "stop.db_unclean",
                        f"The database container '{container.name}' did not shut down "
                        f"cleanly: its log never reached '{DB_SHUTDOWN_COMPLETE}' "
                        f"(exit code {(container.attrs.get('State') or {}).get('ExitCode')}). "
                        "MariaDB will run crash recovery on its next start.",
                    )
                )
        else:
            container.stop()
        stopped_names.append(container.name)

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=StopOutcome(
            project=project_name,
            stopped=len(stopped_names),
            already_stopped=False,
            containers=stopped_names,
            db_clean_shutdown=db_clean,
        ),
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchStopOutcome:
    """The typed outcome of stopping ONE bench's dev processes (serializable)."""

    project: str
    bench_path: str
    stopped_processes: list[str]  # the labels that were up and are now down
    already_stopped: bool  # the bench had no supervisord running


def stop_bench(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
) -> Result[BenchStopOutcome]:
    """Stop ONE bench's dev processes, leaving sibling benches and containers up.

    Shuts down that bench's supervisord (see :func:`supervision.shutdown`), which is
    what ``cwcli start`` launched, so the pair is symmetric and ``cwcli start`` can
    relaunch the bench afterwards. Returns ``NEEDS_CHOICE`` ``select_bench`` on a
    multi-bench project with no selector - the same fork ``core.restart_process``
    returns, resolved by each frontend its own way. A bench that is not running is a
    clean ``already_stopped`` success, not an error, so an agent can stop twice.

    Raises ``CwcliError``: ``DOCKER`` (daemon unreachable), ``NOT_FOUND`` (no such
    project / no frappe service), ``NOT_RUNNING`` (the container is stopped, so
    there is nothing bench-scoped to stop - the whole instance is already down).
    """
    warnings: list[Message] = []

    containers = get_project_containers(project_name)
    if containers is None:
        raise CwcliError(
            ErrorKind.DOCKER,
            "docker.unreachable",
            "Could not connect to Docker daemon.",
            hint=DOCKER_UNREACHABLE_HINT,
        )
    if not containers:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "project.not_found", f"Project '{project_name}' not found."
        )

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "project.no_frappe_service",
            f"No 'frappe' service found for project '{project_name}'.",
        )
    try:
        frappe_container.reload()
    except (APIError, NotFound) as e:
        raise CwcliError(
            ErrorKind.DOCKER,
            "container.reload_failed",
            f"Could not read state of the frappe container for project '{project_name}'.",
            detail={"output": str(e)},
        ) from e
    if frappe_container.status != "running":
        raise CwcliError(
            ErrorKind.NOT_RUNNING,
            "container.not_running",
            f"Frappe container for project '{project_name}' is not running.",
            hint=f"The whole instance is already stopped; run 'cwcli start {project_name}'.",
        )

    # The SAME selector start/status/restart use, so a --bench index means one thing.
    resolved = resolvers.resolve_bench(project_name, bench, bench_path)
    if resolved is None:
        resolved_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif resolved.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=resolved.choice)
    else:
        assert resolved.data is not None
        resolved_path = resolved.data
        warnings.extend(resolved.warnings)

    snapshot = supervision.discover_stack(frappe_container, resolved_path)
    if not snapshot.supervisor_up:
        supervision.clear_marker(frappe_container, resolved_path)
        return Result(
            status=Status.OK,
            data=BenchStopOutcome(
                project=project_name,
                bench_path=resolved_path,
                stopped_processes=[],
                already_stopped=True,
            ),
            warnings=warnings,
        )

    # Report what was actually UP before the shutdown, so the outcome names what this
    # call ended rather than what the Procfile says should have been running. Labels
    # are DEDUPLICATED: discovery is per-PID and a Procfile program can hold more than
    # one live process (the bench wrapper plus the process it execs), which would
    # otherwise read as two `web` programs having been stopped.
    stopped = sorted({p.label for p in snapshot.processes if p.up})
    # The SAME teardown ``core.start`` uses for its relaunch: SIGTERM to the bench's
    # own supervisord by discovered PID, waiting (bounded) for the tree to actually
    # exit, escalating to SIGKILL if it overstays. Sibling benches each have their
    # own supervisord and are never signalled.
    supervision.stop_supervisor(frappe_container, resolved_path)
    supervision.clear_marker(frappe_container, resolved_path)

    return Result(
        status=Status.OK,
        data=BenchStopOutcome(
            project=project_name,
            bench_path=resolved_path,
            stopped_processes=stopped,
            already_stopped=False,
        ),
        warnings=warnings,
    )
