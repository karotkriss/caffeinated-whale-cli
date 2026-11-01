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

A press made while a transition runs must reach the live page: Chrome aims it at ``<html>``
while the page is a snapshot, so the product hands it over (``webui/src/premium/
press-through.js``), and a second test presses in the sidebar mid cross-fade and inside the
open Appearance dialog mid scheme wipe.
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
# or text under 0.9 opacity, is a frame the user sees as blank or fading. While a view
# transition runs it also records the latest end time of the transition's own animations,
# which is how long the transition lasts.
_SAMPLER = """
(() => {
  const nav = (window.__nav = { on: false, frames: 0, bad: [], vt: 0 })
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
      if (document.activeViewTransition) {
        for (const a of document.documentElement.getAnimations({ subtree: true })) {
          if (a.effect?.pseudoElement?.startsWith('::view-transition'))
            nav.vt = Math.max(nav.vt, a.effect.getComputedTiming().endTime)
        }
      }
    }
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  return 'installed'
})()
"""


def _open_instances(
    chrome: Chrome, base: str, project: str, *, width: int, height: int, reduced_motion: bool
) -> str:
    """Sign in, size the page, open the Instances page and return the instance's href."""
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
    return instance_href


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
    instance_href = _open_instances(
        chrome, base, project, width=width, height=height, reduced_motion=reduced_motion
    )
    assert chrome.eval(_SAMPLER) == "installed"
    if reduced_motion:
        assert chrome.eval("matchMedia('(prefers-reduced-motion: reduce)').matches") is True

    # Each wait has its own budget, for what it waits on. The location changes once the
    # router has resolved the route, which the first time includes fetching the page's lazy
    # chunk from the loopback daemon (measured 0.4 to 2.1 s): 10 s fails a dropped or
    # mis-aimed press fast, and as that. The page's data is the daemon's first inspect read,
    # which on a cold cache execs into the container: it keeps the 30 s the step always had.
    located_s, ready_s = 10, 30

    def waited(condition: str, budget: int, desc: str) -> None:
        """``chrome.wait``, and on a timeout a failure that names its cause: where the page
        is, whether a transition is still running, how many frames it painted, and how the
        daemon answers the instance's detail read right now."""
        try:
            chrome.wait(condition, timeout=budget, desc=desc)
        except TimeoutError as exc:
            try:
                page = chrome.eval(
                    "({ href: location.href, transition: !!document.activeViewTransition,"
                    " frames: __nav.frames })",
                    timeout=5,
                )
            except Exception as err:  # noqa: BLE001 - a hung page is itself the answer
                page = f"unreadable ({err})"
            t0 = time.monotonic()
            status, _ = harness.api_call(base, f"/api/v1/instances/{project}", token=_TOKEN)
            took = (time.monotonic() - t0) * 1000
            raise AssertionError(
                f"{exc}; page {page}; GET /api/v1/instances/{project} answered {status}"
                f" in {took:.0f} ms"
            ) from None

    def navigate(selector: str, located: str, ready: str | None, desc: str) -> float:
        chrome.eval("(() => { __nav.frames = 0; __nav.bad = []; __nav.vt = 0; __nav.on = true })()")
        chrome.click(selector)
        # The press's own effect first: the router changes the location as soon as the
        # route is resolved, before any data read, so a dropped or mis-aimed press fails
        # here as that, never as a slow first read of the page's data (which is `ready`).
        waited(located, located_s, f"{desc}: the press to change the location")
        if ready:
            waited(ready, ready_s, f"{desc}: the page's data")
        time.sleep(0.5)  # past the page's first data read
        # Wall time alone does not cover the cross-fade or enough frames on a slow runner,
        # so wait for both conditions themselves.
        chrome.wait(
            "!document.activeViewTransition && __nav.frames >= 10",
            desc=f"{desc}: the cross-fade to end and 10 frames sampled",
        )
        got = chrome.eval(
            "(() => { __nav.on = false;"
            " return { frames: __nav.frames, bad: __nav.bad, vt: __nav.vt } })()"
        )
        assert (
            got["bad"] == []
        ), f"{desc}: frames {got['bad']} of {got['frames']} had no visible page"
        # premium.css's page cross-fade: every animation of the transition, the group's
        # included, must end with it, or the transition runs longer than designed.
        assert got["vt"] <= 160, f"{desc}: the page transition ran {got['vt']} ms, over 160"
        return got["vt"]

    at = lambda path: f"location.pathname === {json.dumps(path)}"  # noqa: E731
    bench_href = f"{instance_href}/benches/0"
    tabs = "document.querySelectorAll('[role=tab]').length >= 4"
    steps = [
        # (press target, the location the press lands on, the page's data if it reads any)
        # An instance or bench page is ready once the inspect report is in and the tabs
        # (Benches, Sites, Apps, Performance) have rendered; the sampler watches until then.
        (f"a[href={json.dumps(instance_href)}]", at(instance_href), tabs, "instance overview"),
        (
            "[role=tab]:nth-of-type(4)",
            "location.search === '?tab=performance'",
            None,
            "performance tab",
        ),
        ("[role=tab]:nth-of-type(1)", "location.search === ''", None, "benches tab again"),
        (f"a[href^={json.dumps(bench_href)}]", at(bench_href), tabs, "bench page"),
        ('a[href="/apps"]', at("/apps"), None, "apps"),
        ('a[href="/sites"]', at("/sites"), None, "sites"),
        ('a[href="/jobs"]', at("/jobs"), None, "jobs"),
        ('a[href="/backups"]', at("/backups"), None, "backups"),
        ('a[href="/schedules"]', at("/schedules"), None, "schedules"),
        ('a[href="/advisories"]', at("/advisories"), None, "advisories"),
        ('a[href="/instances"]', at("/instances"), None, "back to instances"),
        (
            f"a[href={json.dumps(instance_href)}]",
            at(instance_href),
            None,
            "instance again, from cache",
        ),
    ]
    longest = max(
        navigate(selector, located, ready, desc) for selector, located, ready, desc in steps
    )
    if reduced_motion:
        assert longest == 0, f"a view transition ran under reduced motion ({longest} ms)"
    else:
        assert longest > 0, "no page transition was measured on any navigation"


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


