"""Real-Docker E2E: ``cwcli start`` recovers an instance whose compose network is gone.

A WSL or Docker crash can remove ``<project>_default`` while the instance's stopped
containers keep pointing at it by id, so ``container.start()`` died with a raw docker
``404 ... network <id> not found`` traceback and the instance needed its network
recreated by hand. ``start`` must recreate the network (same name, driver and compose
labels), keep each container's compose DNS aliases, and start normally - or, for a
network the instance does not own, refuse with the exact ``docker network create``.

The shared-instance test drives the REAL instance on both start surfaces. The
external-network test is standalone and needs no bench: lightweight stand-in
containers carry the compose labels cwcli discovers projects by.
"""

from __future__ import annotations

import pytest

from . import harness

_SLIM = "python:3.12-slim"


def _docker():
    import docker

    return docker.from_env()


def _output(proc) -> str:
    return harness.strip_ansi((proc.stdout or "") + (proc.stderr or ""))


def _remove_network_of(project: str) -> str:
    """Stop the project's containers, then remove its compose network. Returns its name."""
    client = _docker()
    (network,) = client.networks.list(filters={"label": f"com.docker.compose.project={project}"})
    for container_id in harness.project_containers(project):
        client.containers.get(container_id).stop()
    network.remove()
    return network.name


@pytest.mark.e2e
@pytest.mark.parametrize("surface", ["axi", "human"])
def test_start_recreates_a_missing_compose_network(running_instance, surface):
    inst = running_instance
    cwcli_stop = harness.run_cwcli("stop", inst.name)
    assert cwcli_stop.returncode == 0, _output(cwcli_stop)
    name = _remove_network_of(inst.name)
    assert harness.project_networks(inst.name) == []

    args = ["axi", "start", inst.name] if surface == "axi" else ["start", inst.name, "--yes"]
    proc = harness.run_cwcli(*args, timeout=600)
    out = _output(proc)

    assert proc.returncode == 0, f"start did not recover from the missing network:\n{out}"
    assert "Traceback" not in out and "not found" not in out, out
    assert f"The network '{name}' was missing" in out, out
    harness.wait_for_site_ready(inst.name, inst.site)

    (network,) = _docker().networks.list(
        filters={"label": f"com.docker.compose.project={inst.name}"}
    )
    assert network.name == name
    assert network.attrs["Driver"] == "bridge"
    assert network.attrs["Labels"]["com.docker.compose.network"] == "default"
    # compose's service-name DNS survives: the app container still resolves its database
    code, hosts = harness.exec_in_frappe(inst.name, "getent hosts mariadb")
    assert code == 0 and hosts.strip(), f"mariadb no longer resolves from frappe: {hosts}"


@pytest.mark.e2e
@pytest.mark.standalone
def test_start_never_invents_a_missing_network_the_instance_does_not_own():
    project = harness.project_name("netext")
    external = f"{project}-shared"  # not `<project>_<key>`, so not a compose-owned network
    harness.assert_prefixed(project)
    client = _docker()
    client.images.pull(_SLIM)  # `containers.create` never pulls
    net = client.networks.create(external)
    try:
        for service in ("frappe", "mariadb"):
            client.containers.create(
                _SLIM,
                ["sleep", "infinity"],
                name=f"{project}-{service}-1",
                network=external,
                labels={
                    "com.docker.compose.project": project,
                    "com.docker.compose.service": service,
                    "com.docker.compose.container-number": "1",
                },
            )
        net.remove()

        for args in (["axi", "start", project], ["start", project, "--yes"]):
            proc = harness.run_cwcli(*args, timeout=300)
            out = _output(proc)
            assert proc.returncode != 0, f"start exited 0 with a missing external network:\n{out}"
            assert "Traceback" not in out, out
            assert f"docker network create {external}" in out, out
            assert not client.networks.list(names=[external]), "start invented a foreign network"
            assert all(
                client.containers.get(cid).status != "running"
                for cid in harness.project_containers(project)
            ), "start brought containers up before refusing"
    finally:
        harness.sweep_cwe2e(only=project)
        for stale in client.networks.list(names=[external]):
            stale.remove()
