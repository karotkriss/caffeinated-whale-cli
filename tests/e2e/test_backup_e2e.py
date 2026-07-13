"""§4.2 backup E2E - the first destructive-command real-side-effect proof.

Asserts a real, non-empty DB dump actually lands on the host (copied out of the
container), not a string match, in BOTH non-interactive (flags, stdin closed)
and interactive (pty) modes. This proves the whole pattern end to end: the
session fixture stands the instance up, backup produces a genuine artifact, and
`cwcli rm` tears it down.
"""

from __future__ import annotations

import shlex

import pytest

from . import harness

pytestmark = pytest.mark.e2e


def _newest_dump_path(inst) -> str | None:
    """Full container path of the newest DB dump (*.sql.gz) for the site, or None."""
    backups = f"{inst.bench}/sites/{inst.site}/private/backups"
    code, out = harness.exec_in_frappe(
        inst.name, f"ls -1t {shlex.quote(backups)}/*.sql.gz 2>/dev/null | head -1"
    )
    path = out.strip()
    return path if code == 0 and path else None


def _assert_real_dump_on_host(inst, tmp_path, fname):
    """The core real-side-effect assertion: the newest dump copies out of the
    container and is non-empty on the host filesystem."""
    dump = _newest_dump_path(inst)
    assert dump, "no *.sql.gz dump found in the container's backups dir"
    host_file = tmp_path / fname
    assert harness.docker_cp_out(inst.name, dump, host_file), f"docker cp failed for {dump}"
    assert host_file.stat().st_size > 0, "DB dump copied to the host is empty"


def test_backup_noninteractive_creates_real_dump(running_instance, tmp_path):
    inst = running_instance
    result = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Successfully created backup" in result.stdout
    _assert_real_dump_on_host(inst, tmp_path, "noninteractive.sql.gz")


def test_backup_interactive_creates_real_dump(running_instance, tmp_path):
    import pexpect

    inst = running_instance
    child = harness.spawn_cwcli(["backup", inst.name, "--site", inst.site], timeout=900)
    try:
        # The instance is running, so backup runs straight through with no prompt;
        # this exercises the real TTY path and asserts the genuine artifact.
        child.expect("Successfully created backup", timeout=900)
        child.expect(pexpect.EOF, timeout=120)
    finally:
        child.close(force=True)
    _assert_real_dump_on_host(inst, tmp_path, "interactive.sql.gz")
