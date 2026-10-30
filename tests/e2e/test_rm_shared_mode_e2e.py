"""Committed E2E: a shared-mode member's ``cwcli rm --yes`` removes the whole project.

In shared mode the container ``frappe`` user is aligned to the ``cwcli`` SERVICE
uid, so every bench file under ``<state>/projects/<p>/data`` is service-owned
(``drwxr-sr-x cwcli:cwcli``). A member running ``cwcli rm`` removed the
containers, volume and network, then failed ``rmtree`` on that bench dir with
``[Errno 13] Permission denied`` and exited 1 with the removal half done.

This builds the real layout with REAL distinct uids inside ONE throwaway
``python:3.12-slim`` sandbox that drives the host Docker daemon through its socket:
``core.setup.provision`` creates the group, service account and state tree; a
project container runs as the service uid and writes the bench; then the member
runs the real ``cwcli rm <p> --yes --no-backup`` and every piece must be gone.
``--no-backup`` because the stand-in container has no bench to dump; the backup
gate is unchanged by this fix and covered elsewhere.

The state tree lives in a host scratch dir mounted at the SAME path in the
sandbox, so the bind-mount paths cwcli hands the host daemon resolve to the same
files. No Frappe instance, no host root setup and no real ``~/.cwcli`` is touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

_IMAGE = "python:3.12-slim"
_SOCK = "/var/run/docker.sock"

_SCRIPT = r"""
set -e
cp -r /src /build
rm -rf /build/.venv /build/build /build/*.egg-info /build/webui/node_modules 2>/dev/null || true
pip install -q /build 2>&1 | tail -1

useradd -m -s /bin/bash alice
# The member reaches Docker the way a real member does: through the socket's group
# (read in here: Docker Desktop's socket has another owner inside containers).
SOCK_GID=$(stat -c %g /var/run/docker.sock)
SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
if [ -z "$SOCK_GROUP" ]; then groupadd -g "$SOCK_GID" dockerhost; SOCK_GROUP=dockerhost; fi
usermod -aG "$SOCK_GROUP" alice

python - <<'PY'
import os
from pathlib import Path
from caffeinated_whale_cli.core import setup
setup.provision(state_dir=Path(os.environ["STATE"]), users=["alice"])
PY

PDIR="$STATE/projects/$P"
# The member creates the project dir as `cwcli init` does (umask 0007, setgid tree).
su alice -c "umask 0007; mkdir -p '$PDIR/conf' '$PDIR/data' && echo 'services: {}' > '$PDIR/conf/docker-compose.yml'"

# The instance: a frappe-service container running as the SERVICE uid writes the
# bench into the bind-mounted data dir, plus the project's named volume + network.
python - <<'PY'
import os, pwd, grp, time
import docker
p = os.environ["P"]
svc = pwd.getpwnam("cwcli").pw_uid
gid = grp.getgrnam("cwcli").gr_gid
labels = {"com.docker.compose.project": p}
c = docker.from_env()
c.volumes.create(f"{p}_db-data", labels=labels)
c.networks.create(f"{p}_default", labels={**labels, "com.docker.compose.network": "default"})
c.containers.run(
    os.environ["IMAGE"],
    ["sh", "-c", "umask 022; mkdir -p /workspace/frappe-bench/sites/site1 "
     "/workspace/frappe-bench/apps/frappe && echo '{}' > "
     "/workspace/frappe-bench/sites/site1/site_config.json && exec sleep infinity"],
    name=f"{p}-frappe-1", detach=True, init=True, user=f"{svc}:{gid}",
    labels={**labels, "com.docker.compose.service": "frappe"},
    volumes={os.environ["STATE"] + f"/projects/{p}/data": {"bind": "/workspace", "mode": "rw"}},
    network=f"{p}_default",
)
bench = os.environ["STATE"] + f"/projects/{p}/data/frappe-bench/sites/site1/site_config.json"
for _ in range(60):
    if os.path.exists(bench):
        break
    time.sleep(0.5)
st = os.stat(os.path.dirname(os.path.dirname(bench)))
print(f"BENCH_OWNER={st.st_uid}:{st.st_gid} SVC={svc}:{gid}", flush=True)
PY

set +e
su - alice -c "cwcli rm '$P' --yes --no-backup" > /tmp/rm.out 2>&1
echo "RM_EXIT=$?"
set -e
cat /tmp/rm.out

python - <<'PY'
import os
import docker
p = os.environ["P"]
c = docker.from_env()
flt = {"label": f"com.docker.compose.project={p}"}
print("LEFT_CONTAINERS=%d" % len(c.containers.list(all=True, filters=flt)))
print("LEFT_VOLUMES=%d" % len(c.volumes.list(filters=flt)))
print("LEFT_NETWORKS=%d" % len(c.networks.list(filters=flt)))
print("LEFT_DIR=%s" % os.path.exists(os.environ["STATE"] + f"/projects/{p}"))
PY
echo DONE
"""


# Shadows the session sweep of every cwe2e- project: this test removes exactly
# its own project in ``scratch``, and a sweep would also kill a parallel run's.
@pytest.fixture(scope="module", autouse=True)
def _teardown_backstop():
    yield


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture
def scratch():
    # /var/tmp, not /tmp: the scratch path is mounted into containers by the host
    # daemon, and must be world-traversable for the sandbox's non-root member.
    root = Path(tempfile.mkdtemp(prefix="cwrmshared-", dir="/var/tmp"))
    root.chmod(0o755)
    project = f"cwe2e-rmsh{uuid.uuid4().hex[:6]}"
    try:
        yield root, project
    finally:
        flt = f"label=com.docker.compose.project={project}"
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", flt], capture_output=True, text=True
        ).stdout.split()
        if ids:
            _docker("rm", "-f", *ids)
        _docker("volume", "rm", "-f", f"{project}_db-data")
        _docker("network", "rm", f"{project}_default")
        # The tree holds files owned by the sandbox's uids; remove it as root.
        _docker("run", "--rm", "-v", f"{root}:/scratch", _IMAGE, "rm", "-rf", "/scratch")
        subprocess.run(["rm", "-rf", str(root)], capture_output=True)


@pytest.mark.skipif(
    not os.path.exists(_SOCK) or not shutil.which("docker"),
    reason="needs the local Docker socket and CLI",
)
def test_shared_mode_member_rm_removes_the_service_owned_project_dir(scratch):
    root, project = scratch
    state = root / "state"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(_SCRIPT)
        script_path = f.name

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{_repo_root()}:/src:ro",
            "-v",
            f"{script_path}:/smoke.sh:ro",
            "-v",
            f"{_SOCK}:{_SOCK}",
            # cwcli's frontend refuses without a docker CLI on PATH, as a member has.
            "-v",
            f"{os.path.realpath(shutil.which('docker') or 'docker')}:/usr/local/bin/docker:ro",
            "-v",
            f"{root}:{root}",
            "-e",
            f"STATE={state}",
            "-e",
            f"P={project}",
            "-e",
            f"IMAGE={_IMAGE}",
            _IMAGE,
            "bash",
            "/smoke.sh",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    out = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0 and "DONE" in out, f"sandbox script failed:\n{out}"

    # The precondition the bug needs: the bench is owned by the service uid.
    owner_line = next(line for line in out.splitlines() if line.startswith("BENCH_OWNER="))
    owner, svc = owner_line.removeprefix("BENCH_OWNER=").split(" SVC=")
    assert owner == svc, out

    assert "Permission denied" not in out, out
    assert "RM_EXIT=0" in out, out
    assert "LEFT_CONTAINERS=0" in out, out
    assert "LEFT_VOLUMES=0" in out, out
    assert "LEFT_NETWORKS=0" in out, out
    assert "LEFT_DIR=False" in out, out
