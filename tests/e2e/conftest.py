"""E2E fixtures: isolation, the safety backstop, and the shared real instance.

All Docker/rail work happens inside these fixtures (never at import), so
collecting the e2e modules under ``-m unit`` (which deselects them) stays cheap
and Docker-free.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

import pytest

from . import harness

# A fixed, non-secret throwaway admin password for the non-interactive session
# instance (the interactive test covers the GENERATED-password path separately).
SESSION_ADMIN_PW = "CwE2ETestAdmin-123"


@dataclass
class Instance:
    name: str
    site: str
    bench: str
    port: int
    init_stdout: str
    init_stderr: str


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    """Keep the `standalone` marker honest against the fixtures a test requests.

    e2e.yml splits each version leg into two matrix jobs on this marker, and the
    split only pays off while the marker means what it says: a `standalone` test
    must never reach the shared session instance, and a test that does reach it
    must not be marked. Drift in either direction is silent - the tests still
    pass, the job just does the wrong work - so it is caught here, at collection,
    in whichever group collected the offending test. Scoped to `e2e` items; the
    `e2e_pkg` leg is not split and carries no marker.
    """
    for item in items:
        if item.get_closest_marker("e2e") is None:
            continue
        marked = item.get_closest_marker("standalone") is not None
        shared = bool({"session_instance", "running_instance"} & set(item.fixturenames))
        if marked and shared:
            raise pytest.UsageError(
                f"{item.nodeid} is marked `standalone` but requests the shared "
                "session instance. Drop the marker, or stop using it."
            )
        if not marked and not shared:
            raise pytest.UsageError(
                f"{item.nodeid} touches no shared session instance, so it belongs "
                "in the standalone group: add `@pytest.mark.standalone`."
            )


@pytest.fixture(scope="session", autouse=True)
def _docker_gate():
    """SKIP LOUDLY when no Docker daemon is reachable - an infra failure, never
    a silent green pass."""
    if not harness.docker_available():
        pytest.skip(
            "DOCKER UNAVAILABLE - no reachable daemon (`docker info` failed). "
            "This is an INFRASTRUCTURE problem, not a passing E2E run; the "
            "real-Docker E2E tier cannot execute here."
        )


@pytest.fixture(scope="session", autouse=True)
def isolated_home(tmp_path_factory):
    """Per-session temp HOME + CWCLI_HOME so ~/.cwcli is never touched, then the
    hard isolation rail. Restores the environment on teardown."""
    tmp_home = tmp_path_factory.mktemp("cwe2e-home")
    cwcli_home = tmp_home / ".cwcli"
    saved = {k: os.environ.get(k) for k in ("HOME", "CWCLI_HOME")}
    os.environ["HOME"] = str(tmp_home)
    os.environ["CWCLI_HOME"] = str(cwcli_home)
    # `cwcli init` bind-mounts $CWCLI_HOME/projects/<name> into the frappe
    # container as /workspace, and `bench init` writes there as the container's
    # `frappe` user (UID 1000). When the HOST user's UID differs - GitHub-hosted
    # runners run as UID 1001 - a default-umask 0755 workspace is not writable by
    # frappe, so `bench init` fails immediately. Relax the umask (inherited by the
    # cwcli subprocess) so the project/workspace dirs are created world-writable
    # and the container can write regardless of host UID. cwcli's cache dir keeps
    # its explicit 0700 mode (umask only removes bits, never adds), so no secret
    # dir is loosened. Local runs whose host UID is already 1000 are unaffected.
    saved_umask = os.umask(0o000)
    harness.enforce_isolation()  # fail closed before any instance work
    try:
        yield tmp_home
    finally:
        os.umask(saved_umask)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # The Docker daemon (root) can leave root-owned paths inside the bind-mounted
        # CWCLI_HOME (e.g. a compose-created working_dir), which this non-root rmtree
        # cannot remove - and tmp_home lives under a shared temp home, so the leak
        # re-breaks later pytest runs. Reclaim ownership (scoped to tmp_home) first.
        harness.reclaim_root_owned(tmp_home)
        shutil.rmtree(tmp_home, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _teardown_backstop(_docker_gate):
    """Unconditional sweep of every cwe2e- compose project on session teardown,
    so a crashed test (whose `cwcli rm` never fired) cannot leak."""
    yield
    harness.sweep_cwe2e()


@pytest.fixture(scope="session")
def port_allocator():
    return harness.PortAllocator()


@pytest.fixture(scope="session")
def session_instance(isolated_home, _docker_gate, _teardown_backstop, port_allocator):
    """One real throwaway instance for the whole session: a genuine `cwcli init`
    (bench init + new-site), torn down with `cwcli rm --yes --volumes`.

    Stood up NON-INTERACTIVELY (stdin closed, `--admin-password` supplied), so it
    doubles as the non-interactive init proof; the interactive/generated-password
    path is a separate dedicated test.
    """
    harness.enforce_isolation()
    name = harness.project_name("main")
    port = port_allocator.next()
    result = harness.run_cwcli(
        "init",
        name,
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
        "--auto-start",
        timeout=harness.INIT_TIMEOUT,
    )
    if result.returncode != 0:
        pytest.fail(
            f"`cwcli init {name}` (frappe {harness.FRAPPE_BRANCH}) failed "
            f"(exit {result.returncode}). If the preflight upstream check passed, "
            f"this is a cwcli regression.\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    # Real readiness: Frappe boots and reaches its DB for the site.
    harness.wait_for_site_ready(name, harness.DEFAULT_SITE)
    inst = Instance(
        name=name,
        site=harness.DEFAULT_SITE,
        bench=harness.DEFAULT_BENCH_PATH,
        port=port,
        init_stdout=result.stdout,
        init_stderr=result.stderr,
    )
    try:
        yield inst
    finally:
        harness.cwcli_rm(name)


@pytest.fixture()
def running_instance(session_instance):
    """The session instance, guaranteed to have a running frappe container."""
    if harness.frappe_container_id(session_instance.name) is None:
        harness.run_cwcli("start", session_instance.name, "--yes")
        harness.wait_for_site_ready(session_instance.name, session_instance.site)
    return session_instance
