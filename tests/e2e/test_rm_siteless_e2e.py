"""``cwcli rm`` on a site-less (half-provisioned) instance - real Docker only.

Issue #237: an instance whose containers are up but whose bench never finished
provisioning (no ``sites/`` directory, so no site exists) has nothing to back
up, yet the backup gate refused removal and forced a second run with
``--no-backup``. The refusal fired because ``_backup_sites`` returns False when a
bench's ``sites`` directory cannot be listed - which is exactly the
half-provisioned state, not a real backup failure.

The fix proves the site inventory LIVE from the instance before the gate refuses
(``core.rm._live_site_census``) and relaxes the gate only on a positive proof of
zero sites - the same "recheck rather than blindly refuse" the volume-free
orphan path already does. This test reproduces the exact old-gate-refusal state
(a running instance whose ``sites/`` directory is gone) and asserts ``cwcli rm``
now PROCEEDS in one run, deleting honestly, with no ``--no-backup`` detour.

The safety twin - a real site whose backup fails STILL blocks deletion - is
covered by ``test_rm_stopped_backup_e2e.py`` / ``test_axi_rm_e2e.py`` and the
unit gate (``tests/test_rm_safety.py::TestSiteLessBackupGate``); this leg only
needs to prove the RELAX half end to end.

Provisions its OWN instance (``standalone``) because ``rm`` destroys it. Runs
once on the v16 leg (the census does not vary by Frappe major).
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

# `standalone`: builds its own instance, never touches the shared session one.
pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="the live site census does not vary by Frappe major; runs once",
)


def _init(name: str, port: int):
    return harness.run_cwcli(
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


def _project_dir(project: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / project


@v16_only
def test_rm_siteless_instance_proceeds_without_no_backup(port_allocator):
    """A running instance with no sites is removed in ONE run, no --no-backup.

    1. Real instance, then make it site-less by deleting the bench's sites/ dir -
       the exact state (`ls sites` fails -> list_sites None) that used to refuse.
    2. `cwcli rm <project> --yes` succeeds: the live census proves zero sites and
       relaxes the gate, so removal proceeds instead of refusing.
    3. The deletion is honest: no containers, volumes, network, or dir survive.
    """
    site = harness.DEFAULT_SITE
    bench = harness.DEFAULT_BENCH_PATH
    project = harness.project_name("rmsiteless")

    try:
        init_result = _init(project, port_allocator.next())
        assert init_result.returncode == 0, init_result.stdout + init_result.stderr
        harness.wait_for_site_ready(project, site)

        # --- 1. reproduce the half-provisioned state: no sites/ directory. This
        #        is what makes list_sites return None and the OLD gate refuse. ---
        code, out = harness.exec_in_frappe(project, f"rm -rf {shlex.quote(bench)}/sites")
        assert code == 0, f"could not remove the sites directory: {out}"
        code, out = harness.exec_in_frappe(project, f"ls -d {shlex.quote(bench)}/sites")
        assert code != 0, f"sites directory still present after removal: {out}"
        assert harness.frappe_container_id(project) is not None, "frappe container is not running"

        # --- 2. the guarded delete now PROCEEDS in one run (no --no-backup) ---
        rm_result = harness.run_cwcli("rm", project, "--yes", "--verbose", timeout=1200)
        combined = rm_result.stdout + rm_result.stderr
        # Collapse whitespace: rich wraps console lines at ~80 cols in a non-TTY,
        # so a phrase can straddle a newline in the raw capture.
        flat = " ".join(combined.split())
        assert rm_result.returncode == 0, combined
        # The regression guard: the gate did NOT refuse a proven site-less instance.
        assert "a verified database backup could not be created" not in flat, combined
        # The census-relax path fired (proven zero sites, nothing to back up).
        assert "nothing to back up" in flat, combined

        # --- 3. the deletion is honest ---
        assert harness.frappe_container_id(project) is None, "a container survived removal"
        assert not harness.project_containers(project), "a container survived removal"
        assert not harness.project_volumes(project), "a named volume survived removal"
        assert not harness.project_networks(project), "the compose network survived removal"
        assert not _project_dir(project).exists(), "the project directory survived removal"
    finally:
        # Tolerates the project already being gone (the success path deleted it).
        harness.cwcli_rm(project)
