"""Real non-root E2E: ``cwcli rm`` on a host whose uid is a system account in the image.

A shell runner whose own user is a system account (uid 998, gid 998) is the shape
the unusable-uid fallback exists for: on the ``frappe/bench`` image 998 is
``systemd-network``, so cwcli aligns the container ``frappe`` user to 1000 (keeping
gid 998) instead. The bench files on the bind-mounted workspace are then owned by
1000, which the runner user cannot delete, so ``cwcli rm`` must finish the project
directory through its root helper container once the backup gate has passed.

This runs the code under test AS uid 998 against a genuine Frappe instance: a
runner container (``test_root_host_cwcli_e2e``'s image, nothing built from this
checkout) imports cwcli straight from ``src/``, joins the Docker socket's group,
and mounts a world-writable home at the SAME absolute path, so the compose bind
mount resolves to the very directory the uid-998 process sees.

It proves, on one instance:

1. ``cwcli init`` as uid 998 says once that ``frappe`` was given uid 1000 instead
   of ``systemd-network``'s, and leaves ``frappe`` at 1000:998.
2. A seeded record lands in the site's database.
3. ``cwcli rm`` WITH its backup succeeds: the kept backup's database dump holds the
   record, and the named volumes and the project directory are gone.

Marked ``standalone`` (it builds its own instance) and v16-only: the uid choice is
version-agnostic, and one more bench build per leg buys nothing.
"""

from __future__ import annotations

import gzip
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import pytest

from . import harness
from .test_root_host_cwcli_e2e import ADMIN_PW, SRC, SYSTEM_UID, _runner_image

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="the frappe uid choice is version-agnostic; runs only on the v16 leg",
)

MARKER = "CWE2E-SYSTEM-UID-RM-MARKER"
DOCKER_SOCK = "/var/run/docker.sock"


def _as_system_user(
    image: str, home: Path, *args: str, timeout: int = 600
) -> subprocess.CompletedProcess:
    """``cwcli <args>`` as uid 998 (gid 998), against this machine's Docker."""
    sock_gid = os.stat(DOCKER_SOCK).st_gid
    cmd = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{SYSTEM_UID}:{SYSTEM_UID}",
        "--group-add",
        str(sock_gid),
        "-v",
        f"{DOCKER_SOCK}:{DOCKER_SOCK}",
        "-v",
        f"{home}:{home}",
        "-v",
        f"{SRC}:/cwcli-src:ro",
        "-e",
        f"HOME={home}",
        "-e",
        f"CWCLI_HOME={home / '.cwcli'}",
        "-e",
        "PYTHONPATH=/cwcli-src",
        "-e",
        "CWCLI_NO_UPDATE_CHECK=1",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "sh",
        image,
        "-c",
        'umask 000 && exec python -m caffeinated_whale_cli "$@"',
        "cwcli",
        *args,
    ]
    return subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


def _ok(r: subprocess.CompletedProcess, what: str) -> str:
    assert (
        r.returncode == 0
    ), f"{what} as uid {SYSTEM_UID} failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}"
    return r.stdout + r.stderr


def _seed_marker(project: str) -> None:
    kwargs = f'{{"doc": {{"doctype": "ToDo", "description": "{MARKER}", "status": "Open"}}}}'
    code, out = harness.exec_in_frappe(
        project,
        f"cd {shlex.quote(harness.DEFAULT_BENCH_PATH)} && bench --site "
        f"{shlex.quote(harness.DEFAULT_SITE)} execute frappe.client.insert --kwargs '{kwargs}'",
    )
    assert code == 0, f"seeding the marker record failed: {out}"


def _remove_as_root(image: str, home: Path) -> None:
    """Delete ``home``, whatever uid owns what is left in it (the frappe user, the
    Docker daemon, or the runner's 0700 cache dir)."""
    harness._docker(
        "run", "--rm", "-v", f"{home}:/target", "--entrypoint", "rm", image, "-rf", "/target"
    )
    subprocess.run(["rm", "-rf", str(home)], check=False)


@v16_only
def test_cwcli_as_a_system_account_uid_removes_its_instance_with_a_backup(
    isolated_home, port_allocator, monkeypatch
):
    harness.enforce_isolation()
    name = harness.project_name("sysuid")
    port = port_allocator.next()
    image = _runner_image()
    # Outside pytest's 0700 tmp tree, so uid 998 can reach it; world-writable,
    # because uid 998 cannot be given ownership of a dir by a non-root test.
    home = Path(tempfile.mkdtemp(prefix=f"{name}-home-", dir="/var/tmp"))
    home.chmod(0o777)
    project_dir = home / ".cwcli" / "projects" / name
    try:
        # 1. init + site creation as uid 998: frappe falls back to 1000:998.
        out = harness.collapse_ws(
            harness.strip_ansi(
                _ok(
                    _as_system_user(
                        image,
                        home,
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
        assert "systemd-network" in out, out
        harness.wait_for_site_ready(name, harness.DEFAULT_SITE)
        code, ident = harness.exec_in_frappe(name, 'echo "ids=$(id -u frappe):$(id -g frappe)"')
        assert code == 0 and f"ids=1000:{SYSTEM_UID}" in ident, ident
        assert (project_dir / "data" / harness.DEFAULT_BENCH_NAME).stat().st_uid == 1000

        # 2. real data in the site's database.
        _seed_marker(name)

        # 3. rm WITH its backup, as uid 998.
        _ok(_as_system_user(image, home, "rm", name, "--yes", timeout=1200), "rm")
        assert not harness.project_containers(name), "a container survived removal"
        assert not harness.project_volumes(name), "a named volume survived removal"
        assert not project_dir.exists(), "the project directory survived removal"

        monkeypatch.setenv("CWCLI_HOME", str(home / ".cwcli"))
        dumps = [
            f
            for f in harness.removal_backup_files(name, harness.DEFAULT_SITE)
            if f.name.endswith(".sql.gz")
        ]
        assert dumps, "no database dump in the kept backup"
        assert any(
            MARKER.encode() in gzip.decompress(f.read_bytes()) for f in dumps
        ), "the kept backup does not hold the seeded record"
    finally:
        _as_system_user(image, home, "rm", name, "--yes", "--volumes", "--no-backup")
        harness.sweep_cwe2e(only=name)
        _remove_as_root(image, home)
        harness._docker("rmi", "-f", image)
