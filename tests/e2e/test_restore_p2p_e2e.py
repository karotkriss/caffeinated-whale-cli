"""P2P (sendme) send->receive E2E - the ``e2e_p2p`` marker's real transport proof.

``restore --send``/``--receive`` is cwcli's peer-to-peer backup transport (iroh via
sendme), riding on top of its MOST destructive command (restore drops and recreates
a live site's DB). This suite moves REAL bytes over sendme, loopback on one box:
back up a seeded site, serve it with ``restore --send`` (a genuine sendme ticket),
then pull-and-restore it with ``restore --receive --ticket <t>`` (the non-interactive
t5 path) and prove the data arrived by a cache-free DB marker. Nothing is mocked -
if the sendme transport broke, ``test_sendme_send_receive_loopback_is_genuine``
would FAIL.

Two isolation decisions, both deferred review notes from the E2E foundation (PR #60):

1. **Destructive tests do NOT share the session instance.** A restore wipes the
   site DB, so a P2P receive on the shared ``session_instance`` could corrupt another
   test's fixture. These tests stand up their OWN dedicated ``cwcli init`` instance
   (``p2p_instance``, module-scoped) and tear it down; they never touch
   ``session_instance``.
2. **The v14-only ``--receive`` bare-filename bug is a SEPARATE, clearly-labeled
   test.** On Frappe v14, ``bench restore <bare-filename>`` (cwd = the backups dir)
   fails ``Invalid path``; v15/v16 mask it with an alternative-directory fallback.
   cwcli builds the FULL container path, so a real v14 receive succeeds.
   ``test_receive_bare_filename_path_v14`` reproduces that on the v14 leg only; the
   generic loopback proof runs once on the v16 leg.

sendme is installed on demand by cwcli into the isolated ``$HOME/.local/bin`` on the
first ``--send``; a genuinely failed install surfaces as a test failure (never a
silent skip). All isolation (temp HOME + ``CWCLI_HOME``, the ``cwe2e-`` prefix, the
sweep backstop) comes from ``conftest.py``'s session fixtures.
"""

from __future__ import annotations

import os

import pytest

from . import harness

# Reuse the cache-free DB-marker helpers proven by the restore E2E (a throwaway
# ``cwe2e_marker`` table dumped by `bench backup`); duplicating them here would just
# drift. Importing a sibling e2e module is safe - it only sets its own pytestmark.
from .conftest import SESSION_ADMIN_PW, Instance
from .test_restore_e2e import DB_PW, _mutate_marker, _read_marker, _seed_marker

# Both markers: `e2e` puts these on the existing per-version matrix (so `-m e2e`
# runs them); `e2e_p2p` makes the P2P marker non-empty and lets a targeted
# `-m e2e_p2p` run select them alone. Each test is version-gated below so the
# generic transport proof runs ONCE (v16) while the v14 reproduction runs on v14.
# `standalone`: `p2p_instance` is this module's own instance; nothing here touches
# the shared session instance (see the marker's entry in pyproject.toml).
pytestmark = [pytest.mark.e2e, pytest.mark.e2e_p2p, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="version-agnostic P2P transport proof runs only on the v16 leg",
)
v14_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 14,
    reason="the --receive bare-filename bug only reproduces on Frappe v14 "
    "(v15/v16 mask it with an alternative-directory fallback)",
)


