"""Real-Docker E2E: a failed ``bench update --reset`` that leaves the WHOLE bench in
maintenance mode is reported by ``cwcli axi apps update`` and ``cwcli axi status``,
and the remedy cwcli prints brings the bench back.

``cwcli apps update <p> frappe`` runs bench's own ``bench update --reset``. bench
writes ``maintenance_mode: 1`` and ``pause_scheduler: 1`` into
``sites/common_site_config.json`` before it backs up the first site and writes 0
back only when the whole update succeeds (bench 5.31.0 has no ``finally``). So any
failure after the first second leaves every site that does not override the flags
at HTTP 503 with no scheduled jobs. cwcli used to report only ``failed_apps:
[frappe]``, and ``status`` read only the site's own ``site_config.json``, so it said
the site was not in maintenance while it answered 503.

**How the failure is produced without touching the shared bench's real code.** The
reported failure class is an app whose checked-out branch does not exist on its
remote (``fatal: ... 'upstream/feature-x' ...`` / ``couldn't find remote ref``). bench
resets ``frappe`` FIRST and every other app before the failing one in directory
order, so letting the real apps through would hard-reset the shared instance's code
to whatever upstream holds today. Instead this test lists every real app in
``sites/excluded_apps.txt`` (bench skips those: ``bench/app.py`` ``pull_apps``) and
plants ONE throwaway app on a local-only branch, cloned from a bare repo inside the
container the way ``bench get-app`` clones (``--origin upstream``). bench still runs
its real pre-flight, sets the flags, backs up every site, and then fails on that
app - the same code path, with nothing real reset.

What bench itself still does and the finalizer undoes: it rewrites
``common_site_config.json`` (restored byte-for-byte) and writes one backup per site
(the new backup files are deleted). Its frappe version check also runs
``git fetch --depth=1 upstream <branch>`` in ``apps/frappe``, which moves only the
remote-tracking ref - the checkout, HEAD and the running code are unchanged.

The throwaway app is never registered in ``sites/apps.txt`` and never installed on a
site, so nothing here can break the shared bench (the poisoning lesson), and the
run uses ``--no-recache`` so the cache never remembers it. It also passes
``--no-preflight``, because cwcli's own preflight catches this exact app before bench
starts (``tests/e2e/test_update_preflight_e2e.py`` covers that refusal).
"""

from __future__ import annotations

import json
import re
import shlex

import pytest

from . import harness

pytestmark = pytest.mark.e2e

APP = "cwe2e_resetapp"
REMOTE = "/tmp/cwe2e-resetapp.git"
LOCAL_ONLY_BRANCH = "cwe2e-local-only"
FLAGS = ("maintenance_mode", "pause_scheduler")


def _sh(inst, script: str) -> str:
    code, out = harness.exec_in_frappe(inst.name, script, workdir=inst.bench)
    assert code == 0, f"in-container step failed ({code}): {script}\n{out}"
    return out


def _common_config(inst) -> dict:
    return json.loads(_sh(inst, "cat sites/common_site_config.json"))


def _site_override(inst, site: str) -> object:
    """``site``'s own ``maintenance_mode`` (None when it sets none)."""
    return json.loads(_sh(inst, f"cat sites/{shlex.quote(site)}/site_config.json")).get(
        "maintenance_mode"
    )


def _backups(inst) -> set[str]:
    out = _sh(inst, "find sites -path '*/private/backups/*' -type f | sort")
    return {line for line in out.splitlines() if line.strip()}


def _plant_app_on_a_local_only_branch(inst) -> None:
    """One app bench will discover (``hooks.py``, ``modules.txt``, ``patches.txt``),
    whose checked-out branch exists only locally."""
    git = "git -c user.name=cwe2e -c user.email=cwe2e@example.invalid"
    _sh(
        inst,
        f"set -e; src=$(mktemp -d); mkdir -p $src/{APP}; "
        f"printf '__version__ = \"0.0.1\"\\n' > $src/{APP}/__init__.py; "
        f"printf 'app_name = \"{APP}\"\\n' > $src/{APP}/hooks.py; "
        f": > $src/{APP}/modules.txt; : > $src/{APP}/patches.txt; "
        f"{git} -C $src init -q -b main; {git} -C $src add -A; "
        f"{git} -C $src commit -qm init; "
        f"git clone -q --bare $src {REMOTE}; rm -rf $src; "
        f"git clone -q --origin upstream --branch main {REMOTE} apps/{APP}; "
        f"git -C apps/{APP} checkout -q -b {LOCAL_ONLY_BRANCH}",
    )


def _remedy_commands(update_stdout: str) -> list[list[str]]:
    """The ``cwcli run ...`` commands the update report printed, exactly as printed."""
    commands = re.findall(r"cwcli run \S+ set-config --global --parse \w+ 0", update_stdout)
    assert len(commands) == len(FLAGS), f"expected one remedy per flag:\n{update_stdout}"
    return [shlex.split(command) for command in commands]


