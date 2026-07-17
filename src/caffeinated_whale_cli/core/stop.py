"""``core.stop`` - stop a project's running containers.

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
"""

from __future__ import annotations

from dataclasses import dataclass

from .docker import get_project_containers
from .envelope import Result, Status
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
