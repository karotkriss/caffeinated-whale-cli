"""Instance-backed dialogs stay inside the dialog, in a REAL browser, at desktop and phone widths.

The instance-backed half of ``test_webui_modal_overflow_e2e.py`` (see its docstring for the
bug and the measurement, shared through ``dialogs.py``): new bench, stop, new site, clone or
move sites and clear cache, against the shared session instance.

This module's position is deliberate. pytest collects modules in name order, and this name
sorts after ``test_webui_navigation_e2e.py`` so the case runs AFTER the navigation proof.
On the v14 shared leg the navigation proof failed twice, in two different ways, when this
case ran immediately before it (PR #333), so the two are now separated to tell apart a
navigation defect from an effect of this case.
"""

from __future__ import annotations

import json

import pytest

from . import harness
from .browser import open_signed_in
from .dialogs import (
    DAEMON_TOKEN,
    PASSWORD,
    SIZES,
    check,
    close_dialog,
    open_dialog,
    page,
    press,
    settings,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture()
def ui_with_instance(running_instance):
    with harness.ui_daemon(DAEMON_TOKEN) as base:
        yield base, running_instance.name


@pytest.mark.parametrize("width, height", SIZES)
def test_instance_dialogs_fit_the_dialog(chrome, ui_with_instance, width, height):
    base, project = ui_with_instance
    open_signed_in(chrome, base, PASSWORD)
    chrome.viewport(width, height)
    failures: list[str] = []

    def opened(route: str, opener: tuple, title: str, *, tab: str | None = None) -> None:
        what = f"{width}px {title!r} dialog"
        page(chrome, base, route, opener[1] if tab is None else tab)
        if tab:
            press(chrome, "[role=tab]", tab)
            chrome.wait(
                f"[...document.querySelectorAll('button')].some((b) => b.getClientRects().length"
                f" && b.textContent.trim().startsWith({json.dumps(opener[1])}))",
                desc=f"the {tab} tab with its {opener[1]!r} button",
            )
        open_dialog(chrome, opener, title, what)
        failures.append("\n".join(check(chrome, what)))
        close_dialog(chrome, what)

    instance = f"/instances/{project}"
    opened(instance, ("button", "New bench"), "New bench")
    # The confirmation only; Escape cancels it, so the instance keeps running.
    opened(instance, ("button", "Stop"), "Stop")
    bench = f"{instance}/benches/0"
    opened(bench, ("button", "New site"), "New site", tab="Sites")
    opened(bench, ("button", "Clone or move sites"), "Clone", tab="Sites")
    # A cached project is what makes "Clear everything" live, and inspect caches it.
    inspected = harness.run_cwcli("inspect", project)
    assert inspected.returncode == 0, inspected.stderr
    settings(chrome, base, "cache", project)
    open_dialog(chrome, ("button", "Clear everything"), "Clear the whole cache", "clear cache")
    failures.append("\n".join(check(chrome, f"{width}px clear-cache dialog")))
    close_dialog(chrome, "clear cache")

    assert not any(failures), "\n".join(f for f in failures if f)
