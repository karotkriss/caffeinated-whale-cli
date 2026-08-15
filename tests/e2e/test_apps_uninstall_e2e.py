"""Prove `cwcli apps uninstall` against a real bench and site.

The test establishes the app's presence through both the command's per-site read
surface and Frappe before uninstalling it, so the absence checks cannot pass
vacuously.
It then proves absence through both surfaces, checks the process resynchronisation,
and restores the shared session instance in a `finally`.
"""

from __future__ import annotations

import json
import re

import pytest

from . import harness
from .test_start_status_e2e import _ensure_serving, _wait_supervised_stack

pytestmark = pytest.mark.e2e

_APP = "payments"


def _installed_on_site(inst) -> list[str]:
    """Authoritative site state: the apps `frappe.get_installed_apps` reports."""
    code, out = harness.exec_in_frappe(
        inst.name,
        f"cd {inst.bench} && bench --site {inst.site} execute frappe.get_installed_apps",
    )
    assert code == 0, out
    return json.loads(out.strip().splitlines()[-1])


def _apps_list_site(inst) -> list[str]:
    """What `cwcli apps list --site --json` reports installed for the site.

    The per-site `installed` map is the destructive command's own read surface;
    the default `available` view is not, because the app source stays in `apps/`
    until `bench remove-app`, so it would still list a site-uninstalled app.
    """
    result = harness.run_cwcli("apps", "list", inst.name, "--site", inst.site, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    doc = json.loads(result.stdout)
    return doc["installed"][inst.site]


def _axi_apps_list_site(inst) -> list[str]:
    """What `cwcli axi apps list --site --installed` reports for the site."""
    result = harness.run_cwcli("axi", "apps", "list", inst.name, "--site", inst.site, "--installed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stdout.lstrip().startswith("{"), "must be TOON, never JSON"
    match = re.search(
        rf"^  {re.escape(inst.site)}\[(\d+)\]:(?: (.*))?$",
        result.stdout,
        re.MULTILINE,
    )
    assert match is not None, f"missing installed entry for {inst.site}: {result.stdout}"
    apps = match.group(2).split(",") if match.group(2) else []
    assert len(apps) == int(match.group(1)), result.stdout
    return apps


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


def _remove_app_source(inst, *, required: bool = True) -> None:
    """Remove the fetched app source after it has been uninstalled from the site."""
    code, out = harness.exec_in_frappe(
        inst.name, f"cd {inst.bench} && bench remove-app --no-backup {_APP}"
    )
    if required:
        assert code == 0, out


def test_apps_uninstall_destroys_the_sites_app_state(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    _wait_supervised_stack(inst.name)
    assert _APP not in _installed_on_site(inst), "fixture already has payments installed"
    assert _site_ping_code(inst) == "200", "the running fixture must serve before the mutation"

    restored = False
    try:
        # --- Establish the POSITIVE state through the human install verb, and prove
        # it on both the site and the command's own read surface. If this does not
        # hold, the destructive assertion below would pass against nothing.
        with harness.quiesce_v14_asset_watcher(inst.name, inst.bench):
            install = harness.run_cwcli(
                "apps",
                "install",
                inst.name,
                "--app",
                _APP,
                "--site",
                inst.site,
                "--branch",
                harness.FRAPPE_BRANCH,
                "--yes",
            )
        assert install.returncode == 0, install.stdout + install.stderr
        assert _APP in _installed_on_site(inst), "install did not reach the site"
        assert _APP in _apps_list_site(inst), "install is not visible in `apps list --site`"

        # The agent read verb reflects the same installed state (previously E2E-uncovered).
        assert _APP in _axi_apps_list_site(inst)

        # --- Destroy it.
        uninstall = harness.run_cwcli(
            "apps", "uninstall", inst.name, "--app", _APP, "--site", inst.site, "--yes"
        )
        assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr

        # POSITIVE proof the bench still works, then the promised side effect: the
        # long-lived processes were resynchronised, so none serves the dropped app.
        assert _site_ping_code(inst) == "200", "the site does not serve after uninstall"
        assert "restart-processes" in uninstall.stdout + uninstall.stderr

        # --- The NEGATIVE, on every surface the presence was proven on.
        assert _APP not in _installed_on_site(inst), "still installed on the site after uninstall"
        assert _APP not in _apps_list_site(inst), "still in `apps list --site` after uninstall"

        _remove_app_source(inst)
        restored = True
    finally:
        try:
            if not restored:
                # Best-effort cleanup that never masks the real assertion failure.
                try:
                    installed = _APP in _installed_on_site(inst)
                except (AssertionError, IndexError, json.JSONDecodeError):
                    installed = True
                try:
                    if installed:
                        harness.run_cwcli(
                            "apps",
                            "uninstall",
                            inst.name,
                            "--app",
                            _APP,
                            "--site",
                            inst.site,
                            "--yes",
                        )
                finally:
                    _remove_app_source(inst, required=False)
        finally:
            _ensure_serving(inst.name)
