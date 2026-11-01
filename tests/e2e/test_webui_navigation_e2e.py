"""Page navigation in the web UI, in a REAL browser: no frame without a visible page.

Navigating between pages used to go blank: the shell's ``out-in`` route transition faded
the old page out and removed it, then inserted the next page at opacity 0, so every
navigation painted an empty main area for several frames (white in light mode). The fix
holds the old page until the next one has rendered (a View Transition started from the
router, see ``webui/src/premium/page-transition.js``) and keys the page by path so a tab
change keeps it mounted.

Only a browser can show that: a unit test sees neither the render flush nor the frames in
between. This drives headless Chrome through the DevTools protocol over its debugging pipe
(stdlib only, no driver package), against the shipped ``cwcli ui`` daemon and the real
instance, samples the content column on EVERY animation frame across each main-page
navigation, and fails on any frame whose page is absent or faded. It runs at the desktop
and phone widths and once under reduced motion, where no transition plays and the swap
must be atomic.
"""

from __future__ import annotations

import json
import time

import pytest

from . import harness
from .browser import Chrome, open_signed_in

pytestmark = [pytest.mark.e2e]

_TOKEN = "cwe2e-webui-nav-token"
_PASSWORD = "cwe2e-webui-nav-password"

# Sampled on every animation frame: the first text in the content column and the opacity
# it is painted at (its own and every ancestor's up to the column). A frame with no text,
# or text under 0.9 opacity, is a frame the user sees as blank or fading.
_SAMPLER = """
(() => {
  const nav = (window.__nav = { on: false, frames: 0, bad: [] })
  const column = () => document.querySelector('[data-slot="desktop-shell-content"]')
  const visible = () => {
    const root = column()
    if (!root) return false
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.textContent.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
    })
    const text = walker.nextNode()
    if (!text) return false
    let opacity = 1
    for (let el = text.parentElement; el && el !== root; el = el.parentElement) {
      const cs = getComputedStyle(el)
      if (cs.display === 'none' || cs.visibility === 'hidden') return false
      opacity *= parseFloat(cs.opacity)
    }
    return opacity >= 0.9
  }
  const tick = () => {
    if (nav.on) {
      nav.frames += 1
      if (!visible()) nav.bad.push(nav.frames)
    }
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  return 'installed'
})()
"""


def run_proof(
    chrome: Chrome,
    base: str,
    project: str,
    *,
    width: int,
    height: int,
    reduced_motion: bool,
) -> None:
    """Drive every main page in turn and fail on any frame without a visible page."""
    open_signed_in(chrome, base, _PASSWORD)
    chrome.viewport(width, height)
    chrome.send(
        "Emulation.setEmulatedMedia",
        {
            "features": [
                {
                    "name": "prefers-reduced-motion",
                    "value": "reduce" if reduced_motion else "no-preference",
                }
            ]
        },
        page=True,
    )
    chrome.navigate(base + "/instances")
    instance_href = f"/instances/{project}"
    chrome.wait(
        f"!!document.querySelector('a[href={json.dumps(instance_href)}]')",
        desc="the Instances page with the session instance listed",
    )
    assert chrome.eval(_SAMPLER) == "installed"
    if reduced_motion:
        assert chrome.eval("matchMedia('(prefers-reduced-motion: reduce)').matches") is True

    def navigate(selector: str, arrives: str, desc: str) -> None:
        chrome.eval("(() => { __nav.frames = 0; __nav.bad = []; __nav.on = true })()")
        chrome.click(selector)
        chrome.wait(arrives, desc=desc)
        time.sleep(0.5)  # past the page's first data read
        # Wall time alone does not cover the cross-fade or enough frames on a slow runner,
        # and a click while a view transition still runs is dropped by the browser (the next
        # step's click then never arrives), so wait for both conditions themselves.
        chrome.wait(
            "!document.activeViewTransition && __nav.frames >= 10",
            desc=f"{desc}: the cross-fade to end and 10 frames sampled",
        )
        got = chrome.eval(
            "(() => { __nav.on = false; return { frames: __nav.frames, bad: __nav.bad } })()"
        )
        assert (
            got["bad"] == []
        ), f"{desc}: frames {got['bad']} of {got['frames']} had no visible page"

    at = lambda path: f"location.pathname === {json.dumps(path)}"  # noqa: E731
    bench_href = f"{instance_href}/benches/0"
    steps = [
        (
            # Arrived once the inspect report is in and the tabs (Benches, Sites, Apps,
            # Performance) have rendered; the sampler keeps watching until then.
            f"a[href={json.dumps(instance_href)}]",
            at(instance_href) + " && document.querySelectorAll('[role=tab]').length >= 4",
            "instance overview",
        ),
        ("[role=tab]:nth-of-type(4)", "location.search === '?tab=performance'", "performance tab"),
        ("[role=tab]:nth-of-type(1)", "location.search === ''", "benches tab again"),
        (
            f"a[href^={json.dumps(bench_href)}]",
            at(bench_href) + " && document.querySelectorAll('[role=tab]').length >= 4",
            "bench page",
        ),
        ('a[href="/apps"]', at("/apps"), "apps"),
        ('a[href="/sites"]', at("/sites"), "sites"),
        ('a[href="/jobs"]', at("/jobs"), "jobs"),
        ('a[href="/backups"]', at("/backups"), "backups"),
        ('a[href="/schedules"]', at("/schedules"), "schedules"),
        ('a[href="/advisories"]', at("/advisories"), "advisories"),
        ('a[href="/instances"]', at("/instances"), "back to instances"),
        (f"a[href={json.dumps(instance_href)}]", at(instance_href), "instance again, from cache"),
    ]
    for selector, arrives, desc in steps:
        navigate(selector, arrives, desc)


# Function-scoped like `running_instance` it depends on (a module scope here is a pytest
# ScopeMismatch); the daemon starts in a second, once per case.
@pytest.fixture()
def ui(running_instance):
    with harness.ui_daemon(_TOKEN) as base:
        yield type("UI", (), {"base": base, "project": running_instance.name})


@pytest.mark.parametrize(
    "width, height, reduced_motion",
    [
        pytest.param(1280, 800, False, id="desktop"),
        pytest.param(390, 844, False, id="phone"),
        pytest.param(1280, 800, True, id="desktop-reduced-motion"),
    ],
)
def test_no_navigation_paints_a_frame_without_a_page(chrome, ui, width, height, reduced_motion):
    run_proof(
        chrome, ui.base, ui.project, width=width, height=height, reduced_motion=reduced_motion
    )
