"""Real-root E2E: the actual ``cwcli`` run as uid 0, the GitLab shell-runner shape.

`test_root_host_init_e2e.py` proves the uid -> HOME mechanism against a small
stand-in container with a monkeypatched ``os.getuid``. This one runs the code
under test AS ROOT against a genuine Frappe instance, end to end: a runner
container (uid 0) shares this machine's Docker socket and mounts the isolated
session home at the SAME absolute path, so the compose bind mount
``../data:/workspace`` resolves to the very directory the root cwcli process sees -
exactly like a root CI runner driving Docker on its own host. The runner imports
cwcli straight from this checkout's ``src/`` (nothing is built or installed), with
only the runtime dependencies ``pyproject.toml`` declares.

It proves, on one instance:

1. ``cwcli init`` (bench + site) as root succeeds, says once that ``frappe`` was
   given uid 1000 instead of 0, and leaves ``frappe`` at uid 1000 with its own HOME
   and a workspace owned by 1000 on the host - not by root.
2. ``apps list`` and ``backup`` work as root against that instance.
3. The repair path: an instance an older cwcli left broken (``frappe`` remapped onto
   a system account in the image, ``systemd-network`` uid 998, with the bench owned
   by it - ``bench`` is then not even on PATH) is repaired by a plain
   ``cwcli start``: ``frappe`` back on 1000, the bench re-owned to it, and the site
   serving again. No data is removed.

Marked ``standalone`` (it builds its own instance) and v16-only: the uid choice is
version-agnostic, and one more bench build per leg buys nothing.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
import toml

from . import harness

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="the frappe uid choice is version-agnostic; runs only on the v16 leg",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
ADMIN_PW = "cwe2e-root-admin"
SYSTEM_UID = 998  # `systemd-network` in the frappe/bench image


def _runner_image() -> str:
    """A uid-0 Python image with the Docker CLI + compose plugin and cwcli's runtime
    dependencies (read from pyproject.toml, so the list can never drift)."""
    deps = toml.load(REPO_ROOT / "pyproject.toml")["project"]["dependencies"]
    tag = f"{harness.CWE2E_PREFIX}root-runner:{harness.RUN_ID}"
    dockerfile = "\n".join(
        [
            "FROM python:3.12-slim",
            # Static binaries from the official CLI image, so they run on any base.
            "COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker",
            "COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-compose "
            "/usr/local/libexec/docker/cli-plugins/docker-compose",
            "RUN pip install --no-cache-dir --root-user-action=ignore "
            + " ".join(shlex.quote(d) for d in deps),
        ]
    )
    r = subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert r.returncode == 0, f"runner image build failed:\n{r.stdout}\n{r.stderr}"
    return tag


def _as_root(image: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """``cwcli <args>`` as uid 0, against this machine's Docker, non-interactively."""
    home = os.environ["HOME"]
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{home}:{home}",
        "-v",
        f"{SRC}:/cwcli-src:ro",
        "-e",
        f"CWCLI_HOME={os.environ['CWCLI_HOME']}",
        "-e",
        "PYTHONPATH=/cwcli-src",
        "-e",
        "CWCLI_NO_UPDATE_CHECK=1",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        image,
        "python",
        "-m",
        "caffeinated_whale_cli",
        *args,
    ]
    return subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


def _frappe(project: str, script: str, user: str = "frappe") -> tuple[int, str]:
    cid = harness.frappe_container_id(project)
    assert cid, f"no frappe container for {project}"
    r = harness._docker("exec", "-u", user, cid, "bash", "-lc", script)
    return r.returncode, (r.stdout + r.stderr)


def _ok(r: subprocess.CompletedProcess, what: str) -> str:
    assert (
        r.returncode == 0
    ), f"{what} as root failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}"
    return r.stdout + r.stderr


@v16_only
def test_cwcli_as_root_builds_a_working_bench_and_repairs_a_system_account_owner(
    isolated_home, port_allocator
):
    harness.enforce_isolation()
    name = harness.project_name("root")
    port = port_allocator.next()
    bench_host = Path(os.environ["CWCLI_HOME"]) / "projects" / name / "data" / "frappe-bench"
    image = _runner_image()
    try:
        # 1. init + site creation as root.
        out = harness.collapse_ws(
            harness.strip_ansi(
                _ok(
                    _as_root(
                        image,
                        "init",
                        name,
                        "--port",
                        str(port),
                        "--frappe-branch",
                        harness.FRAPPE_BRANCH,
                        "--admin-password",
                        ADMIN_PW,
                        timeout=harness.INIT_TIMEOUT,
                    ),
                    "init",
                )
            )
        )
        assert "uid 0 (root)" in out, out
        harness.wait_for_site_ready(name, harness.DEFAULT_SITE)
        code, ident = _frappe(name, 'echo "$(id -u):$(id -g) HOME=$HOME"; command -v bench')
        assert code == 0 and "1000:1000 HOME=/home/frappe" in ident, ident
        st = bench_host.stat()
        assert (st.st_uid, st.st_gid) == (1000, 1000), "workspace must not be root-owned"

        # 2. apps and backup work as root.
        listed = _as_root(image, "apps", "list", name, "--json")
        _ok(listed, "apps list")
        assert "frappe" in json.loads(listed.stdout)["available_apps"], listed.stdout
        _ok(_as_root(image, "backup", name, "--site", harness.DEFAULT_SITE), "backup")

        # 3. Break it the way an older cwcli did - frappe onto a system account, the
        # bench owned by it - and prove `bench` is gone from frappe's PATH.
        # Stop the bench stack first, then bring back only the (stopped) frappe
        # container, so nothing runs as `frappe` while its files change owner.
        _ok(_as_root(image, "stop", name), "stop")
        stopped = harness._docker(
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={name}",
            "--filter",
            "label=com.docker.compose.service=frappe",
        ).stdout.split()
        assert stopped and harness._docker("start", stopped[0]).returncode == 0
        code, out = _frappe(
            name,
            f"chown -R {SYSTEM_UID}:{SYSTEM_UID} {harness.DEFAULT_BENCH_PATH} && "
            rf"sed -i 's/^frappe:\([^:]*\):[^:]*:/frappe:\1:{SYSTEM_UID}:/' /etc/passwd",
            user="root",
        )
        assert code == 0, out
        code, out = _frappe(name, "echo HOME=$HOME; command -v bench")
        assert code != 0 and "HOME=/" in out and "/home/frappe" not in out, out

        # ...then a plain `cwcli start` as root repairs it in place.
        _ok(_as_root(image, "start", name), "start")
        code, ident = _frappe(name, 'echo "$(id -u) HOME=$HOME"; command -v bench')
        assert code == 0 and "1000 HOME=/home/frappe" in ident, ident
        code, owners = _frappe(
            name,
            f"find {harness.DEFAULT_BENCH_PATH} -not -uid 1000 -print -quit",
            user="root",
        )
        assert code == 0 and owners.strip() == "", f"left owned by another uid: {owners}"
        harness.wait_for_site_ready(name, harness.DEFAULT_SITE)
        assert bench_host.stat().st_uid == 1000
    finally:
        _as_root(image, "rm", name, "--yes", "--volumes", "--no-backup")
        harness.sweep_cwe2e(only=name)
        harness._docker("rmi", "-f", image)
