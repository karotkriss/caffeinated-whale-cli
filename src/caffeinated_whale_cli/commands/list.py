# src/caffeinated_whale_cli/commands/list.py

import typer
import docker
from docker.errors import DockerException
from typing import List, Dict, Set

app = typer.Typer(help="List running Frappe projects")


def _get_container_ports(container) -> Set[str]:
    ports = set()
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


def _list_instances(service_name: str = "frappe") -> List[Dict]:
    try:
        client = docker.from_env()
        client.ping()
        containers = client.containers.list(
            all=True, filters={"label": f"com.docker.compose.service={service_name}"}
        )
    except DockerException:
        return []

    projects = {}
    for container in containers:
        project_name = container.labels.get("com.docker.compose.project")
        if not project_name:
            continue
        if project_name not in projects:
            projects[project_name] = {"status": container.status, "ports": set()}
        ports = _get_container_ports(container)
        projects[project_name]["ports"].update(ports)

    return [
        {
            "projectName": name,
            "ports": sorted(list(data["ports"])),
            "status": data["status"],
        }
        for name, data in projects.items()
    ]


@app.command("pods")
def list_frappe_instances():
    """
    List running Docker containers labeled as 'frappe' services, grouped by project.
    """
    instances = _list_instances()
    if not instances:
        typer.echo("No Frappe services found.")
        raise typer.Exit()

    for inst in instances:
        ports = ", ".join(inst["ports"]) if inst["ports"] else "none"
        typer.echo(f"[{inst['status']}] {inst['projectName']} → ports: {ports}")


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context):
    """
    Default to listing pods if no subcommand is passed.
    """
    if ctx.invoked_subcommand is None:
        list_frappe_instances()
