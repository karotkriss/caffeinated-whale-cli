"""``core.list_instances`` - the read-only instance listing, lifted into the core.

Scans Docker for Frappe/ERPNext compose projects and returns typed, serializable
:class:`InstanceDTO` rows - no live container object crosses the return boundary.
Raises :class:`~.errors.CwcliError` (``DOCKER``) when the daemon is unreachable;
each frontend maps that to its own exit code and rendering. The port-range
condensing stays presentation and lives in ``commands/list.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import docker
from docker.errors import DockerException

from .envelope import Result, Status
from .errors import CwcliError, ErrorKind


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceDTO:
    """One compose project's status and sorted host ports (serializable, no live objects)."""

    project_name: str
    status: str
    ports: list[str]


def _container_ports(container) -> set[str]:
    """The set of host ports a live container publishes (both mapping sources)."""
    ports: set[str] = set()
    if container.ports:
        for _container_port, host_ports in container.ports.items():
            if host_ports:
                for host_port_info in host_ports:
                    if host_port_info and "HostPort" in host_port_info:
                        ports.add(host_port_info["HostPort"])
    if not ports and container.attrs:
        port_bindings = container.attrs.get("HostConfig", {}).get("PortBindings")
        if port_bindings:
            for _container_port, bindings in port_bindings.items():
                if bindings:
                    for binding in bindings:
                        if "HostPort" in binding and binding["HostPort"]:
                            ports.add(binding["HostPort"])
    return ports


def list_instances(*, service_name: str = "frappe") -> Result[list[InstanceDTO]]:
    """List all Frappe/ERPNext instances Docker Compose manages.

    Returns ``Result(OK, [InstanceDTO, ...])`` (an empty list is a valid, definitive
    empty state). Raises ``CwcliError(DOCKER)`` when the daemon is unreachable.
    """
    try:
        client = docker.from_env()
        client.ping()
        containers = client.containers.list(
            all=True, filters={"label": f"com.docker.compose.service={service_name}"}
        )
    except DockerException as e:
        raise CwcliError(
            ErrorKind.DOCKER,
            "docker.unreachable",
            "Could not connect to Docker daemon.",
            detail={"output": str(e)},
        ) from e

    projects: dict[str, dict] = {}
    for container in containers:
        project_name = container.labels.get("com.docker.compose.project")
        if not project_name:
            continue
        entry = projects.setdefault(project_name, {"status": container.status, "ports": set()})
        entry["ports"].update(_container_ports(container))

    instances = [
        # Sort ports numerically ("8000" before "10000"), not lexicographically.
        InstanceDTO(project_name=name, status=data["status"], ports=sorted(data["ports"], key=int))
        for name, data in projects.items()
    ]
    return Result(status=Status.OK, data=instances)
