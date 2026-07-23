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

from docker.errors import APIError, NotFound

from . import resolvers, supervision
from .docker import get_project_containers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class StopOutcome:
    """The typed outcome of a stop (serializable, no live objects)."""

    project: str
    stopped: int  # containers THIS call stopped
    already_stopped: bool  # found, but nothing was running
    containers: list[str]  # names of the containers stopped, never Container objects


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

    stopped_names = []
    for container in running:
        container.stop()
        stopped_names.append(container.name)

    return Result(
        status=Status.OK,
        data=StopOutcome(
            project=project_name,
            stopped=len(stopped_names),
            already_stopped=False,
            containers=stopped_names,
        ),
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
            ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker daemon."
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
