"""The database root password given to init is the one MariaDB is created with.

Regression for the init failure where a non-default ``--db-root-password`` (or the
web wizard's "Database root password") reached only ``bench new-site``, while the
instance's MariaDB was created from the compose template's hardcoded
``MYSQL_ROOT_PASSWORD: 123``: ``new-site`` presented the chosen password, MariaDB
answered ``(1045, "Access denied for user 'root'...")``, and the error said only
"Command failed with exit code 1".

One real instance, real Docker only, walks the whole arc: init with a chosen
password succeeds and MariaDB accepts exactly that password (and refuses ``123``);
the value is in neither the compose file nor the MariaDB container's environment;
a backup works; a second site created WITHOUT the password succeeds on the
remembered one; ``rm-site`` drops it WITHOUT the password; and a wrong password
fails naming its cause instead of a bare exit code. Version-agnostic (the compose
secret and the 1045 do not vary by Frappe major), so it runs on the v16 leg only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="version-agnostic (MariaDB's root password does not vary by Frappe major); runs once",
)

CHOSEN = "Chosen-Root-Pw-7731"


def _mariadb_container(project: str) -> str:
    out = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=mariadb",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out, f"no mariadb container for {project}"
    return out[0]


def _root_login(container: str, password: str) -> subprocess.CompletedProcess:
    # The password rides the environment, never this test's argv.
    return subprocess.run(
        ["docker", "exec", "-e", f"MYSQL_PWD={password}", container]
        + ["mariadb", "-uroot", "-e", "select 1"],
        capture_output=True,
        text=True,
    )


def _site_exists(project: str, site: str) -> bool:
    code, _ = harness.exec_in_frappe(project, f"test -d {harness.DEFAULT_BENCH_PATH}/sites/{site}")
    return code == 0


@v16_only
def test_init_password_is_mariadbs_and_later_commands_remember_it(port_allocator, monkeypatch):
    project = harness.project_name("dbpw")
    site = harness.DEFAULT_SITE
    second = "second.localhost"
    monkeypatch.setenv("CWCLI_ADMIN_PASSWORD", SESSION_ADMIN_PW)
    try:
        monkeypatch.setenv("CWCLI_DB_ROOT_PASSWORD", CHOSEN)
        init = harness.run_cwcli(
            "axi",
            "init",
            project,
            "--port",
            str(port_allocator.next()),
            "--frappe-branch",
            harness.FRAPPE_BRANCH,
            "--site",
            site,
            "--no-start",
            timeout=harness.INIT_TIMEOUT,
        )
        # Before the fix this is the 1045 failure.
        assert init.returncode == 0, init.stdout + init.stderr
        harness.wait_for_site_ready(project, site)

        # MariaDB was created with exactly the chosen password.
        mariadb = _mariadb_container(project)
        assert _root_login(mariadb, CHOSEN).returncode == 0
        refused = _root_login(mariadb, "123")
        assert refused.returncode != 0 and "Access denied" in refused.stderr

        # The value stays out of the compose file and the container's environment.
        conf = Path(os.environ["CWCLI_HOME"]) / "projects" / project / "conf"
        assert CHOSEN not in (conf / "docker-compose.yml").read_text()
        assert (conf / "db-root-password").stat().st_mode & 0o777 == 0o600
        env = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", mariadb],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert CHOSEN not in env

        # Every later command runs WITHOUT the password.
        monkeypatch.delenv("CWCLI_DB_ROOT_PASSWORD")
        backup = harness.run_cwcli("axi", "backup", project, "--site", site, timeout=600)
        assert backup.returncode == 0, backup.stdout + backup.stderr

        added = harness.run_cwcli(
            "axi",
            "init",
            project,
            "--frappe-branch",
            harness.FRAPPE_BRANCH,
            "--reuse-bench",
            "--site",
            second,
            "--no-start",
            timeout=harness.INIT_TIMEOUT,
        )
        assert added.returncode == 0, added.stdout + added.stderr
        assert _site_exists(project, second)

        # A wrong password fails naming the cause, and drops nothing.
        wrong = harness.run_cwcli(
            "axi", "rm-site", project, second, "--yes", "--db-root-password", "wrong-pw"
        )
        assert wrong.returncode != 0
        assert "MariaDB rejected the password" in wrong.stdout, wrong.stdout
        assert _site_exists(project, second)

        dropped = harness.run_cwcli("axi", "rm-site", project, second, "--yes", timeout=600)
        assert dropped.returncode == 0, dropped.stdout + dropped.stderr
        assert not _site_exists(project, second)
    finally:
        harness.cwcli_rm(project)
