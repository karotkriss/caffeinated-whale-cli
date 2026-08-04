"""`cwcli axi apps install` E2E - real Docker, real bench, real git fetch.

Validates the scoped-safe-half `install_apps(require_absent=True)` gate against a
real bench: the permitted path genuinely fetches and installs an app the site did
not have, the SAME command re-run is genuinely refused before anything is fetched
again, the git-URL spelling of an already-installed app is refused the same way
(proving `derive_app_name` feeds the guard against a real target), an unreadable
site fails closed, and omitting `--site` is a usage error.

It also validates the opt-in idempotent path: `--if-not-present` turns the
already-installed case into a reported exit-0 skip that never reinstalls and leaves
the running bench undisturbed.
"""

from __future__ import annotations

import json

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_APP = "payments"


def _installed_apps(inst) -> list[str]:
    code, out = harness.exec_in_frappe(
        inst.name,
        f"cd {inst.bench} && bench --site {inst.site} " "execute frappe.get_installed_apps",
    )
    assert code == 0, out
    return json.loads(out.strip().splitlines()[-1])


def _site_ping_code(inst) -> str:
    """The HTTP result a real site-routed request gets from this bench's web process."""
    code, config = harness.exec_in_frappe(
        inst.name, f"cat {inst.bench}/sites/common_site_config.json"
    )
    assert code == 0, config
    port = int(json.loads(config)["webserver_port"])
    code, out = harness.exec_in_frappe(
        inst.name,
        f'curl -sS --max-time 10 -o /dev/null -w "%{{http_code}}" '
        f'-H "Host: {inst.site}" http://localhost:{port}/api/method/ping',
    )
    assert code == 0, out
    return out.strip().splitlines()[-1]


def test_axi_apps_install_permitted_then_refused_on_rerun(running_instance):
    """The full arc against a real bench: fetch+install lands, state genuinely
    changes, and the exact same command re-run is refused before any fetch.

    Installs onto the SHARED session site, so it uninstalls again in a ``finally``.
    Every other shared-instance test that mutates state restores it - see
    ``_ensure_serving``'s "order-independently" and
    ``test_workspace_persistence_e2e``'s "regardless of collection order" - and
    this one did not, leaving an app on the site for every later test in the job.
    """
    inst = running_instance
    assert _APP not in _installed_apps(inst), "fixture already has payments installed"
    assert _site_ping_code(inst) == "200", "the running fixture must serve before the mutation"

    restored = False
    try:
        with harness.quiesce_v14_asset_watcher(inst.name, inst.bench):
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

        # Positive proof first: the already-running web process must genuinely serve
        # a site-routed request after loading the newly installed app. Checking only
        # The authoritative site state is the exact false green this regression closes.
        assert _site_ping_code(inst) == "200"

        # The state genuinely changed, and the necessary disturbance was reported.
        assert _APP in _installed_apps(inst)
        assert "restart-processes" in result.stdout

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

        uninstall = harness.run_cwcli(
            "apps", "uninstall", inst.name, _APP, "--site", inst.site, "--yes"
        )
        assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr

        # Positive proof before the negative state assertion: the post-uninstall
        # web process serves the site, then the removed app is confirmed absent.
        assert _site_ping_code(inst) == "200"
        assert "restart-processes" in uninstall.stdout + uninstall.stderr
        assert _APP not in _installed_apps(inst)
        restored = True
    finally:
        if not restored:
            # Best-effort cleanup that never masks the real assertion failure.
            if _APP in _installed_apps(inst):
                harness.run_cwcli(
                    "apps", "uninstall", inst.name, _APP, "--site", inst.site, "--yes"
                )
            harness.run_cwcli("axi", "restart", inst.name, "--process", "web")


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


def test_axi_apps_install_if_not_present_skips_an_already_installed_app(running_instance):
    """--if-not-present is the idempotent reading: an app the site already has is
    skipped and exits 0, never reinstalled. `frappe` has been installed since site
    creation, so this is a read-only proof needing no cleanup, and the running bench
    is NOT disturbed (a pure skip changes nothing, so no restart)."""
    inst = running_instance
    assert "frappe" in _installed_apps(inst)

    result = harness.run_cwcli(
        "axi",
        "apps",
        "install",
        inst.name,
        "frappe",
        "--site",
        inst.site,
        "--if-not-present",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "skip-install" in result.stdout
    assert "ok: true" in result.stdout
    assert not result.stdout.lstrip().startswith("{")  # TOON, never JSON
    # Skipped, not reinstalled, and the running bench was left alone.
    assert "install-app" not in result.stdout
    assert "restart-processes" not in result.stdout
    # And it is genuinely still there afterwards.
    assert "frappe" in _installed_apps(inst)


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
