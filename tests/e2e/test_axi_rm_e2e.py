"""``cwcli axi rm`` E2E - the full destructive-delete lifecycle, real Docker only.

Twenty e2e files existed before this one and none of them exercised removal:
every other command's teardown calls ``harness.cwcli_rm`` (the human verb, with
``--no-backup`` - a throwaway teardown does not need a live backup), so the
destructive path this branch's ``add-axi-rm-verb`` adds - the fail-closed C1
backup gate, the verified per-bench copy-out, and the honest deletion - had only
ever been proven by a single hand-run session (``docs/e2e/
axi-rm-destructive-lifecycle.md``). This is the permanent, CI-enforced net for
that gap: it drives the real ``cwcli`` binary against genuine throwaway
instances with no mocked delete anywhere.

Provisions its OWN two dedicated instances rather than the shared
``session_instance``/``running_instance`` fixtures every other e2e file relies
on - ``rm`` DESTROYS the instance, and that session fixture is shared by every
other test in the run. One test function walks the whole lifecycle (seed on
instance A, ``cwcli axi rm A --yes``, confirm the deletion is honest, restore
the archived backup into a fresh instance B, read the seeded record back
through Frappe) rather than splitting it across functions that would each pay
for their own ~10-20 minute provisioning. Version-agnostic (the C1 gate and the
copy-out mechanics do not vary by Frappe major), so it runs once on the v16 leg
only, mirroring the existing ``v16_only`` precedent in ``test_init_e2e.py``.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = pytest.mark.e2e

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="version-agnostic (the C1 backup gate does not vary by Frappe major); runs once",
)

MARKER = "CWE2E-RM-MARKER"


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
    """Insert a real ToDo via `bench execute frappe.client.insert` (not SQL)."""
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
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.lstrip("-").isdigit():
            return int(line)
    raise AssertionError(f"could not parse an integer count from bench execute output: {out!r}")


def _toon_int(stdout: str, key: str) -> int:
    for line in stdout.splitlines():
        if line.startswith(f"{key}:"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError(f"TOON output has no {key!r} line:\n{stdout}")


def _project_dir(project: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / project


def _archive_backup_dir(project: str, site: str) -> Path:
    archive_root = Path(os.environ["CWCLI_HOME"]) / "archive"
    matches = sorted(archive_root.glob(f"{project}_*"))
    assert matches, f"no archive directory for {project!r} found under {archive_root}"
    return matches[-1] / "backups" / site


@v16_only
def test_axi_rm_deletes_honestly_and_the_backup_genuinely_restores(port_allocator):
    """The full arc, real Docker only:

    1. A real instance, provisioned through the real cwcli binary.
    2. A real seeded record, read back through Frappe BEFORE removal (so a
       later failure cannot be explained by the seed never landing).
    3. `cwcli axi rm <project> --yes` - the verb this change adds - asserted on
       its exit code and its TOON outcome fields.
    4. The deletion is honest: no containers, no named volumes, no project
       directory left for the project.
    5. The archived backup genuinely restores: copied into a FRESH second
       instance's bench, restored with `cwcli restore`, and the same record is
       read back through Frappe (not grepped out of the dump).
    """
    site = harness.DEFAULT_SITE
    bench = harness.DEFAULT_BENCH_PATH
    project = harness.project_name("rm")

    try:
        init_result = _init(project, port_allocator.next())
        assert init_result.returncode == 0, init_result.stdout + init_result.stderr
        harness.wait_for_site_ready(project, site)

        # --- 2. seed real data, prove it landed before touching removal ---
        _seed_marker(project, site, bench, MARKER)
        assert _count_marker(project, site, bench, MARKER) == 1, "seeded marker did not land"

        # --- 3. the destructive verb itself ---
        rm_result = harness.run_cwcli("axi", "rm", project, "--yes")
        assert rm_result.returncode == 0, rm_result.stdout + rm_result.stderr
        stdout = rm_result.stdout
        assert f"project: {project}" in stdout, stdout
        assert "found: true" in stdout, stdout
        assert "orphan: false" in stdout, stdout
        assert "dir_removed: true" in stdout, stdout
        assert "backup_ok: true" in stdout, stdout
        assert "failures[0]:" in stdout, stdout  # empty failures list, never omitted
        assert _toon_int(stdout, "containers_removed") > 0, stdout
        assert _toon_int(stdout, "volumes_removed") > 0, stdout

        # --- 4. the deletion is honest ---
        # Containers, named volumes, and the project directory - what
        # `core.remove` actually promises to remove (`containers_removed`/
        # `volumes_removed`/`dir_removed` above). The project's compose NETWORK
        # is deliberately excluded here: cwcli never removes it (no code path in
        # src/ touches a network), on either the human or the axi surface, and
        # that is a pre-existing, accepted fact of this codebase - the harness's
        # own unconditional session-end `sweep_cwe2e()` backstop exists precisely
        # because the network outlives a clean `cwcli rm`. Asserting it away here
        # would fail every rm regardless of this branch's changes.
        assert harness.frappe_container_id(project) is None, "a container survived removal"
        assert not harness.project_containers(project), "a container survived removal"
        assert not harness.project_volumes(project), "a named volume survived removal"
        assert not _project_dir(project).exists(), "the project directory survived removal"

        # --- 5. the backup genuinely restores, into a FRESH second instance ---
        backups_dir = _archive_backup_dir(project, site)
        backup_files = sorted(backups_dir.glob("*"))
        assert backup_files, f"no backup artifacts found in {backups_dir}"
        sql_dumps = [f for f in backup_files if f.name.endswith(".sql.gz")]
        assert sql_dumps, f"no database dump in the archived backup: {backup_files}"
        assert all(f.stat().st_size > 0 for f in sql_dumps), "the archived database dump is empty"

        restore_project = harness.project_name("rmrestore")
        try:
            restore_init = _init(restore_project, port_allocator.next())
            assert restore_init.returncode == 0, restore_init.stdout + restore_init.stderr
            harness.wait_for_site_ready(restore_project, site)

            # Sanity: the fresh instance genuinely does not have the marker yet.
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
        # Tolerates the project already being gone (the success path deleted it
        # itself); `core.remove` reports a genuinely-absent project as an exit-0
        # no-op, so this is also the correct cleanup for a partway failure.
        harness.cwcli_rm(project)