# Installed before any transition starts, so it runs ahead of the page's own listeners:
# whether each real press arrived while a view transition was running.
_PRESS_RECORDER = """
(() => {
  window.__presses = []
  addEventListener('pointerdown', (e) => {
    if (e.isTrusted) __presses.push(!!document.activeViewTransition)
  }, true)
  return 'installed'
})()
"""

# Resolves 50 ms after a view transition has started (false if none starts within 10 s).
_INTO_TRANSITION = """
new Promise((resolve) => {
  const until = performance.now() + 10000
  const poll = () => document.activeViewTransition ? setTimeout(() => resolve(true), 50)
    : performance.now() > until ? resolve(false) : setTimeout(poll, 5)
  poll()
})
"""


def _press_mid_transition(chrome: Chrome, start: str, point: dict, desc: str) -> None:
    """Press ``start`` to begin a transition, then press ``point`` 50 ms into it."""
    chrome.click(start)
    assert chrome.eval(_INTO_TRANSITION) is True, f"{desc}: no view transition started"
    chrome.eval("__presses = []")
    chrome.press_at(point)
    assert chrome.eval("__presses") == [True], f"{desc}: the press did not land mid-transition"


def test_a_press_during_a_transition_reaches_the_live_page(chrome, ui):
    """A real press made while a view transition runs lands on the element under the
    pointer, never on ``<html>``, where it was lost and where an open dialog took it for a
    press outside itself and closed."""
    _open_instances(chrome, ui.base, ui.project, width=1280, height=800, reduced_motion=False)
    assert chrome.eval(_PRESS_RECORDER) == "installed"
    # Slowed so the press lands well inside the transition however busy the runner is; the
    # recorder still checks that it did.
    chrome.send("Animation.enable", page=True)
    chrome.send("Animation.setPlaybackRate", {"playbackRate": 0.25}, page=True)
    try:
        # A quick second press in the sidebar, which the old and the new page share.
        sites = chrome.stable_point('a[href="/sites"]')
        _press_mid_transition(chrome, 'a[href="/apps"]', sites, "page cross-fade")
        chrome.wait(
            "location.pathname === '/sites'",
            timeout=10,
            desc="the press made mid cross-fade to open Sites",
        )
        # A press mid cross-fade on a text field of the page being opened leaves it focused
        # and ready to type, as a native press would. Backups' search field sits below
        # content drawn from the store snapshot alone, which changes only after a job and
        # outlives the page: once the snapshot is in (no summary value still reads '…'),
        # the field is where it is measured, on this visit and on the next.
        backups, search = (
            'a[href="/backups"]',
            'input[placeholder="Search sites, instances, notes"]',
        )
        chrome.click(backups)
        chrome.wait(
            f"!!document.querySelector({json.dumps(search)})"
            " && [...document.querySelectorAll('.text-2xl')].length >= 4"
            " && [...document.querySelectorAll('.text-2xl')].every((e) => e.textContent.trim() !== '…')",
            timeout=10,
            desc="Backups open with the store snapshot in",
        )
        field = chrome.stable_point(search)
        chrome.click('a[href="/sites"]')
        chrome.wait(
            "location.pathname === '/sites' && !document.activeViewTransition",
            timeout=10,
            desc="Sites open again",
        )
        _press_mid_transition(chrome, backups, field, "cross-fade onto a text field")
        chrome.wait(
            f"document.activeElement === document.querySelector({json.dumps(search)})",
            timeout=10,
            desc="the field pressed mid cross-fade to have focus",
        )
        chrome.send("Input.insertText", {"text": "cw"}, page=True)
        assert chrome.eval(f"document.querySelector({json.dumps(search)}).value") == "cw"
        # A press inside the open Appearance dialog while the scheme wipe runs.
        light = '[aria-label="Colour scheme"] [role=radio]:nth-of-type(1)'
        dark = '[aria-label="Colour scheme"] [role=radio]:nth-of-type(2)'
        chrome.click('[data-slot="sidebar-item"]:has(.cw-scheme-toggle) > button')
        chrome.wait(f"!!document.querySelector({json.dumps(light)})", desc="the dialog open")
        chrome.click(light)
        painted_light = (
            "!document.activeViewTransition"
            " && document.documentElement.getAttribute('data-theme') === 'light'"
        )
        chrome.wait(painted_light, timeout=10, desc="the page painted light")
        _press_mid_transition(chrome, dark, chrome.stable_point(light), "scheme wipe")
        chrome.wait(painted_light, timeout=10, desc="the press made mid wipe to choose Light")
        assert chrome.eval(f"!!document.querySelector({json.dumps(light)})"), "the dialog closed"
    finally:
        chrome.send("Animation.setPlaybackRate", {"playbackRate": 1}, page=True)
