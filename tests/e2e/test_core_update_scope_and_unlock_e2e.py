"""Real-Docker E2E: a core update touches ONLY frappe, clears every site's stale
locks first and lists them, and never leaves anything bench-wide behind.

The captain's staging16 run (2026-10-28): ``cwcli apps update staging16 frappe`` ran
bench's bench-wide ``bench update --reset``, which hard-reset EVERY app on the bench,
and a stale document lock (DocumentLockedError on a Module Profile fixture) killed
the migrate on one site while cwcli reported "Git pull failed". The core path now
resets the named core apps only, after clearing each site's ``locks`` folder, and
runs the ordinary per-site migrate under cwcli's own maintenance lifecycle.

Deterministic on the SHARED instance: frappe's ``upstream`` is repointed at a bare
mirror of its own HEAD (the reset is a byte-exact no-op; ``preserve_frappe_fetch_state``
puts the remote back) and ``bench setup requirements`` is shimmed (a real reinstall
takes minutes). The real migrate runs on the real site. A throwaway app is planted
with a LOCAL EDIT: a bench-wide reset would have wiped it; the core update must not.
"""

from __future__ import annotations

import shlex

import pytest

from . import harness
from .test_apps_update_e2e import _frappe_remote_mirrored, _install_bench_shim, _remove_bench_shim

pytestmark = pytest.mark.e2e

LOCK = "Module Profile-cwe2e-stale.lock"


def _sh(inst, script: str) -> str:
    code, out = harness.exec_in_frappe(inst.name, script, workdir=inst.bench)
    assert code == 0, f"in-container step failed ({code}): {script}\n{out}"
    return out


def test_core_update_resets_only_frappe_and_clears_every_site_s_locks(running_instance):
    inst = running_instance
    site = inst.site
    other = "cwe2e_untouched"
    locks = f"sites/{shlex.quote(site)}/locks"
    try:
        # A second app with uncommitted work: the old bench-wide reset wiped this.
        _sh(
            inst,
            f"rm -rf apps/{other} && mkdir -p apps/{other} && cd apps/{other} && "
            "git init -q && git -c user.name=cwe2e -c user.email=cwe2e@example.invalid "
            "commit -q --allow-empty -m init && echo local-edit > README.md",
        )
        _sh(inst, f"mkdir -p {locks} && touch {locks}/{shlex.quote(LOCK)}")
        frappe_head = _sh(inst, "git -C apps/frappe rev-parse HEAD").strip()
        _install_bench_shim(inst, exit_code=0)
        with harness.preserve_frappe_fetch_state(inst.name), _frappe_remote_mirrored(inst):
            result = harness.run_cwcli(
                "apps", "update", inst.name, "frappe", "--no-recache", "--yes", timeout=1800
            )
        text = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr))
        assert result.returncode == 0, text
        # The stale lock was cleared BEFORE the reset, and the log names it. Not an exact
        # count: frappe's filelock never unlinks bench_migrate.lock, so an earlier
        # migrate on this shared site leaves that file for the same step to clear.
        assert "Stale locks: cleared" in text and LOCK in text, text
        assert (
            harness.exec_in_frappe(
                inst.name, f"test -e {locks}/{shlex.quote(LOCK)}", workdir=inst.bench
            )[0]
            != 0
        )
        # frappe was reset to its (mirrored) remote branch: same HEAD, no git pull.
        assert _sh(inst, "git -C apps/frappe rev-parse HEAD").strip() == frappe_head
        assert "bench update --reset" not in text and "Git pull" not in text
        # The other app's local edit survived: nothing bench-wide ran.
        assert _sh(inst, f"cat apps/{other}/README.md").strip() == "local-edit"
        # The site migrated and is not in maintenance; nothing is set bench-wide.
        assert f"Migration completed for '{site}'" in text or "Successfully updated" in text
        common = _sh(inst, "cat sites/common_site_config.json")
        assert '"maintenance_mode": 1' not in common and '"pause_scheduler": 1' not in common
        status = harness.run_cwcli("axi", "status", inst.name)
        assert "bench-wide" not in status.stdout, status.stdout
    finally:
        _remove_bench_shim(inst)
        harness.exec_in_frappe(
            inst.name, f"rm -rf apps/{other} {locks}/{shlex.quote(LOCK)}", workdir=inst.bench
        )


