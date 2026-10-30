"""Real-Docker E2E: an app's Frappe range and required apps are checked before it lands.

Three proofs on the SHARED instance, none of which installs anything into the bench
(the shared-bench rule: nothing an E2E installs may be able to break it):

1. A real upstream app (frappe/hrms) at a branch built for ANOTHER Frappe major is
   refused by ``apps install`` before anything is fetched: exit 1, the declared range
   and the remedy named, and no ``apps/hrms`` left behind.
2. ``axi apps plan`` for hrms at the branch that fits reads its ``required_apps``
   (``frappe/erpnext``) and orders erpnext first, changing nothing.
3. An app planted with a LOCAL bare remote whose new tip adds a required app the
   bench lacks: ``apps update`` stops in the preflight with nothing pulled (HEAD
   unchanged) and names ``--with-deps``. The planted app is a plain git checkout,
   never pip-installed or registered, so the bench cannot be poisoned by it.
"""

from __future__ import annotations

import shlex

import pytest

from . import harness

pytestmark = pytest.mark.e2e

HRMS = "https://github.com/frappe/hrms"
PLANTED = "cwe2e_depsupdate"
PLANTED_REMOTE = "/tmp/cwe2e-depsupdate.git"


def _sh(inst, script: str) -> str:
    code, out = harness.exec_in_frappe(inst.name, script, workdir=inst.bench)
    assert code == 0, f"in-container step failed ({code}): {script}\n{out}"
    return out


def _major(inst) -> int:
    return int(
        _sh(inst, "grep -m1 __version__ apps/frappe/frappe/__init__.py").split('"')[1].split(".")[0]
    )


def test_an_app_built_for_another_frappe_is_refused_before_anything_is_fetched(
    running_instance,
):
    inst = running_instance
    major = _major(inst)
    other = "version-16" if major == 15 else "version-15"
    assert harness.exec_in_frappe(inst.name, "test -e apps/hrms", workdir=inst.bench)[0] != 0
    result = harness.run_cwcli(
        "apps", "install", inst.name, HRMS, "--branch", other, "--site", inst.site, timeout=600
    )
    text = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr))
    assert result.returncode == 1, text
    assert "Nothing was fetched or installed" in text, text
    assert "needs frappe" in text and "--ignore-frappe-range" in text, text
    # Refused BEFORE the fetch: the bench is exactly as it was.
    assert harness.exec_in_frappe(inst.name, "test -e apps/hrms", workdir=inst.bench)[0] != 0


def test_axi_apps_plan_orders_the_required_app_first_and_changes_nothing(running_instance):
    inst = running_instance
    major = _major(inst)
    if major < 15:
        pytest.skip("hrms declares frappe/erpnext in required_apps from version-15 on")
    before = _sh(inst, "ls -1 apps")
    result = harness.run_cwcli(
        "axi", "apps", "plan", inst.name, HRMS, "--branch", f"version-{major}", "--site", inst.site,
        timeout=600,
    )
    out = result.stdout
    assert "plan[" in out, out + result.stderr
    rows = [line.strip() for line in out.splitlines() if line.startswith("  ") and "," in line]
    apps = [row.split(",")[1] for row in rows if row.split(",")[0].isdigit()]
    if "erpnext" not in _sh(inst, "ls -1 apps").split():
        assert apps.index("erpnext") < apps.index("hrms"), out
        assert "needs_consent: true" in out, out
    assert _sh(inst, "ls -1 apps") == before  # a read: nothing landed


def _plant_app_whose_tip_requires_another(inst) -> str:
    """A git checkout whose remote's newer commit adds required_apps. Returns HEAD."""
    git = "git -c user.name=cwe2e -c user.email=cwe2e@example.invalid"
    _sh(
        inst,
        f"set -e; rm -rf {PLANTED_REMOTE} apps/{PLANTED}; src=$(mktemp -d); "
        f"mkdir -p $src/{PLANTED}; "
        f"printf '__version__ = \"0.0.1\"\\n' > $src/{PLANTED}/__init__.py; "
        f"printf 'app_name = \"{PLANTED}\"\\n' > $src/{PLANTED}/hooks.py; "
        f": > $src/{PLANTED}/modules.txt; : > $src/{PLANTED}/patches.txt; "
        f"printf '[project]\\nname = \"{PLANTED}\"\\nversion = \"0.0.1\"\\n' > $src/pyproject.toml; "
        f"{git} -C $src init -q -b develop; {git} -C $src add -A; {git} -C $src commit -qm one; "
        f"git clone -q --bare $src {PLANTED_REMOTE}; "
        f"git clone -q --origin upstream {PLANTED_REMOTE} apps/{PLANTED}; "
        f"printf 'app_name = \"{PLANTED}\"\\nrequired_apps = [\"cwe2e_not_on_this_bench\"]\\n' "
        f"> $src/{PLANTED}/hooks.py; {git} -C $src commit -qam requires; "
        f"{git} -C $src push -q {PLANTED_REMOTE} develop; rm -rf $src",
    )
    return _sh(inst, f"git -C apps/{PLANTED} rev-parse HEAD").strip()


def test_an_update_whose_new_code_needs_another_app_stops_before_the_pull(running_instance):
    inst = running_instance
    try:
        head = _plant_app_whose_tip_requires_another(inst)
        result = harness.run_cwcli(
            "apps", "update", inst.name, PLANTED, "--no-recache", "--yes", timeout=600
        )
        text = harness.collapse_ws(harness.strip_ansi(result.stdout + result.stderr))
        assert result.returncode == 1, text
        assert "cwe2e_not_on_this_bench" in text and "--with-deps" in text, text
        # Nothing was pulled: the checkout is still at the old commit.
        assert _sh(inst, f"git -C apps/{PLANTED} rev-parse HEAD").strip() == head
        doc = harness.run_cwcli("axi", "apps", "update", inst.name, PLANTED, "--no-recache")
        assert doc.returncode == 1, doc.stdout
        assert "preflight_blocked: true" in doc.stdout, doc.stdout
        assert "app.required_app_missing" in doc.stdout, doc.stdout
    finally:
        harness.exec_in_frappe(
            inst.name,
            f"rm -rf apps/{shlex.quote(PLANTED)} {shlex.quote(PLANTED_REMOTE)}",
            workdir=inst.bench,
        )
