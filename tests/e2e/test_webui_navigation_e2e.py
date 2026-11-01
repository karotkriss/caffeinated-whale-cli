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
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from . import harness

pytestmark = [pytest.mark.e2e]

_TOKEN = "cwe2e-webui-nav-token"
_PASSWORD = "cwe2e-webui-nav-password"
_CHROME = os.environ.get("CWE2E_CHROME") or next(
    (
        p
        for p in map(
            shutil.which, ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        )
        if p
    ),
    None,
)

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


class Chrome:
    """Headless Chrome driven over ``--remote-debugging-pipe`` (fd 3 in, fd 4 out)."""

    def __init__(self, user_data_dir: Path):
        chrome_in_r, self._w = os.pipe()
        self._r, chrome_out_w = os.pipe()

        def _wire():  # runs in the child before exec: the pipes become fds 3 and 4
            a, b = os.dup(chrome_in_r), os.dup(chrome_out_w)
            os.dup2(a, 3)
            os.dup2(b, 4)

        self.proc = subprocess.Popen(
            [
                _CHROME,
                "--headless=new",
                "--remote-debugging-pipe",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={user_data_dir}",
                "--window-size=1280,800",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False,
            preexec_fn=_wire,
        )
        os.close(chrome_in_r)
        os.close(chrome_out_w)
        self._id = 0
        self._replies: dict[int, dict] = {}
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        target = next(
            t for t in self.send("Target.getTargets")["targetInfos"] if t["type"] == "page"
        )
        self.session = self.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )["sessionId"]

    def _read(self) -> None:
        buf = b""
        while True:
            chunk = os.read(self._r, 65536)
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                if "id" in msg:
                    with self._cv:
                        self._replies[msg["id"]] = msg
                        self._cv.notify_all()

    def send(
        self, method: str, params: dict | None = None, *, page: bool = False, timeout: float = 30
    ) -> dict:
        self._id += 1
        msg: dict = {"id": self._id, "method": method, "params": params or {}}
        if page:
            msg["sessionId"] = self.session
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._id not in self._replies:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no reply to {method} within {timeout}s")
                self._cv.wait(left)
            reply = self._replies.pop(self._id)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def eval(self, expression: str, *, timeout: float = 30):
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            page=True,
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "evaluate failed"))
        return result["result"].get("value")

    def wait(self, expression: str, *, timeout: float = 30, desc: str = "page condition"):
        # Each poll's evaluate is bounded on its own: a reply lost around a page load (seen
        # once right after the first navigation) is retried within the wait instead of
        # consuming it whole.
        def poll():
            try:
                return self.eval(expression, timeout=5)
            except TimeoutError:
                return False

        return harness.wait_until(poll, timeout=int(timeout), interval=0.1, desc=desc)

    def click(self, selector: str) -> None:
        """A real mouse press on the element's centre (reka-ui tabs switch on pointerdown,
        which a synthetic ``click()`` never produces)."""
        box = self.eval(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return null;"
            " el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width / 2, y: r.top + r.height / 2} })()"
        )
        assert box, f"nothing matches {selector!r}"
        for kind in ("mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1},
                page=True,
            )

    def close(self) -> None:
        try:
            self.send("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001 - best effort, the kill below is the guarantee
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    if _CHROME is None:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail(
                "no Chrome binary on this runner; the navigation proof needs a real browser"
            )
        pytest.skip("no Chrome binary found (set CWE2E_CHROME)")
    browser = Chrome(tmp_path_factory.mktemp("cwe2e-chrome"))
    try:
        yield browser
    finally:
        browser.close()


def _browser_session(base: str) -> tuple[str, str]:
    """Sign in the way the page does and return the session cookie (name, value).

    The account is reset to the default password first, because the session's
    ``CWCLI_HOME`` is shared and an earlier sign-in may have changed it.
    """
    reset = harness.run_cwcli("admin", "reset-password")
    assert reset.returncode == 0, reset.stderr
    req = urllib.request.Request(
        base + "/api/v1/session",
        data=json.dumps({"username": "admin", "password": "admin", "remember": False}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback
        name, value = resp.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)
    req = urllib.request.Request(
        base + "/api/v1/session/password",
        data=json.dumps({"new": _PASSWORD, "current": "admin"}).encode(),
        headers={"Content-Type": "application/json", "Cookie": f"{name}={value}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback
        assert resp.status == 200
    return name, value


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
    chrome.send("Network.enable", page=True)
    name, value = _browser_session(base)
    chrome.send("Network.setCookie", {"name": name, "value": value, "url": base}, page=True)
    chrome.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 640},
        page=True,
    )
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
    chrome.send("Page.navigate", {"url": base + "/instances"}, page=True)
    instance_href = f"/instances/{project}"
    chrome.wait(
        f"!!document.querySelector('a[href={json.dumps(instance_href)}]')",
        desc="the Instances page with the session instance listed",
    )
    assert chrome.eval(_SAMPLER) == "installed"
    if reduced_motion:
        assert chrome.eval("matchMedia('(prefers-reduced-motion: reduce)').matches") is True

    def navigate(selector: str, located: str, ready: str | None, desc: str) -> None:
        chrome.eval("(() => { __nav.frames = 0; __nav.bad = []; __nav.on = true })()")
        chrome.click(selector)
        # The press's own effect first: the router changes the location as soon as the
        # route is resolved, before any data read, so a dropped or mis-aimed press fails
        # here as that, never as a slow first read of the page's data (which is `ready`).
        chrome.wait(located, desc=f"{desc}: the press to change the location")
        if ready:
            chrome.wait(ready, desc=f"{desc}: the page's data")
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
    for selector, located, ready, desc in steps:
        navigate(selector, located, ready, desc)


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