MISMATCH_APP = "cwe2e_depmismatch"
MISMATCH_REMOTE = "/tmp/cwe2e-depmismatch.git"


def _plant_mismatched_narrow_clone(inst, frappe_major: int) -> None:
    """An app whose `develop` declares `frappe >=99` (unsatisfiable here) and whose
    `version-<major>` declares `frappe >=<major>,<<major+1>`, cloned the way a narrow
    clone is: `--single-branch --branch develop`, so the refspec covers develop only."""
    git = "git -c user.name=cwe2e -c user.email=cwe2e@example.invalid"
    pyproject = (
        '[project]\nname = "{app}"\nversion = "0.0.1"\n'
        '[tool.bench.frappe-dependencies]\nfrappe = "{spec}"\n'
    )
    _sh(
        inst,
        f"set -e; rm -rf {MISMATCH_REMOTE} apps/{MISMATCH_APP}; src=$(mktemp -d); mkdir -p $src/{MISMATCH_APP}; "
        f"printf '__version__ = \"0.0.1\"\\n' > $src/{MISMATCH_APP}/__init__.py; "
        f"printf 'app_name = \"{MISMATCH_APP}\"\\n' > $src/{MISMATCH_APP}/hooks.py; "
        f": > $src/{MISMATCH_APP}/modules.txt; : > $src/{MISMATCH_APP}/patches.txt; "
        f"printf '%s' {shlex.quote(pyproject.format(app=MISMATCH_APP, spec=f'>={frappe_major},<{frappe_major + 1}'))} > $src/pyproject.toml; "
        f"{git} -C $src init -q -b version-{frappe_major}; {git} -C $src add -A; {git} -C $src commit -qm release; "
        f"{git} -C $src checkout -q -b develop; "
        f"printf '%s' {shlex.quote(pyproject.format(app=MISMATCH_APP, spec='>=99.0.0'))} > $src/pyproject.toml; "
        f"{git} -C $src commit -qam develop; "
        f"git clone -q --bare $src {MISMATCH_REMOTE}; rm -rf $src; "
        f"git clone -q --origin upstream --single-branch --branch develop {MISMATCH_REMOTE} apps/{MISMATCH_APP}",
    )


def test_a_dependency_mismatch_refuses_and_fix_branches_switches_and_tracks_the_app(
    running_instance,
):
    inst = running_instance
    major = int(
        _sh(inst, "grep -m1 __version__ apps/frappe/frappe/__init__.py").split('"')[1].split(".")[0]
    )
    try:
        _plant_mismatched_narrow_clone(inst, major)
        _install_bench_shim(inst, exit_code=0)
        with harness.preserve_frappe_fetch_state(inst.name), _frappe_remote_mirrored(inst):
            refused = harness.run_cwcli(
                "apps", "update", inst.name, "frappe", "--no-recache", "--yes", timeout=1800
            )
            text = harness.collapse_ws(harness.strip_ansi(refused.stdout + refused.stderr))
            assert refused.returncode == 1, text
            assert f"switch {MISMATCH_APP} to version-{major}" in text, text
            assert f"cwcli apps checkout {inst.name} {MISMATCH_APP} version-{major}" in text, text
            assert "--fix-branches" in text and "The core update did not run" in text
            fixed = harness.run_cwcli(
                "apps",
                "update",
                inst.name,
                "frappe",
                "--no-recache",
                "--yes",
                "--fix-branches",
                timeout=1800,
            )
            text = harness.collapse_ws(harness.strip_ansi(fixed.stdout + fixed.stderr))
            assert fixed.returncode == 0, text
            assert f"switched '{MISMATCH_APP}' to version-{major}" in text, text
        assert (
            _sh(inst, f"git -C apps/{MISMATCH_APP} rev-parse --abbrev-ref HEAD").strip()
            == f"version-{major}"
        )
        # The narrow clone's refspec now covers the branch and the remote-tracking ref exists.
        refspecs = _sh(inst, f"git -C apps/{MISMATCH_APP} config --get-all remote.upstream.fetch")
        assert f"refs/heads/version-{major}:" in refspecs, refspecs
        _sh(inst, f"git -C apps/{MISMATCH_APP} rev-parse --verify upstream/version-{major}")
    finally:
        _remove_bench_shim(inst)
        harness.exec_in_frappe(
            inst.name, f"rm -rf apps/{MISMATCH_APP} {MISMATCH_REMOTE}", workdir=inst.bench
        )
