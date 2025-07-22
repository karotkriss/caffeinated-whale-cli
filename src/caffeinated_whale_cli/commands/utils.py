import docker
from docker.errors import DockerException
from typing import List

def get_project_containers(project_name: str) -> List[docker.models.containers.Container] | None:
    """
    Finds all containers belonging to a specific Docker Compose project.

    Args:
        project_name: The name of the docker-compose project.

    Returns:
        A list of container objects, an empty list if not found,
        or None if there was a Docker connection error.
    """
    try:
        client = docker.from_env()
        client.ping()
        
        containers = client.containers.list(
            all=True, 
            filters={"label": f"com.docker.compose.project={project_name}"}
        )
        return containers

    except DockerException:
        return None