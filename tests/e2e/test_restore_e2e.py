"""Restore E2E - the full-lifecycle proof for cwcli's most destructive path
(batch 11, ``migrate-restore-core``).

The genuine-restore assertion is a cache-free DB marker: a throwaway table
``cwe2e_marker`` is seeded with ``ORIGINAL``, backed up, then MUTATED; after
``cwcli restore`` the table must read ``ORIGINAL`` again (the backup's state),
proving the destructive restore actually dropped-and-recreated the DB rather than
no-op'ing. Both modes are exercised (non-interactive flags + a real pty), the
mariadb root password rides ``--mariadb-root-password`` / the interactive prompt,
and post-restore the site is asserted to boot (migrate + restart ran).

All isolation (temp HOME + ``CWCLI_HOME``, the ``cwe2e-`` prefix, the sweep
backstop) comes from ``conftest.py``'s session fixtures; the shared instance is a
real ``cwcli init`` torn down with ``cwcli rm``.
"""

from __future__ import annotations

import pytest

from . import harness

pytestmark = pytest.mark.e2e

DB_PW = "123"  # the init default (compose MYSQL_ROOT_PASSWORD: 123)


# --------------------------------------------------------------------------- #
# The cache-free DB marker: a throwaway table dumped by `bench backup`.
# --------------------------------------------------------------------------- #
def _sql(inst, sql: str) -> tuple[int, str]:
    """Run one SQL statement in the site's DB via `bench execute frappe.db.sql`.

    The SQL uses double-quoted string literals; it is passed as a single-quoted
    Python string inside bash double-quotes, so the SQL's double quotes are
    escaped for bash. `bench execute` commits after the call.
    """
    py_arg = "['" + sql.replace('"', '\\"') + "']"
    script = (
        f"cd {inst.bench} && bench --site {inst.site} execute frappe.db.sql "
        f'--args "{py_arg}"'
    )
    return harness.exec_in_frappe(inst.name, script)


def _seed_marker(inst, value: str) -> None:
    code, out = _sql(inst, "CREATE TABLE IF NOT EXISTS cwe2e_marker (v VARCHAR(32))")
    assert code == 0, f"CREATE TABLE failed: {out}"
    code, out = _sql(inst, "DELETE FROM cwe2e_marker")
    assert code == 0, f"DELETE failed: {out}"
    code, out = _sql(inst, f'INSERT INTO cwe2e_marker VALUES ("{value}")')
    assert code == 0, f"INSERT failed: {out}"


def _mutate_marker(inst, value: str) -> None:
    code, out = _sql(inst, f'UPDATE cwe2e_marker SET v = "{value}"')
    assert code == 0, f"UPDATE failed: {out}"


def _read_marker(inst) -> str:
    code, out = _sql(inst, "SELECT v FROM cwe2e_marker")
    assert code == 0, f"SELECT failed: {out}"
    return out


# --------------------------------------------------------------------------- #
# Non-interactive: the full genuine-restore lifecycle.
# --------------------------------------------------------------------------- #
def test_restore_noninteractive_is_genuine(running_instance):
    inst = running_instance

    # 1. Seed ORIGINAL, 2. back up (captures ORIGINAL), 3. mutate to MUTATED.
    _seed_marker(inst, "ORIGINAL")
    r = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    _mutate_marker(inst, "MUTATED")

    # Sanity: the live DB really holds MUTATED before the restore.
    assert "MUTATED" in _read_marker(inst)

    # 4. Restore the latest backup non-interactively.
    r = harness.run_cwcli(
        "restore",
        inst.name,
        "--site",
        inst.site,
        "--latest",
        "--yes",
        "--mariadb-root-password",
        DB_PW,
    )
    assert r.returncode == 0, f"restore failed: {r.stdout}\n{r.stderr}"
    assert "Successfully restored" in r.stdout

    # 5. The restore is GENUINE: the marker is back to the backup's state.
    marker = _read_marker(inst)
    assert "ORIGINAL" in marker and "MUTATED" not in marker, marker

    # 6. Post-restore the site still boots (migrate + restart ran).
    harness.wait_for_site_ready(inst.name, inst.site)


def test_restore_no_migrate_still_restores(running_instance):
    inst = running_instance
    _seed_marker(inst, "ORIGINAL")
    r = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    _mutate_marker(inst, "MUTATED")

    r = harness.run_cwcli(
        "restore",
        inst.name,
        "--site",
        inst.site,
        "--latest",
        "--yes",
        "--mariadb-root-password",
        DB_PW,
        "--no-migrate",
    )
    assert r.returncode == 0, f"restore --no-migrate failed: {r.stdout}\n{r.stderr}"
    assert "ORIGINAL" in _read_marker(inst)
    # --no-migrate skips the migrate + restart; the container is still up.
    harness.run_cwcli("start", inst.name, "--yes")
    harness.wait_for_site_ready(inst.name, inst.site)


