"""``cwcli rm`` on a STOPPED project - the transient start -> back up -> delete
path, real Docker only.

This is the exact path that had NO end-to-end coverage and that false-failed in
the field: a long-lived instance is deleted while STOPPED, so ``rm`` must
transiently start it, wait for MariaDB to accept connections (``_wait_for_db_ready``),
take a verified backup, and only then delete. The reported bug was that readiness
probe wrongly reporting "not ready" against a demonstrably healthy database, which
aborted the guarded backup-then-remove path and forced a manual dump + ``--no-backup``.

``cwcli axi rm`` REFUSES a stopped project on the volume path (it has no
auto-start), so this path is reachable only through the HUMAN ``cwcli rm`` verb -
which is why ``test_axi_rm_e2e.py`` (a RUNNING project) never exercised it.

The regression assertion is the whole point: on a stopped instance the guarded
path must SUCCEED - the probe waits for the cold-starting DB instead of giving up,
the backup lands verified, and the deletion is honest. A false-fail would instead
abort at non-zero, keep all data, and leave the instance stopped.

Provisions its OWN instances (``standalone``) because ``rm`` destroys them. Runs
once on the v16 leg (the transient-start backup gate does not vary by Frappe
major), mirroring ``test_axi_rm_e2e.py``.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

# `standalone`: builds its own instances, never touches the shared session one.
pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="the transient-start backup gate does not vary by Frappe major; runs once",
)

MARKER = "CWE2E-RM-STOPPED-MARKER"


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


def _seed_marker(project: str, site: str, bench: str, marker: str) -> None:
    kwargs = f'{{"doc": {{"doctype": "ToDo", "description": "{marker}", "status": "Open"}}}}'
    script = (
        f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} execute "
        f"frappe.client.insert --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"seeding the marker record failed: {out}"


def _count_marker(project: str, site: str, bench: str, marker: str) -> int:
    kwargs = f'{{"doctype": "ToDo", "filters": {{"description": "{marker}"}}}}'
    script = (
        f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} execute "
        f"frappe.client.get_count --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"reading the marker count back failed: {out}"
    stripped = out.strip()
    if not stripped:
        # bench's `execute` prints only a truthy return value, so a genuine zero
        # count prints nothing at all - not a failed read.
        return 0
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.lstrip("-").isdigit():
            return int(line)
    raise AssertionError(f"could not parse an integer count from bench execute output: {out!r}")


def _project_dir(project: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / project


def _archive_backup_dir(project: str, site: str) -> Path:
    archive_root = Path(os.environ["CWCLI_HOME"]) / "archive"
    matches = sorted(archive_root.glob(f"{project}_*"))
    assert matches, f"no archive directory for {project!r} found under {archive_root}"
    return matches[-1] / "backups" / site


def _stop_project(project: str) -> None:
    """Stop every container of the project so `rm` sees a STOPPED instance.

    Uses `docker stop` directly (there is no cwcli verb that stops the CONTAINERS -
    `cwcli stop` stops the in-container supervisor). This puts the instance in
    exactly the state that forces the transient-start-for-backup path.
    """
    ids = harness.project_containers(project)
    assert ids, f"no containers to stop for {project}"
    r = harness._docker("stop", *ids, timeout=180)
    assert r.returncode == 0, f"docker stop failed: {r.stdout}{r.stderr}"
    assert harness.frappe_container_id(project) is None, "frappe container is still running"


@v16_only
def test_rm_stopped_transiently_starts_backs_up_and_deletes(port_allocator):
    """A stopped instance is transiently started, backed up, and deleted - the
    guarded path the readiness-probe false-fail used to abort.

    1. Real instance, real seeded record read back before removal.
    2. Stop the instance (the trigger for the transient-start backup path).
    3. `cwcli rm <project> --yes` (human verb) succeeds - no probe false-fail.
    4. The deletion is honest: no containers, volumes, network, or dir survive.
    5. The archived backup genuinely restores into a FRESH instance, and the same
       record reads back through Frappe.
    """
    site = harness.DEFAULT_SITE
    bench = harness.DEFAULT_BENCH_PATH
    project = harness.project_name("rmstopped")

    try:
        init_result = _init(project, port_allocator.next())
        assert init_result.returncode == 0, init_result.stdout + init_result.stderr
        harness.wait_for_site_ready(project, site)

        # --- 1. seed real data, prove it landed before touching removal ---
        _seed_marker(project, site, bench, MARKER)
        assert _count_marker(project, site, bench, MARKER) == 1, "seeded marker did not land"

        # --- 2. stop the instance: the transient-start-for-backup trigger ---
        _stop_project(project)

        # --- 3. the guarded delete on a STOPPED project ---
        rm_result = harness.run_cwcli("rm", project, "--yes", "--verbose", timeout=1200)
        combined = rm_result.stdout + rm_result.stderr
        assert rm_result.returncode == 0, combined
        # The regression guard: the readiness probe must NOT have false-failed the
        # healthy-but-cold-starting DB and aborted the backup.
        assert "did not become ready" not in combined, combined

        # --- 4. the deletion is honest ---
        assert harness.frappe_container_id(project) is None, "a container survived removal"
        assert not harness.project_containers(project), "a container survived removal"
        assert not harness.project_volumes(project), "a named volume survived removal"
        assert not harness.project_networks(project), "the compose network survived removal"
        assert not _project_dir(project).exists(), "the project directory survived removal"

        # --- 5. the archived backup genuinely restores into a FRESH instance ---
        backups_dir = _archive_backup_dir(project, site)
        backup_files = sorted(backups_dir.glob("*"))
        assert backup_files, f"no backup artifacts found in {backups_dir}"
        sql_dumps = [f for f in backup_files if f.name.endswith(".sql.gz")]
        assert sql_dumps, f"no database dump in the archived backup: {backup_files}"
        assert all(f.stat().st_size > 0 for f in sql_dumps), "the archived database dump is empty"

        restore_project = harness.project_name("rmstoppedrestore")
        try:
            restore_init = _init(restore_project, port_allocator.next())
            assert restore_init.returncode == 0, restore_init.stdout + restore_init.stderr
            harness.wait_for_site_ready(restore_project, site)

            assert _count_marker(restore_project, site, bench, MARKER) == 0

            dest_backups = (
                _project_dir(restore_project)
                / "data"
                / "frappe-bench"
                / "sites"
                / site
                / "private"
                / "backups"
            )
            dest_backups.mkdir(parents=True, exist_ok=True)
            for f in backup_files:
                shutil.copy(f, dest_backups / f.name)

            restore_result = harness.run_cwcli(
                "restore",
                restore_project,
                "--site",
                site,
                "--latest",
                "--yes",
                "--mariadb-root-username",
                "root",
                "--mariadb-root-password",
                "123",
                timeout=900,
            )
            assert restore_result.returncode == 0, restore_result.stdout + restore_result.stderr
            harness.wait_for_site_ready(restore_project, site)

            # Read the SAME record back through Frappe - not a grep of the dump.
            assert _count_marker(restore_project, site, bench, MARKER) == 1
        finally:
            harness.cwcli_rm(restore_project)
    finally:
        # Tolerates the project already being gone (the success path deleted it).
        harness.cwcli_rm(project)