# --------------------------------------------------------------------------- #
# A DEDICATED, non-shared instance (destructive isolation - PR #60 note 1).
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def p2p_instance(isolated_home, _docker_gate, _teardown_backstop, port_allocator):
    """A throwaway `cwcli init` instance used ONLY by the destructive P2P tests, so
    a receive-mode DB wipe can never corrupt the shared session fixture."""
    harness.enforce_isolation()
    name = harness.project_name("p2p")
    port = port_allocator.next()
    result = harness.run_cwcli(
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
    if result.returncode != 0:
        pytest.fail(
            f"`cwcli init {name}` (frappe {harness.FRAPPE_BRANCH}) failed for the P2P "
            f"instance (exit {result.returncode}).\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    harness.wait_for_site_ready(name, harness.DEFAULT_SITE)
    inst = Instance(
        name=name,
        site=harness.DEFAULT_SITE,
        bench=harness.DEFAULT_BENCH_PATH,
        port=port,
        init_stdout=result.stdout,
        init_stderr=result.stderr,
    )
    try:
        yield inst
    finally:
        harness.cwcli_rm(name)


# --------------------------------------------------------------------------- #
# The send half: drive `restore --send` on a WIDE pty and capture the ticket.
# --------------------------------------------------------------------------- #
def _spawn_sender_and_get_ticket(inst, timeout: int = 600):
    """Spawn `restore <name> --send --site <site>` interactively, select the newest
    backup, and return (child, ticket). The sender keeps serving (blocked on the
    transfer) until the caller closes it, so the receiver has a live peer.

    A wide terminal keeps the (long) sendme ticket on ONE line so it never wraps.
    On the first `--send`, cwcli auto-installs sendme into the isolated $HOME; the
    generous timeout covers that download.
    """
    import pexpect

    child = pexpect.spawn(
        harness.CWCLI,
        ["restore", inst.name, "--send", "--site", inst.site],
        env=os.environ.copy(),
        encoding="utf-8",
        timeout=timeout,
        dimensions=(50, 400),  # wide, so the ticket line never wraps
    )
    # Backup-selection menu: the newest set is the first selectable row -> Enter.
    child.expect("Select a backup to restore", timeout=timeout)
    harness.expect_prompt_ready(child)
    child.sendline("")

    # sendme prints the ticket; cwcli echoes it as "sendme ticket: <ticket>".
    child.expect(r"sendme ticket:[^\r\n]+", timeout=timeout)
    line = harness.strip_ansi(child.after)
    ticket = line.split("sendme ticket:", 1)[1].strip()
    assert ticket, f"empty sendme ticket parsed from: {line!r}"
    return child, ticket


def _run_p2p_loopback(inst):
    """Seed ORIGINAL, back up, mutate to MUTATED, then send->receive over sendme and
    assert the marker is ORIGINAL again (the sent backup's bytes genuinely arrived)."""
    _seed_marker(inst, "ORIGINAL")
    r = harness.run_cwcli("backup", inst.name, "--site", inst.site, "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    _mutate_marker(inst, "MUTATED")
    assert "MUTATED" in _read_marker(inst)  # live DB really diverged before receive

    child, ticket = _spawn_sender_and_get_ticket(inst)
    try:
        # The t5 non-interactive receive path: --ticket + --yes + --mariadb password.
        r = harness.run_cwcli(
            "restore",
            inst.name,
            "--receive",
            "--ticket",
            ticket,
            "--site",
            inst.site,
            "--yes",
            "--mariadb-root-password",
            DB_PW,
            timeout=1200,
        )
    finally:
        # Stop the sender (sendme send serves until interrupted).
        child.sendcontrol("c")
        child.close(force=True)

    assert r.returncode == 0, f"receive failed: {r.stdout}\n{r.stderr}"
    assert "Successfully restored" in r.stdout

    # The transferred backup held ORIGINAL; a genuine P2P receive-restore brings the
    # live DB back to it. If sendme moved nothing, the marker would still read MUTATED.
    marker = _read_marker(inst)
    assert "ORIGINAL" in marker and "MUTATED" not in marker, marker

    # Post-restore the site still boots (migrate + restart ran on the receive path).
    harness.wait_for_site_ready(inst.name, inst.site)


# --------------------------------------------------------------------------- #
# Generic transport proof (runs ONCE, on the v16 leg).
# --------------------------------------------------------------------------- #
@v16_only
def test_sendme_send_receive_loopback_is_genuine(p2p_instance):
    """Real send->receive over sendme: the ticket the sender serves is fed verbatim
    to a non-interactive `--receive --ticket`, and the data arrives. This is the
    ``e2e_p2p`` marker's real test - it FAILS if the sendme transport breaks."""
    _run_p2p_loopback(p2p_instance)


# --------------------------------------------------------------------------- #
# v14-only reproduction of the `--receive` bare-filename bug (SEPARATE, LABELED).
# --------------------------------------------------------------------------- #
@v14_only
def test_receive_bare_filename_path_v14(p2p_instance):
    """v14-ONLY: a real sendme receive on Frappe v14, where `bench restore
    <bare-filename>` (cwd = the backups dir) fails `Invalid path`. cwcli builds the
    FULL container path, so this receive succeeds - the regression guard for that
    fix. On v15/v16 an alternative-directory fallback masks the bug, so this test is
    gated to the v14 leg and kept separate from the generic loopback proof above."""
    _run_p2p_loopback(p2p_instance)
