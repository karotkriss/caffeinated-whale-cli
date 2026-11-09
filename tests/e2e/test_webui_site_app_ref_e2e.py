"""New site's app picker takes a branch per app, and the site gets the app AT it, in a
REAL browser against a real bench.

The picker used to list apps with no way to say which branch, so a remote pick always
landed at the branch the plan chose (``version-N``). Here ``payments`` is picked at
``version-N-hotfix`` - a branch the plan never picks by itself and one that fits the
bench's Frappe - and the created site must hold payments with the bench's copy on
exactly that branch: once picked from the app sources (fetched at the ref), and once
picked from a bench that already holds it at ``version-N`` (checked out at the ref
first, which moves it for every site on the bench).
"""

from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import app_catalog

from . import harness
from .browser import open_signed_in
from .dialogs import DAEMON_TOKEN, PASSWORD, open_dialog, page, press, settled

pytestmark = [pytest.mark.e2e]

APP = "payments"
KEY = "github.com/frappe/payments"
URL = f"https://{KEY}"
SITE = "refpick.localhost"
# The last open dialog: the picker opens on top of New site.
_TOP = "[...document.querySelectorAll('[role=dialog]')].at(-1)"


def _ref_field(value: str) -> str:
    return f"document.querySelector('[data-ref-for=\"{value}\"] input[role=combobox]')"


