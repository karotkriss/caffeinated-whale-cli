"""BUG-10: cwcli must not report success while the running site executes stale code.

Every apps verb operates on the literal path ``<bench>/apps/<app>`` and reports
``ok`` from that copy's git exit code alone, never checking which copy the bench
actually imports. Two real layouts break that, both reproduced here on a real
bench with a real virtualenv:

- **Orientation A (symlinked source):** ``apps/<app>`` is a symlink to a source
  dir outside the bench, and the venv imports a DIFFERENT copy. cwcli would write
  through the symlink and report success while the site runs the other copy.
- **Orientation B (real dir, env imports a different copy):** ``apps/<app>`` is a
  real git checkout, but the venv imports a separate copy elsewhere (a
  hand-installed app). An ``apps checkout`` lands in a copy the site does not load.

The detection is Python's own: the bench virtualenv's resolved import path,
compared against the real path of ``apps/<app>`` (symlinks resolved on both
sides), so the NORMAL symlink layout - import and ``apps/<app>`` resolving to the
same dir - is NOT flagged (asserted here against a clean ``frappe`` app). A unit
test with a fake container cannot prove find_spec resolving a divergent editable
install against a real venv; this does.

Commit to run on CI (real Docker); do NOT run locally.
"""

from __future__ import annotations

import textwrap

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_BENCH = harness.DEFAULT_BENCH_PATH
_PIP = f"{_BENCH}/env/bin/pip"

# Orientation B: a real git checkout at apps/srcdir, while the venv imports a
# separate copy at /workspace/.hdsrc/srcdir.
_SRCDIR = "srcdir"
# Orientation A: apps/srcsym is a symlink to one dir, the venv imports another.
_SRCSYM = "srcsym"


def _setup(inst) -> None:
    script = textwrap.dedent(
        f"""
        set -e
        BENCH={_BENCH}
        PIP={_PIP}

        _pkg() {{  # $1 = repo dir, $2 = package name
          mkdir -p "$1/$2"
          printf "__version__ = '0.0.1'\\n" > "$1/$2/__init__.py"
          printf "from setuptools import setup, find_packages\\nsetup(name='%s', version='0.0.1', packages=find_packages())\\n" "$2" > "$1/setup.py"
        }}

        # --- Orientation B: real dir apps/srcdir diverges from the imported copy ---
        _pkg /workspace/.hdsrc/{_SRCDIR} {_SRCDIR}
        "$PIP" install -e /workspace/.hdsrc/{_SRCDIR} >/dev/null 2>&1
        _pkg "$BENCH/apps/{_SRCDIR}" {_SRCDIR}
        cd "$BENCH/apps/{_SRCDIR}"
        git init -q
        git config user.email e2e@example.com
        git config user.name e2e
        git config commit.gpgsign false
        git add -A
        git commit -qm init
        git branch -M main
        git clone -q --bare "$BENCH/apps/{_SRCDIR}" /workspace/{_SRCDIR}-remote.git
        git remote add upstream /workspace/{_SRCDIR}-remote.git
        git fetch -q upstream
        # Track upstream/main so `apps update`'s plain `git pull` succeeds (up to date)
        # rather than failing on "no tracking information" - a failed pull would skip
        # the app before the wrong-copy check ever runs.
        git branch --set-upstream-to=upstream/main main

        # --- Orientation A: symlinked apps/srcsym, venv imports a DIFFERENT copy ---
        _pkg /workspace/.hdsrc/{_SRCSYM}-import {_SRCSYM}
        "$PIP" install -e /workspace/.hdsrc/{_SRCSYM}-import >/dev/null 2>&1
        _pkg /workspace/.hdsrc/{_SRCSYM}-target {_SRCSYM}
        ln -s /workspace/.hdsrc/{_SRCSYM}-target "$BENCH/apps/{_SRCSYM}"
        """
    )
    code, out = harness.exec_in_frappe(inst.name, script)
    assert code == 0, f"divergent-layout setup failed:\n{out}"


def _teardown(inst) -> None:
    script = textwrap.dedent(
        f"""
        BENCH={_BENCH}
        "{_PIP}" uninstall -y {_SRCDIR} {_SRCSYM} >/dev/null 2>&1 || true
        rm -rf "$BENCH/apps/{_SRCDIR}" "$BENCH/apps/{_SRCSYM}" \
               /workspace/.hdsrc/{_SRCDIR} /workspace/.hdsrc/{_SRCSYM}-import \
               /workspace/.hdsrc/{_SRCSYM}-target /workspace/{_SRCDIR}-remote.git
        rmdir /workspace/.hdsrc 2>/dev/null || true
        """
    )
    harness.exec_in_frappe(inst.name, script)