def _restore(inst, script: str) -> None:
    """A best-effort finalizer step: never raises, so it cannot mask the real failure.
    The left-as-found assertions after the ``finally`` catch a step that did not take."""
    harness.exec_in_frappe(inst.name, script, workdir=inst.bench)


def test_a_failed_reset_reports_the_bench_left_down_and_its_remedy_works(running_instance):
    inst = running_instance
    original_common = _sh(inst, "base64 -w0 sites/common_site_config.json").strip()
    had_excluded = (
        harness.exec_in_frappe(inst.name, "test -f sites/excluded_apps.txt", workdir=inst.bench)[0]
        == 0
    )
    original_excluded = (
        _sh(inst, "base64 -w0 sites/excluded_apps.txt").strip() if had_excluded else ""
    )
    backups_before = _backups(inst)
    assert not any(
        _common_config(inst).get(flag) for flag in FLAGS
    ), "the shared bench is already flagged; an earlier test left it down"
    try:
        real_apps = _sh(inst, "ls -1 apps").split()
        _sh(
            inst,
            "printf '%s\\n' "
            + " ".join(shlex.quote(app) for app in real_apps)
            + " > sites/excluded_apps.txt",
        )
        _plant_app_on_a_local_only_branch(inst)

        # --no-preflight: cwcli's own preflight refuses this exact bench before bench
        # starts (git.branch_not_on_remote); this test needs bench's late failure.
        update = harness.run_cwcli(
            "axi", "apps", "update", inst.name, "frappe", "--no-recache", "--no-preflight"
        )

        assert update.returncode == 1, update.stdout + update.stderr
        assert "failed_apps[1]: frappe" in update.stdout, update.stdout
        assert "bench_left_in_maintenance: true" in update.stdout, update.stdout
        # bench really did leave both flags set bench-wide.
        assert all(_common_config(inst).get(flag) for flag in FLAGS)
        report = " ".join(update.stdout.split())
        for flag in FLAGS:
            assert f"cwcli run {inst.name} set-config --global --parse {flag} 0" in report

        status = harness.run_cwcli("axi", "status", inst.name)
        assert status.returncode == 0, status.stdout + status.stderr
        status_text = " ".join(status.stdout.split())
        assert "MAINTENANCE MODE ON bench-wide" in status_text, status.stdout
        assert "SCHEDULER PAUSED bench-wide" in status_text, status.stdout
        # The merged view: the site inherits the bench-wide flag unless it overrides it.
        probed = re.search(r"web_site: (\S+)", status.stdout)
        assert probed, status.stdout
        override = _site_override(inst, probed.group(1))
        expected = "true" if override is None else str(bool(override)).lower()
        assert f"maintenance_mode: {expected}" in status.stdout, status.stdout

        # The remedy, run exactly as printed, brings the bench back.
        for argv in _remedy_commands(update.stdout):
            assert argv[:2] == ["cwcli", "run"], argv
            result = harness.run_cwcli(*argv[1:], timeout=300)
            assert result.returncode == 0, f"{argv}\n{result.stdout}{result.stderr}"
        assert not any(_common_config(inst).get(flag) for flag in FLAGS)

        cleared = harness.run_cwcli("axi", "status", inst.name)
        assert cleared.returncode == 0, cleared.stdout + cleared.stderr
        assert "bench-wide" not in cleared.stdout, cleared.stdout
    finally:
        _restore(inst, f"rm -rf apps/{APP} {REMOTE}")
        _restore(inst, f"echo {original_common} | base64 -d > sites/common_site_config.json")
        if had_excluded:
            _restore(inst, f"echo {original_excluded} | base64 -d > sites/excluded_apps.txt")
        else:
            _restore(inst, "rm -f sites/excluded_apps.txt")
        code, out = harness.exec_in_frappe(
            inst.name, "find sites -path '*/private/backups/*' -type f", workdir=inst.bench
        )
        new_backups = set(out.split()) - backups_before if code == 0 else set()
        for path in sorted(new_backups):
            _restore(inst, f"rm -f {shlex.quote(path)}")

    # Left exactly as found: the config is the original, and the bench still works.
    assert _sh(inst, "base64 -w0 sites/common_site_config.json").strip() == original_common
    assert _backups(inst) == backups_before
    assert harness.exec_in_frappe(inst.name, f"test -e apps/{APP}", workdir=inst.bench)[0] != 0
    if had_excluded:
        assert _sh(inst, "base64 -w0 sites/excluded_apps.txt").strip() == original_excluded
    else:
        code, _ = harness.exec_in_frappe(
            inst.name, "test -e sites/excluded_apps.txt", workdir=inst.bench
        )
        assert code != 0
    code, out = harness.exec_in_frappe(
        inst.name, f"bench --site {shlex.quote(inst.site)} list-apps", workdir=inst.bench
    )
    assert code == 0, out
