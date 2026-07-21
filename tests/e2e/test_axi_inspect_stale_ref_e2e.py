"""`cwcli axi inspect` must not vouch for app state it did not observe live.

The regressed defect, on a real bench: after an app's git ref genuinely moves,
the cheap T1/T2 tiers carry the CACHED `installed_apps` forward (they never
re-run `bench list-apps`), yet label the read `served_from: partial` - which a
caller reads as freshness-checked. A next in-instance test then validates the
wrong code and passes.

A unit test over a mocked cache cannot prove this - it cannot show the container
disagreeing with the read surface's own memory. This drives the real sequence:
prime the cache, move `apps/frappe`'s branch inside the real container, confirm
the container's actual HEAD with git (the authoritative ref oracle, as the
original report used), then show the read surface FLAGGING its remembered
`installed_apps` as unverified rather than presenting it as fact.

Note on the oracle: cwcli's `installed_apps` is app-REGISTRY metadata from
`bench --site X list-apps` (install-time app name/version/branch), NOT a live
`git rev-parse`. So no inspect tier can cheaply re-observe a checkout from that
field - which is precisely why the correct fix is the fail-honest
verified-or-remembered token ("or refuses"), not "always print the live ref".
The token is what lets a caller tell a verified read from a remembered one; git
remains the authority on the checked-out ref.

Uses the `frappe` app itself - every bench already has `apps/frappe` as a real
git checkout.
"""

from __future__ import annotations

import pytest

from . import harness

pytestmark = pytest.mark.e2e

_APP_DIR = f"{harness.DEFAULT_BENCH_PATH}/apps/frappe"
_STALE_BRANCH = "cwe2e-inspect-stale-ref"


def _container_branch(inst) -> str:
    code, out = harness.exec_in_frappe(inst.name, f"git -C {_APP_DIR} branch --show-current")
    assert code == 0, out
    return out.strip()


def test_axi_inspect_does_not_vouch_for_an_unverified_app_ref(running_instance):
    inst = running_instance

    # Prime the cache with a full inspect: installed_apps is now live-observed,
    # and the token vouches for it.
    primed = harness.run_cwcli("axi", "inspect", inst.name, "--update")
    assert primed.returncode == 0, primed.stdout + primed.stderr
    assert "served_from: full" in primed.stdout
    assert "installed_apps_verified: true" in primed.stdout
    assert "served from cache and NOT observed live" not in primed.stdout
    original_branch = _container_branch(inst)

    try:
        # Move the ref inside the real container - the container's actual HEAD now
        # disagrees with what the cache last observed.
        code, out = harness.exec_in_frappe(
            inst.name, f"git -C {_APP_DIR} checkout -b {_STALE_BRANCH}"
        )
        assert code == 0, out
        # Confirm the container's actual HEAD (the authoritative ref oracle).
        assert _container_branch(inst) == _STALE_BRANCH
        code, head = harness.exec_in_frappe(inst.name, f"git -C {_APP_DIR} rev-parse HEAD")
        assert code == 0 and head.strip()

        # The default tiered read hits the cache, finds no drift (the checkout
        # touched neither the apps/ listing nor the site set), and serves the
        # REMEMBERED installed_apps. It must NOT present that as fact: the per-site
        # token says unverified, and a warning names the remedy. THIS is the fix -
        # the read surface no longer agrees blindly with its own memory.
        stale = harness.run_cwcli("axi", "inspect", inst.name)
        assert stale.returncode == 0, stale.stdout + stale.stderr
        assert "served_from: partial" in stale.stdout
        assert "installed_apps_verified: false" in stale.stdout
        assert "warnings[" in stale.stdout
        assert "served from cache and NOT observed live" in stale.stdout

        # A full read re-observes the container live and vouches again.
        fresh = harness.run_cwcli("axi", "inspect", inst.name, "--update")
        assert fresh.returncode == 0, fresh.stdout + fresh.stderr
        assert "served_from: full" in fresh.stdout
        assert "installed_apps_verified: true" in fresh.stdout
        assert "served from cache and NOT observed live" not in fresh.stdout
    finally:
        harness.exec_in_frappe(inst.name, f"git -C {_APP_DIR} checkout {original_branch}")
        harness.exec_in_frappe(inst.name, f"git -C {_APP_DIR} branch -D {_STALE_BRANCH}")