def _seed_catalog_with_payments() -> None:
    """The session's isolated home has no app sources: record the one remote app the picker
    needs, in the catalog's own on-disk format. A fresh ``scanned_at`` keeps the daemon from
    starting its startup scan, which would replace this entry mid-test with whatever the
    runner's GitHub token can see."""
    path = Path(os.environ["CWCLI_HOME"]) / "cache" / "app-catalog" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": app_catalog.CATALOG_VERSION,
                "scanned_at": time.time(),
                "remote": {
                    KEY: {
                        "name": APP,
                        "title": "Payments",
                        "source": {
                            "kind": "github",
                            "host": "github.com",
                            "url": URL,
                            "default_branch": "develop",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _sh(inst, script: str) -> tuple[int, str]:
    return harness.exec_in_frappe(inst.name, script, workdir=inst.bench)


def _has_app_dir(inst) -> bool:
    return _sh(inst, f"test -d apps/{APP}")[0] == 0


def _replace_text(chrome, field: str, text: str) -> None:
    chrome.eval(
        f"(() => {{ const i = {field}; i.focus(); i.select();"
        f" return document.execCommand('insertText', false, {json.dumps(text)}) }})()"
    )


def _restore(inst) -> None:
    """Leave the shared bench as it was: drop the site, remove the fetched app."""
    if _sh(inst, f"test -d sites/{SITE}")[0] == 0:
        harness.run_cwcli("rm-site", inst.name, SITE, "--yes", timeout=600)
    if _has_app_dir(inst):
        _sh(inst, f"bench remove-app --no-backup {APP}")
    harness.run_cwcli("start", inst.name, "--yes")


@pytest.fixture()
def ui_with_instance(running_instance):
    if harness.FRAPPE_MAJOR < 15:
        pytest.skip(
            "payments has no version-14 hotfix branch: no non-default branch fits a v14 bench"
        )
    _seed_catalog_with_payments()
    with harness.ui_daemon(DAEMON_TOKEN) as base:
        yield base, running_instance


def _pick_at_ref(chrome, base, inst, value: str, ref: str) -> None:
    """Open New site, pick payments (the picker row ``value``) at ``ref`` in the picker."""
    field = _ref_field(value)
    open_signed_in(chrome, base, PASSWORD)
    chrome.viewport(1280, 800)
    page(chrome, base, f"/instances/{inst.name}/benches/0", "Sites")
    press(chrome, "[role=tab]", "Sites")
    chrome.wait(
        "[...document.querySelectorAll('button')].some((b) => b.getClientRects().length"
        " && b.textContent.trim().startsWith('New site'))",
        desc="the Sites tab with its New site button",
    )
    open_dialog(chrome, ("button", "New site"), "New site", "New site")
    chrome.wait(
        "[...document.querySelectorAll('[role=dialog] button')].some((b) => !b.disabled"
        " && b.textContent.trim() === 'Choose apps')",
        desc="the bench's apps read, Choose apps enabled",
    )
    press(chrome, "[role=dialog] button", "Choose apps")
    chrome.wait(
        f"({_TOP})?.textContent.includes('Apps to install on the new site')",
        desc="the app picker",
    )
    settled(chrome)
    # Before the pick, no ref field; picking the app adds one for it.
    assert not chrome.eval(f"!!{field}")
    # The row's text starts with its icon's letter, so match the app by inclusion.
    assert chrome.eval(
        f"(() => {{ const el = [...{_TOP}.querySelectorAll('[role=checkbox]')]"
        ".find((e) => e.textContent.toLowerCase().includes('payments')); if (!el) return false;"
        " for (const t of ['mousedown', 'mouseup']) el.dispatchEvent(new MouseEvent(t, {button: 0, bubbles: true, cancelable: true}));"
        " el.click(); return true })()"
    ), "no Payments row in the picker"
    chrome.wait(f"!!{field}", desc="the branch field of the picked app")
    chrome.wait(
        "performance.getEntriesByType('resource')"
        ".some((e) => e.name.includes('/apps/refs?url=') && e.name.includes('payments'))",
        desc="the field asked for the repository's refs",
    )
    _replace_text(chrome, field, ref)
    chrome.press("Tab")
    chrome.wait("!document.querySelector('[role=listbox]')", desc="the ref list closed")
    assert chrome.eval(f"{field}.value") == ref
    press(chrome, "[role=dialog] button", "Use 1 app")  # only the picker has it
    chrome.wait(
        f"document.querySelectorAll('[role=dialog]').length === 1"
        f" && document.querySelector('[role=dialog]').textContent.includes({json.dumps(ref)})",
        desc="the picker closed with the pick's ref on its chip",
    )


def _create_site(chrome, base, inst) -> dict:
    """Name the site, press Create site, and wait for the new-site job to finish."""
    _replace_text(
        chrome,
        "document.querySelector('[role=dialog] input[placeholder=\"shop.localhost\"]')",
        SITE,
    )
    _replace_text(
        chrome,
        "document.querySelector('[role=dialog] input[type=password]')",
        "refpick-admin-pw",
    )
    chrome.wait(
        "[...document.querySelectorAll('[role=dialog] button')].some((b) => !b.disabled"
        " && b.textContent.trim() === 'Create site')",
        desc="Create site enabled",
        timeout=120,
    )
    press(chrome, "[role=dialog] button", "Create site")
    chrome.wait("!document.querySelector('[role=dialog]')", desc="New site closed, the job started")

    def finished():
        _, body = harness.api_call(base, "/api/v1/jobs", token=DAEMON_TOKEN)
        jobs = [
            j
            for j in body.get("data", [])
            if j.get("verb") == "new-site" and j.get("target", {}).get("site") == SITE
        ]
        return jobs[0] if jobs and jobs[0]["state"] not in ("queued", "running") else None

    harness.wait_until(
        lambda: finished() is not None,
        timeout=1500,
        interval=10,
        desc="the new-site job to finish",
    )
    return finished()


def _assert_site_holds_payments_at(inst, ref: str) -> None:
    code, out = _sh(inst, f"bench --site {shlex.quote(SITE)} list-apps")
    assert code == 0 and APP in out, out
    code, branch = _sh(inst, f"git -C apps/{APP} branch --show-current")
    assert code == 0 and branch.strip() == ref, branch


def test_new_site_installs_a_remote_pick_at_the_branch_picked_in_the_picker(
    chrome, ui_with_instance
):
    base, inst = ui_with_instance
    ref = f"version-{harness.FRAPPE_MAJOR}-hotfix"
    assert not _has_app_dir(inst), "fixture already holds payments"
    try:
        _pick_at_ref(chrome, base, inst, f"remote:{KEY}", ref)
        # The plan reads the app at the picked ref, and the command line carries it.
        chrome.wait(
            f"document.querySelector('[role=dialog]').textContent.includes('read at {ref}')",
            desc="the plan read at the picked ref",
            timeout=120,
        )
        assert f"--branch {ref}" in chrome.eval(
            "document.querySelector('[role=dialog]').textContent"
        )
        job = _create_site(chrome, base, inst)
        assert job["state"] == "succeeded", job
        _assert_site_holds_payments_at(inst, ref)
    finally:
        _restore(inst)  # the shared bench is as it was: no site, no payments


def test_new_site_checks_a_bench_app_out_at_the_branch_picked_in_the_picker(
    chrome, ui_with_instance
):
    base, inst = ui_with_instance
    held = f"version-{harness.FRAPPE_MAJOR}"
    ref = f"version-{harness.FRAPPE_MAJOR}-hotfix"
    assert not _has_app_dir(inst), "fixture already holds payments"
    try:
        fetched = harness.run_cwcli(
            "apps", "install", inst.name, URL, "--fetch-only", "--branch", held, timeout=900
        )
        assert fetched.returncode == 0, fetched.stdout + fetched.stderr
        code, branch = _sh(inst, f"git -C apps/{APP} branch --show-current")
        assert code == 0 and branch.strip() == held, branch
        _pick_at_ref(chrome, base, inst, APP, ref)
        # The live plan names the move before anything runs, and the command line carries it.
        chrome.wait(
            "document.querySelector('[role=dialog]').textContent"
            f".includes('Create site first checks out {ref} in the bench\\'s copy of {APP}')",
            desc="the page says the bench's copy moves to the picked ref",
            timeout=120,
        )
        assert f"cwcli apps checkout {inst.name} {APP} {ref}" in chrome.eval(
            "document.querySelector('[role=dialog]').textContent"
        )
        job = _create_site(chrome, base, inst)
        assert job["state"] == "succeeded", job
        _assert_site_holds_payments_at(inst, ref)
    finally:
        _restore(inst)  # the shared bench is as it was: no site, no payments
