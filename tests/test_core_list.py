"""``core.list_instances`` branch coverage against a fake Docker client.

The core prints/exits nothing: it returns ``Result(OK, [InstanceDTO, ...])`` (an
empty list is a valid, definitive empty state) or raises ``CwcliError(DOCKER)``.
These are plain function calls with a stubbed ``docker.from_env``.
"""

import dataclasses

import pytest
from docker.errors import DockerException

from caffeinated_whale_cli.core import list as core_list
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


class FakeContainer:
    def __init__(self, *, project, status="running", ports=None, attrs=None, service="frappe"):
        self.status = status
        self.ports = ports or {}
        self.attrs = attrs or {}
        self.labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
        }


class FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, *, all=False, filters=None):  # noqa: A002 - mirror docker-py's kwarg
        return self._containers


class FakeClient:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)

    def ping(self):
        return True


@pytest.fixture
def wire(monkeypatch):
    def _wire(containers):
        monkeypatch.setattr(core_list.docker, "from_env", lambda: FakeClient(containers))

    return _wire


def _ports(*host_ports):
    """A docker-py ``.ports`` mapping publishing the given host ports."""
    return {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": hp} for hp in host_ports]}


class TestListInstances:
    def test_empty_is_ok_empty_list(self, wire):
        wire([])
        result = core_list.list_instances()
        assert result.status is Status.OK
        assert result.data == []

    def test_single_instance(self, wire):
        """`list_instances` maps one container to an InstanceDTO with sorted ports."""
        wire([FakeContainer(project="proj-a", status="running", ports=_ports("8000", "8001"))])
        result = core_list.list_instances()
        assert result.status is Status.OK
        [inst] = result.data
        assert inst.project_name == "proj-a"
        assert inst.status == "running"
        assert inst.ports == ["8000", "8001"]  # sorted

    def test_ports_aggregate_and_sort_across_containers(self, wire):
        wire(
            [
                FakeContainer(project="proj-a", ports=_ports("9000")),
                FakeContainer(project="proj-a", ports=_ports("8000", "8002")),
            ]
        )
        [inst] = core_list.list_instances().data
        assert inst.ports == ["8000", "8002", "9000"]

    def test_ports_sorted_numerically_not_lexicographically(self, wire):
        # Lexicographic sort would put "10000" before "8000" (and "8000" after "10000");
        # numeric sort must order them 8000 < 9000 < 10000.
        wire([FakeContainer(project="proj-a", ports=_ports("10000", "8000", "9000"))])
        [inst] = core_list.list_instances().data
        assert inst.ports == ["8000", "9000", "10000"]

    def test_ports_fall_back_to_host_config_bindings(self, wire):
        # No published .ports, but PortBindings in attrs (a stopped container).
        attrs = {"HostConfig": {"PortBindings": {"8000/tcp": [{"HostPort": "8080"}]}}}
        wire([FakeContainer(project="proj-a", status="exited", ports={}, attrs=attrs)])
        [inst] = core_list.list_instances().data
        assert inst.ports == ["8080"]

    def test_container_without_project_label_skipped(self, wire):
        c = FakeContainer(project="proj-a")
        c.labels.pop("com.docker.compose.project")
        wire([c])
        assert core_list.list_instances().data == []

    def test_docker_error_raises_docker_kind(self, monkeypatch):
        def _boom():
            raise DockerException("daemon down")

        monkeypatch.setattr(core_list.docker, "from_env", _boom)
        with pytest.raises(CwcliError) as exc:
            core_list.list_instances()
        assert exc.value.kind is ErrorKind.DOCKER
        assert exc.value.code == "docker.unreachable"

    def test_dto_is_json_safe(self, wire):
        wire([FakeContainer(project="proj-a", ports=_ports("8000"))])
        [inst] = core_list.list_instances().data
        blob = dataclasses.asdict(inst)
        assert blob == {"project_name": "proj-a", "status": "running", "ports": ["8000"]}