def test_restore_after_cache_clear_auto_inspects_and_succeeds(running_instance):
    """A fresh/cleared cache (fm/cwcli-backup-restore-autoinspect): pins the
    CORE's own belt-and-suspenders fallback in ``restore_plan`` (the frontend
    prologue already ran inspect on a cold cache before this fix, but the core
    function itself - the path ``core_restore.restore_plan`` takes when called
    directly, e.g. by any future caller that skips the CLI prologue - did not).

    ``--site`` is given explicitly (matching the sibling tests in this file):
    a freshly ``cwcli init``'d bench records NO default site anywhere (neither
    ``common_site_config.json`` nor ``currentsite.txt``), a separate,
    pre-existing gap this change does not touch.
    """
    inst = running_instance
    _seed_marker(inst, "ORIGINAL")
    r = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    _mutate_marker(inst, "MUTATED")

    cleared = harness.run_cwcli("config", "cache", "clear", inst.name)
    assert cleared.returncode == 0, cleared.stdout + cleared.stderr

    try:
        r = harness.run_cwcli(
            "restore",
            inst.name,
            "--site",
            inst.site,
            "--latest",
            "--yes",
            "--mariadb-root-password",
            DB_PW,
            "--no-migrate",
        )
        assert r.returncode == 0, f"restore after cache clear failed: {r.stdout}\n{r.stderr}"
        assert "No cached bench path found. Running inspect" in r.stderr
        assert "ORIGINAL" in _read_marker(inst)
    finally:
        # --no-migrate skips the restart; bring the container back and leave a
        # fully warm cache for any sibling test sharing this instance.
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)
        harness.run_cwcli("inspect", inst.name, "--update")


# --------------------------------------------------------------------------- #
# Interactive: the pty menu + destructive confirm + credential prompts.
# --------------------------------------------------------------------------- #
def test_restore_interactive_is_genuine(running_instance):
    import pexpect

    inst = running_instance
    _seed_marker(inst, "ORIGINAL")
    r = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    _mutate_marker(inst, "MUTATED")

    # No selector -> the interactive backup menu; the newest backup is the first
    # selectable row, so Enter selects it. Then the destructive confirm (y+Enter),
    # then the mariadb username (Enter -> root) and password prompts.
    child = harness.spawn_cwcli(["restore", inst.name, "--site", inst.site], timeout=1200)
    try:
        # Backup selection menu.
        child.expect("Select a backup to restore", timeout=300)
        harness.expect_prompt_ready(child)
        child.sendline("")  # select the highlighted (newest) backup

        # Destructive confirm: press y THEN Enter (a human's keystrokes), so the
        # confirm consumes its own trailing Enter (auto_enter=False).
        child.expect("Are you sure you want to restore", timeout=300)
        harness.expect_prompt_ready(child)
        child.send("y")
        child.sendline("")

        # MariaDB username (default root) then password.
        child.expect("MariaDB root username", timeout=120)
        harness.expect_prompt_ready(child)
        child.sendline("")  # blank -> root
        child.expect("MariaDB root password", timeout=120)
        harness.expect_prompt_ready(child)
        child.sendline(DB_PW)

        child.expect("Successfully restored", timeout=1200)
        child.expect(pexpect.EOF, timeout=300)
    finally:
        child.close(force=True)

    assert "ORIGINAL" in _read_marker(inst)
    harness.wait_for_site_ready(inst.name, inst.site)


# --------------------------------------------------------------------------- #
# Targeted non-interactive refusals / guards (fast; no real restore needed).
# --------------------------------------------------------------------------- #
def test_non_tty_without_selector_refuses(running_instance):
    inst = running_instance
    # A non-TTY (stdin closed) with no --latest/--backup-file must refuse the menu.
    r = harness.run_cwcli("restore", inst.name, "--site", inst.site)
    assert r.returncode != 0
    assert "--latest" in r.stderr or "--backup-file" in r.stderr


def test_non_tty_without_password_refuses(running_instance):
    inst = running_instance
    # A non-TTY with a selector + --yes but no password must refuse (secret prompt
    # cannot be answered non-interactively).
    r = harness.run_cwcli("restore", inst.name, "--site", inst.site, "--latest", "--yes")
    assert r.returncode != 0
    assert "password" in (r.stdout + r.stderr).lower()


def test_receive_non_tty_without_ticket_refuses(running_instance):
    inst = running_instance
    # A non-TTY --receive with no --ticket must refuse (non-zero) naming --ticket,
    # never the old silent exit-0 no-op that looked like a successful restore.
    r = harness.run_cwcli("restore", inst.name, "--receive")
    assert r.returncode != 0
    assert "--ticket" in r.stderr


def test_mutually_exclusive_flags(running_instance):
    inst = running_instance
    assert harness.run_cwcli("restore", inst.name, "--send", "--receive").returncode != 0
    assert (
        harness.run_cwcli(
            "restore", inst.name, "--latest", "--backup-file", "x.sql.gz"
        ).returncode
        != 0
    )
    assert harness.run_cwcli("restore", inst.name, "--latest", "--send").returncode != 0
