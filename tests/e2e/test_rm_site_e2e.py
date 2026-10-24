"""`rm-site` / `axi rm-site` E2E - both modes, against a real instance.

`rm-site` is cwcli's newest destructive, prompting command (a `--yes`-gated
confirm, like `apps uninstall`), so the captain standard requires it proven in
BOTH interactive and non-interactive modes, plus its `axi` sibling. Every
assertion here is a real outcome read back out of the container or the host
filesystem (the site's directory genuinely gone, its database genuinely
dropped, its credential-bearing archive genuinely relocated and pruned - or
genuinely left in place when it could not be verified), never a string match
on cwcli's own claims alone.

Each test creates its OWN throwaway site on the shared `running_instance`
bench and drops only that site, never `inst.site` (the shared instance's own
default site, which every other e2e test in the session still depends on).

The backup gate is proven the way `cwcli rm`'s is: a seeded record is found in
the host archive's database dump and genuinely restores, and a forced backup
failure (an unwritable backups directory) drops NOTHING - the regression guard
for `bench drop-site --force` silently dropping a database whose backup failed.
"""

from __future__ import annotations

import gzip
import json
import os
import shlex
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = pytest.mark.e2e

MARKER = "CWE2E-RMSITE-MARKER"


def _create_site(inst, site: str) -> None:
    """A real `bench new-site`, both secrets supplied so it never prompts."""
    result = harness.run_cwcli(
        "run",
        inst.name,
        "-i",
        "new-site",
        site,
        "--db-root-password",
        "123",
        "--admin-password",
        SESSION_ADMIN_PW,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _site_exists(inst, site), f"new-site did not create {site}"


def _site_exists(inst, site: str) -> bool:
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {inst.bench}/sites/{shlex.quote(site)}")
    return code == 0


def _archived_path(inst, site: str) -> str:
    return f"{inst.bench}/archived/sites/{site}"


def _archived_exists(inst, site: str) -> bool:
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {_archived_path(inst, site)}")
    return code == 0


def _bench_execute(inst, site: str, method: str, kwargs: str) -> str:
    script = (
        f"cd {shlex.quote(inst.bench)} && bench --site {shlex.quote(site)} execute "
        f"{method} --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(inst.name, script)
    assert code == 0, f"bench execute {method} failed: {out}"
    return out


def _seed_marker(inst, site: str) -> None:
    """Insert a real ToDo through Frappe (not SQL), the rm/rm-bench E2E pattern."""
    doc = f'{{"doc": {{"doctype": "ToDo", "description": "{MARKER}", "status": "Open"}}}}'
    _bench_execute(inst, site, "frappe.client.insert", doc)


def _count_marker(inst, site: str) -> int:
    filters = f'{{"doctype": "ToDo", "filters": {{"description": "{MARKER}"}}}}'
    out = _bench_execute(inst, site, "frappe.client.get_count", filters).strip()
    if not out:
        # bench's `execute` only prints a truthy return, so a genuine 0 prints nothing.
        return 0
    for line in reversed(out.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    raise AssertionError(f"could not parse an integer count from: {out!r}")


def _db_name(inst, site: str) -> str:
    code, out = harness.exec_in_frappe(
        inst.name, f"cat {inst.bench}/sites/{shlex.quote(site)}/site_config.json"
    )
    assert code == 0, out
    return json.loads(out)["db_name"]


def _database_exists(inst, db_name: str) -> bool:
    code, out = harness.exec_in_frappe(
        inst.name, f"mysql -h mariadb -uroot -p123 -N -e \"SHOW DATABASES LIKE '{db_name}'\""
    )
    assert code == 0, f"could not query MariaDB: {out}"
    return db_name in out


def _dropped_site_dirs(inst, site: str) -> list[Path]:
    root = Path(os.environ["CWCLI_HOME"]) / "archive" / f"{inst.name}_dropped_sites"
    return sorted(root.glob(f"{site}_*")) if root.exists() else []


def _host_site_backups(inst, site: str) -> Path:
    """The site's `private/backups` on the host, through the workspace bind mount."""
    root = Path(os.environ["CWCLI_HOME"]) / "projects" / inst.name
    bench_name = inst.bench.rstrip("/").rsplit("/", 1)[-1]
    for bench_dir in (root / "data" / bench_name, root / bench_name):
        if bench_dir.exists():
            return bench_dir / "sites" / site / "private" / "backups"
    raise AssertionError(f"no host bench directory for {inst.name} under {root}")


def _drop_temporary_site(inst, site: str) -> None:
    """Best-effort teardown of a refusal-path fixture on the shared session bench.

    Bounded (a real drop is seconds) and non-fatal on timeout: this is teardown
    of a throwaway site no later test depends on, and ``bench drop-site``'s
    ``DROP DATABASE`` waits (up to MariaDB's one-day ``lock_wait_timeout``) on any
    session still holding the site's tables - it once hung on the orphaned
    installer a non-TTY ``run -i new-site`` left behind - so a hang here must never
    fail a test whose own assertions already passed.
    """
    if not _site_exists(inst, site):
        return
    try:
        result = harness.run_cwcli("rm-site", inst.name, site, "--yes", timeout=300)
    except subprocess.TimeoutExpired:
        warnings.warn(f"teardown of {site} timed out; leaving it for session sweep", stacklevel=2)
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _site_exists(inst, site), f"temporary site {site} survived cleanup"


def test_rm_site_noninteractive_really_drops_the_site(running_instance):
    """Flags supplied + stdin closed: no prompt, exit 0, and the site is GENUINELY
    gone - its database dropped, its directory gone, and its credential-bearing
    archive relocated out of the container and pruned, not left to accumulate."""
    inst = running_instance
    site = "cwe2e-dropsite-a.localhost"
    _create_site(inst, site)

    result = harness.run_cwcli("rm-site", inst.name, site, "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _site_exists(inst, site), "site directory still present after rm-site"
    assert not _archived_exists(inst, site), "bench's own archive was not pruned from the container"
    out = harness.collapse_ws(harness.strip_ansi(result.stdout))
    assert "dropped" in out.lower(), out
    assert "removed from the container" in out, out

    # The shared instance's OWN default site is untouched.
    assert _site_exists(inst, inst.site), "the shared instance's own site was affected"


def test_rm_site_without_yes_refuses_noninteractively(running_instance):
    """A non-TTY without --yes must refuse, never silently drop the site."""
    inst = running_instance
    site = "cwe2e-dropsite-b.localhost"
    _create_site(inst, site)

    try:
        result = harness.run_cwcli("rm-site", inst.name, site)

        assert result.returncode != 0, result.stdout + result.stderr
        assert _site_exists(inst, site), "site was dropped despite refusing the confirmation"
    finally:
        _drop_temporary_site(inst, site)


def test_rm_site_of_a_nonexistent_site_is_not_found(running_instance):
    """A typo'd site name is a clear refusal, not a silent no-op or a crash."""
    inst = running_instance

    result = harness.run_cwcli("rm-site", inst.name, "cwe2e-dropsite-nope.localhost", "--yes")

    assert result.returncode != 0, result.stdout + result.stderr
    out = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr)).lower()
    assert "not found" in out, out


def test_axi_rm_site_without_yes_is_a_usage_error(running_instance):
    """The agent surface never prompts: missing --yes is a structured usage error
    on stdout (exit 2), and the site is left untouched."""
    inst = running_instance
    site = "cwe2e-dropsite-c.localhost"
    _create_site(inst, site)

    try:
        result = harness.run_cwcli("axi", "rm-site", inst.name, site)

        assert result.returncode == 2, result.stdout + result.stderr
        assert "error:" in result.stdout, result.stdout
        assert "--yes" in result.stdout, result.stdout
        assert _site_exists(inst, site), "site was dropped despite the missing --yes usage error"
    finally:
        _drop_temporary_site(inst, site)


def test_axi_rm_site_emits_toon_and_drops_for_real(running_instance):
    """The agent surface: one TOON document naming the archive's real host path,
    and a real drop - not a mocked or partial one."""
    inst = running_instance
    site = "cwe2e-dropsite-d.localhost"
    _create_site(inst, site)

    result = harness.run_cwcli("axi", "rm-site", inst.name, site, "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert f"site: {site}" in out, out
    assert "ok: true" in out, out
    assert "archive_pruned_in_container: true" in out, out
    assert "archived_host_path:" in out, out
    assert not _site_exists(inst, site), "site directory still present after axi rm-site"
    assert not _archived_exists(inst, site), "bench's own archive was not pruned from the container"


def test_rm_site_interactive_confirm_over_a_real_pty(running_instance):
    """The real TTY path: the destructive confirm is genuinely shown and genuinely
    awaited (the raw-mode marker proves it), and answering `y` really drops it."""
    import pexpect

    inst = running_instance
    site = "cwe2e-dropsite-e.localhost"
    _create_site(inst, site)

    child = harness.spawn_cwcli(["rm-site", inst.name, site], timeout=300)
    try:
        harness.expect_prompt_ready(child)
        child.send("y")
        child.send("\r")
        child.expect(pexpect.EOF, timeout=300)
    finally:
        child.close(force=True)

    assert not _site_exists(inst, site), "site still present after the interactive confirm"
    assert not _archived_exists(inst, site), "bench's own archive was not pruned from the container"


def test_rm_site_interactive_decline_leaves_the_site_untouched(running_instance):
    """Declining the confirm ('n') must leave the site fully intact."""
    import pexpect

    inst = running_instance
    site = "cwe2e-dropsite-f.localhost"
    _create_site(inst, site)

    try:
        child = harness.spawn_cwcli(["rm-site", inst.name, site], timeout=300)
        try:
            harness.expect_prompt_ready(child)
            child.send("n")
            child.send("\r")
            child.expect(pexpect.EOF, timeout=300)
        finally:
            child.close(force=True)

        assert _site_exists(inst, site), "site was dropped despite declining the confirmation"
    finally:
        _drop_temporary_site(inst, site)


def test_rm_site_backs_up_before_dropping_and_the_backup_restores(running_instance):
    """The backup gate's success half, end to end: a seeded record is in the host
    archive's verified database dump (with both file archives beside it), the site
    and its database are genuinely gone, and that dump genuinely restores the
    record into a recreated site - read back through Frappe, not grepped."""
    inst = running_instance
    # Hyphen-free on purpose: `cwcli restore`'s backup-filename parser cannot yet
    # match a site whose name contains a hyphen, and this test proves restore works.
    site = "cwe2edropsiteg.localhost"
    _create_site(inst, site)

    try:
        _seed_marker(inst, site)
        assert _count_marker(inst, site) == 1, "the seeded record did not land"
        db_name = _db_name(inst, site)

        result = harness.run_cwcli("axi", "rm-site", inst.name, site, "--yes", timeout=600)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "ok: true" in result.stdout, result.stdout
        assert not _site_exists(inst, site), "site directory still present after rm-site"
        assert not _database_exists(inst, db_name), "the site's database was not dropped"

        dirs = _dropped_site_dirs(inst, site)
        assert len(dirs) == 1, dirs
        assert f"archived_host_path: {dirs[0]}" in result.stdout, result.stdout
        assert (dirs[0] / f"{site}.tar").stat().st_size > 0, "the site archive is missing"
        backup_files = sorted((dirs[0] / "backups").iterdir())
        names = [f.name for f in backup_files]
        dumps = [f for f in backup_files if f.name.endswith("-database.sql.gz")]
        assert len(dumps) == 1, names
        assert MARKER.encode() in gzip.decompress(dumps[0].read_bytes()), "record not in dump"
        assert any(n.endswith("-private-files.tar") for n in names), names
        assert any(n.endswith("-files.tar") and "-private-" not in n for n in names), names

        # --- the dump genuinely restores, into a recreated site of the same name ---
        _create_site(inst, site)
        assert _count_marker(inst, site) == 0, "the recreated site already has the record"
        dest = _host_site_backups(inst, site)
        dest.mkdir(parents=True, exist_ok=True)
        for f in backup_files:
            shutil.copy(f, dest / f.name)

        restored = harness.run_cwcli(
            "restore",
            inst.name,
            "--site",
            site,
            "--latest",
            "--yes",
            "--mariadb-root-username",
            "root",
            "--mariadb-root-password",
            "123",
            # A same-bench dump needs no migrate, and skipping it skips the restart
            # of the bench every other shared test is still using.
            "--no-migrate",
            timeout=900,
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert _count_marker(inst, site) == 1, "the restored site does not have the record"
    finally:
        _drop_temporary_site(inst, site)


def test_rm_site_drops_nothing_when_the_backup_fails(running_instance):
    """The backup gate's failure half, on both surfaces: with the site's backups
    directory unwritable, `bench backup` fails, so rm-site exits non-zero with
    bench's own error, and the site, its database and its data all survive.
    Without the gate, `bench drop-site --force` ignored exactly this failure and
    dropped the database while cwcli reported `ok: true`."""
    inst = running_instance
    site = "cwe2e-dropsite-h.localhost"
    _create_site(inst, site)
    backups = f"{inst.bench}/sites/{site}/private/backups"

    try:
        _seed_marker(inst, site)
        db_name = _db_name(inst, site)
        code, out = harness.exec_in_frappe(inst.name, f"mkdir -p {backups} && chmod 0555 {backups}")
        assert code == 0, out

        for argv in (("axi", "rm-site"), ("rm-site",)):
            result = harness.run_cwcli(*argv, inst.name, site, "--yes", timeout=600)

            combined = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr))
            assert result.returncode == 1, combined
            assert "nothing was dropped" in combined, combined
            assert "Backup failed for Site" in combined, "bench's own error was not surfaced"
            assert _site_exists(inst, site), f"{argv}: the site was dropped"
            assert _database_exists(inst, db_name), f"{argv}: the database was dropped"
            assert _count_marker(inst, site) == 1, f"{argv}: the site's data was lost"
            assert not _archived_exists(inst, site), f"{argv}: bench archived the site"
            assert _dropped_site_dirs(inst, site) == [], "a half-written host archive was left"
    finally:
        harness.exec_in_frappe(inst.name, f"chmod 0755 {backups}")
        _drop_temporary_site(inst, site)