@pytest.fixture(scope="module")
def divergent_apps(session_instance):
    """Provision the two divergent app layouts once, tearing them down after.

    Module-scoped so the two editable pip installs run once; restores the shared
    session instance's state unconditionally (a stray app must not outlive the
    module and mislead a sibling test)."""
    if harness.frappe_container_id(session_instance.name) is None:
        harness.run_cwcli("start", session_instance.name, "--yes")
        harness.wait_for_site_ready(session_instance.name, session_instance.site)
    _teardown(session_instance)  # belt-and-suspenders: clear any prior remnants
    _setup(session_instance)
    try:
        yield session_instance
    finally:
        _teardown(session_instance)


def test_apps_list_flags_a_symlink_and_a_wrong_imported_copy(divergent_apps):
    """`cwcli axi apps list` names the divergence and the symlink; a clean app is not flagged."""
    inst = divergent_apps

    result = harness.run_cwcli("axi", "apps", "list", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout

    # app_copies renders as a TOON table:
    #   {app,entry,is_symlink,resolved_path,imported_path,diverged,checked}
    assert "app_copies" in out
    # Orientation B: apps/srcdir is real, but the bench imports .hdsrc/srcdir, so the
    # row ends imported_path=.hdsrc/srcdir, diverged=true, checked=true.
    assert f"/workspace/.hdsrc/{_SRCDIR},true,true" in out
    # Orientation A: apps/srcsym is a symlink (the is_symlink column is true), and
    # the symlink target is named.
    assert f"apps/{_SRCSYM},true," in out
    assert f"/workspace/.hdsrc/{_SRCSYM}-target" in out

    # No false positive: frappe resolves to apps/frappe on both sides, so it is
    # listed as available but never carries a divergent .hdsrc import.
    assert "frappe" in out
    assert "/workspace/.hdsrc/frappe" not in out


def test_inspect_full_read_flags_the_divergent_apps(divergent_apps):
    """`cwcli axi inspect --update` (the live full read) flags both orientations."""
    inst = divergent_apps

    result = harness.run_cwcli("axi", "inspect", inst.name, "--update")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout

    assert "served_from: full" in out  # the live tier that observes the divergence
    assert "app_copies" in out
    assert f"/workspace/.hdsrc/{_SRCDIR},true,true" in out  # imported,diverged,checked
    assert f"apps/{_SRCSYM},true," in out  # is_symlink column
    assert f"/workspace/.hdsrc/{_SRCSYM}-target" in out


def test_apps_checkout_of_a_divergent_app_is_not_ok(divergent_apps):
    """The headline fix: a checkout that lands in a copy the bench does not import
    is NOT reported as plain success - it is not-ok, names both paths, and exits 1.

    The git checkout itself genuinely succeeds on apps/srcdir (a real git repo with
    a local upstream); the divergence is what flips the result."""
    inst = divergent_apps

    result = harness.run_cwcli("axi", "apps", "checkout", inst.name, _SRCDIR, "main")

    # The git steps ran and succeeded (proving the checkout really landed) ...
    assert "checkout" in result.stdout
    assert "$ git fetch" in result.stderr
    # ... but the wrong-copy verification makes the whole result not-ok.
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ok: false" in result.stdout
    assert "verify-import" in result.stdout
    # Both paths are named so a human/agent can act.
    assert f"{_BENCH}/apps/{_SRCDIR}" in (result.stdout + result.stderr)
    assert f"/workspace/.hdsrc/{_SRCDIR}" in (result.stdout + result.stderr)


def test_apps_update_of_a_divergent_app_is_not_ok(divergent_apps):
    """`cwcli apps update` reaches the same wrong-copy verdict via its own path.

    srcdir is not installed on any site, so no migration runs; the pull lands on
    apps/srcdir while the bench imports .hdsrc/srcdir, so the run is not-ok with a
    warning naming both paths (exercised through the deprecated-free `apps update`)."""
    inst = divergent_apps

    result = harness.run_cwcli("axi", "apps", "update", inst.name, _SRCDIR)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "ok: false" in result.stdout
    assert f"/workspace/.hdsrc/{_SRCDIR},true,true" in result.stdout  # diverged app_imports row
    # The warning names both copies.
    assert "other copy" in (result.stdout + result.stderr)
    assert f"/workspace/.hdsrc/{_SRCDIR}" in (result.stdout + result.stderr)
