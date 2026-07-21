"""`cwcli axi apps install` E2E - real Docker, real bench, real git fetch.

Validates the scoped-safe-half `install_apps(require_absent=True)` gate against a
real bench: the permitted path genuinely fetches and installs an app the site did
not have, the SAME command re-run is genuinely refused before anything is fetched
again, the git-URL spelling of an already-installed app is refused the same way
(proving `derive_app_name` feeds the guard against a real target), an unreadable
site fails closed, and omitting `--site` is a usage error.
"""

from __future__ import annotations

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_APP = "payments"


def _installed_apps(inst) -> list[str]:
    code, out = harness.exec_in_frappe(
        inst.name, f"cd {inst.bench} && bench --site {inst.site} list-apps"
    )
    assert code == 0, out
    return [line.split()[0] for line in out.strip().splitlines() if line.strip()]


def test_axi_apps_install_permitted_then_refused_on_rerun(running_instance):
    """The full arc against a real bench: fetch+install lands, state genuinely
    changes, and the exact same command re-run is refused before any fetch."""
    inst = running_instance
    assert _APP not in _installed_apps(inst), "fixture already has payments installed"

    result = harness.run_cwcli(
        "axi",
        "apps",
        "install",
        inst.name,
        _APP,
        "--site",
        inst.site,
        "--branch",
        harness.FRAPPE_BRANCH,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"project: {inst.name}" in result.stdout
    assert "get-app" in result.stdout and "install-app" in result.stdout
    assert "ok: true" in result.stdout
    assert not result.stdout.lstrip().startswith("{")  # TOON, never JSON

    # bench's own output narrates to stderr, stdout stays one document.
    assert "get-app" in result.stderr

    # The state genuinely changed.
    assert _APP in _installed_apps(inst)

    # Re-running the EXACT same command is refused, before anything is fetched.
    rerun = harness.run_cwcli(
        "axi",
        "apps",
        "install",
        inst.name,
        _APP,
        "--site",
        inst.site,
        "--branch",
        harness.FRAPPE_BRANCH,
    )
    assert rerun.returncode == 1, rerun.stdout + rerun.stderr
    assert rerun.stdout.startswith("error:")
    assert "already installed" in rerun.stdout
    assert "apps checkout" in rerun.stdout
    assert "apps update" in rerun.stdout


def test_axi_apps_install_refuses_the_git_url_spelling_of_an_installed_app(running_instance):
    """`derive_app_name` feeds the guard: the git-URL spelling of an app the site
    already has (frappe, installed since site creation) is refused the same way."""
    inst = running_instance

    result = harness.run_cwcli(
        "axi",
        "apps",
        "install",
        inst.name,
        "https://github.com/frappe/frappe.git",
        "--site",
        inst.site,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "already installed" in result.stdout
    assert "'frappe'" in result.stdout


def test_axi_apps_install_fails_closed_on_an_unreadable_site(running_instance):
    inst = running_instance

    result = harness.run_cwcli(
        "axi", "apps", "install", inst.name, "hrms", "--site", "nosuch.localhost"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot be confirmed" in result.stdout


def test_axi_apps_install_without_site_is_a_usage_error(running_instance):
    inst = running_instance

    result = harness.run_cwcli("axi", "apps", "install", inst.name, "hrms")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--site" in (result.stdout + result.stderr)
