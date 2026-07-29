"""`cwcli apps uninstall` E2E - real Docker, real bench, real site data destroyed.

`apps uninstall` runs `bench uninstall-app`, which DROPS the app's tables from the
site's database - the one apps-group path that destroys user data. It shipped on
unit proof; the maintainer standard requires a data-destroying command proven on a
real instance, and proven the honest way: the app must be genuinely present first,
or asserting its absence afterwards passes vacuously against a no-op that removed
nothing.

This is the dedicated destructive proof. The install E2E exercises uninstall inside
its own happy path, but that proof is coupled to install succeeding and never checks
the command's OWN read surface (`apps list`). This test stands alone and asserts the
presence-then-absence arc on that surface:

1. POSITIVE first - the app is genuinely installed, visible in `apps list --site`,
   and present in the authoritative `frappe.get_installed_apps`. Fails here if the
   install did not land, so the later absence check can never pass vacuously.
2. The destructive `cwcli apps uninstall`.
3. NEGATIVE - gone from `apps list --site` and from the site - plus the side effect
   the command promises: the long-lived processes are resynchronised (the site still
   serves 200 and the uninstall reports `restart-processes`).

The setup install runs through the human `cwcli apps install`, giving that verb real
E2E exercise too, and a read through `cwcli axi apps list` covers the agent read verb
(previously unit-only). `axi apps uninstall` deliberately does not exist, so there is
no agent destructive surface to prove.

Mutates the shared session instance (installs then uninstalls an app, cycles its
stack), so it removes the app source and restores serving in a `finally`, per the
shared-instance convention.
"""

from __future__ import annotations

import json

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
        install = harness.run_cwcli(
            "apps",
            "install",
            inst.name,
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
        axi_list = harness.run_cwcli(
            "axi", "apps", "list", inst.name, "--site", inst.site, "--installed"
        )
        assert axi_list.returncode == 0, axi_list.stdout + axi_list.stderr
        assert not axi_list.stdout.lstrip().startswith("{"), "must be TOON, never JSON"
        assert _APP in axi_list.stdout

        # --- Destroy it.
        uninstall = harness.run_cwcli(
            "apps", "uninstall", inst.name, _APP, "--site", inst.site, "--yes"
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
        if not restored:
            # Best-effort cleanup that never masks the real assertion failure.
            if _APP in _installed_on_site(inst):
                harness.run_cwcli(
                    "apps", "uninstall", inst.name, _APP, "--site", inst.site, "--yes"
                )
            _remove_app_source(inst, required=False)
        _ensure_serving(inst.name)
