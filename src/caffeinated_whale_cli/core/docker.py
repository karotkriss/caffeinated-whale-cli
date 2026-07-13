"""Core Docker accessors: resolve a container, raise typed errors, leak nothing.

The frappe container object stays INTERNAL to the core function that execs into
it - it is never placed in a DTO returned across a ``core.<verb>`` boundary. The
CLI-facing ``docker_utils.get_frappe_container`` is re-expressed as a thin
wrapper over :func:`get_frappe_container` here, so there is one resolution
implementation and the CLI's message/exit behavior is preserved.
"""

from __future__ import annotations

from ..utils.docker_utils import get_project_containers
from .errors import CwcliError, ErrorKind


def get_frappe_container(project_name: str):
    """Resolve a project's frappe container to an exec-usable handle.

    Raises :class:`CwcliError` (``DOCKER`` when the daemon is unreachable,
    ``NOT_FOUND`` when the project or its frappe service is absent) instead of
    printing and calling ``typer.Exit``. The returned container is for the
    caller's own exec calls; it must not be surfaced in any returned DTO.
    """
    containers = get_project_containers(project_name)

    if containers is None:
        # get_project_containers returns None ONLY on a Docker connection error.
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

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )
    if frappe_container is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "frappe.not_found",
            f"No 'frappe' service found for project '{project_name}'.",
        )

    return frappe_container
