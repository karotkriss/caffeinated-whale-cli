"""Dialog content stays inside the dialog, in a REAL browser, at desktop and phone widths.

Settings > Integrations used to grow wider than its modal once the Remote hosts over
SSH card showed the hub's public key and its ``cwcli remote allow`` command: frappe-ui's
tab panel is a flex-row item with no ``min-width: 0``, so one unbreakable line set the
panel's width, the dialog clipped the right edge, the Integrations intro lost its last
words and the cards' buttons ("Settings…", "Add from a repository…") were cut off. On a
phone the tab root was wider than the dialog from the start (``w-screen`` inside a dialog
with margins). The fix is shared dialog layout (``webui/src/style.css``, the local
``SettingsRow``/``SettingsHeader`` wrappers), not one card.

Only a browser can measure that, so this drives headless Chrome against the shipped
``cwcli ui`` daemon, seeds real long values where the page shows user data (the hub key
and allow command, a long search path, a long app source, a long remote host address,
long browser user agents), opens every Settings pane and every dialog reachable without
an instance, and fails when any element inside the topmost dialog lies past the
dialog's horizontal edges or when a control is clipped by an inner scroll region.
Each pane and dialog is measured twice: as served, and again with an unbreakable
token appended to its text so a long value anywhere in it is covered, not only where
the seeds land. The instance-backed dialogs (new bench, new site, clone or move sites,
stop, clear cache) are a second case on the shared session instance.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request

import pytest

from . import harness
from .browser import Chrome, open_signed_in

pytestmark = [pytest.mark.e2e]

_TOKEN = "cwe2e-webui-dialogs-token"
_PASSWORD = "cwe2e-webui-dialogs-password"
SIZES = [pytest.param(1280, 800, id="desktop"), pytest.param(390, 844, id="phone")]

# Real long values, each where the page shows user data; none has a break opportunity.
LONG_PATH = "/workspace/" + "benches-with-an-unusually-long-directory-name-" * 3 + "0123456789"
LONG_SOURCE = "github:" + "an-organisation-with-an-unusually-long-name-" * 2 + "0123456789"
LONG_ADDRESS = "a-remote-machine-with-an-unusually-long-hostname-label.internal.example.invalid"
# The daemon keeps the first 80 characters of a user agent, so the marker leads.
LONG_AGENT = (
    "ExampleCorporateBrowserBuild/2026.11.01.0123456789 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
# Appended to every wrapping text node of a pane or dialog for its second measurement.
TOKEN = "ssh-ed25519-" + "AAAAC3NzaC1lZDI1NTE5" * 5

# The topmost open dialog: frappe-ui marks every dialog's content box data-slot=content;
# the Settings modal sits one z-index layer below the dialogs it opens (style.css).
_TOP = """
  const zOf = (el) => { const sc = el.closest('.dialog-scroll-container'); return sc ? Number(getComputedStyle(sc).zIndex) || 0 : 0 }
  const dialogs = [...document.querySelectorAll('[data-slot="content"]')].filter((d) => d.getClientRects().length)
  const top = dialogs.length ? dialogs.reduce((a, b) => (zOf(b) >= zOf(a) ? b : a)) : null
"""

# Everything inside the dialog's box, horizontally; every control also inside each
# horizontally clipping ancestor (a code block that scrolls sideways is allowed).
_MEASURE = (
    "(() => {"
    + _TOP
    + """
  if (!top) return null
  const box = top.getBoundingClientRect()
  const title = (top.querySelector('h1, h2, h3, header')?.textContent || '').trim()
  const name = (el) => `<${el.tagName.toLowerCase()}${typeof el.className === 'string' && el.className ? '.' + el.className.split(' ').slice(0, 3).join('.') : ''}>`
  const describe = (el, r) => `${name(el)} left=${Math.round(r.left)} right=${Math.round(r.right)} "${(el.textContent || '').trim().slice(0, 40)}"`
  const clips = (el) => ['hidden', 'clip', 'auto', 'scroll'].includes(getComputedStyle(el).overflowX)
  const past = []
  const hidden = []
  for (const el of top.querySelectorAll('*')) {
    const r = el.getBoundingClientRect()
    if (r.width <= 1 || r.height <= 1) continue  // not rendered, or screen-reader only
    if (el.closest('pre') && el.tagName !== 'PRE') continue
    if (r.right > box.right + 1 || r.left < box.left - 1) past.push(describe(el, r))
    if (!el.matches('button, a, input, select, textarea, [role="button"]')) continue
    for (let p = el.parentElement; p && p !== top; p = p.parentElement) {
      if (!clips(p) || p.tagName === 'PRE') continue
      const pr = p.getBoundingClientRect()
      if (r.right > pr.right + 1 || r.left < pr.left - 1) {
        hidden.push(`${describe(el, r)} clipped by ${name(p)} right=${Math.round(pr.right)}`)
        break
      }
    }
  }
  return { title, count: dialogs.length, left: Math.round(box.left), right: Math.round(box.right), past, hidden }
})()
"""
)

# Appends TOKEN to every text node that can wrap and is not a control, a tab, a heading
# or preformatted text; _RESTORE puts the text back. (reka marks everything outside the
# topmost dialog aria-hidden, the wrapper it hangs from included, so only an aria-hidden
# element INSIDE the dialog excludes its text.)
_SEED = (
    "(() => {"
    + _TOP
    + """
  if (!top) return 0
  const skip = 'button, a, label, input, textarea, select, option, [role="tab"], [role="tablist"], h1, h2, h3, svg, pre, [aria-hidden="true"], .sr-only'
  const walker = document.createTreeWalker(top, NodeFilter.SHOW_TEXT)
  const seeded = (window.__seeded = [])
  let node
  while ((node = walker.nextNode())) {
    const el = node.parentElement
    if (!node.textContent.trim() || !el) continue
    const hit = el.closest(skip)
    if (hit && top.contains(hit)) continue
    const ws = getComputedStyle(el).whiteSpace
    if (ws.startsWith('nowrap') || ws.startsWith('pre')) continue
    seeded.push([node, node.textContent])
    node.textContent = node.textContent + ' ' + __TOKEN__
  }
  return seeded.length
})()
""".replace(
        "__TOKEN__", json.dumps(TOKEN)
    )
)
_RESTORE = (
    "(() => { for (const [n, t] of window.__seeded || []) n.textContent = t;"
    " window.__seeded = []; return true })()"
)
_COUNT = "document.querySelectorAll('[data-slot=\"content\"]').length"
_PANEL = "document.querySelector('[role=\"tabpanel\"]:not([hidden])')"


def _panel_shows(text: str) -> str:
    return f"(() => {{ const p = {_PANEL}; return !!p && p.textContent.includes({json.dumps(text)}) }})()"


# Every Settings section in sidebar order: (section, tab label, what shows once its data is in).
PANES = [
    ("paths", "Search paths", LONG_PATH),
    ("cache", "Cache", "Remembered projects"),
    ("daemons", "Background daemons", "Persistent credential bridge"),
    ("sources", "App sources", LONG_SOURCE),
    ("agents", "Agent hooks", "Install or repair"),
    ("integrations", "Integrations", "Hub key: ssh-ed25519"),
    ("hosts", "Remote hosts", LONG_ADDRESS),
    ("account", "Your account", "ExampleCorporateBrowserBuild"),
    ("appearance", "Appearance", "Appearance"),
    ("health", "Health check", " passed"),
    ("about", "About cwcli", "Config file"),
]

# Dialogs reachable without an instance: (where, how to open it, its title's start).
# `where` is a Settings section or a page route.
DIALOGS = [
    ("integrations", ("button", "Settings…"), "Remote hosts over SSH"),
    ("integrations", ("button", "Turn off"), "Turn off Remote hosts over SSH"),
    ("integrations", ("button", "Add from a repository"), "Add a community integration"),
    ("hosts", ("button", "Add host"), "Add a remote host"),
    ("hosts", ('button[aria-label^="Remove "]', None), "Remove"),
    ("about", ("button", "Update"), "Update cwcli"),
    ("/instances", ("button", "New instance"), "Create an instance"),
    ("/instances", ("button", "Appearance"), "Appearance"),
    ("/schedules", ("button", "New schedule"), "New schedule"),
    ("/schedules", ("button", "Windows and guards"), "Windows and guards"),
    ("/backups", ("button", "Receive"), "Receive a backup"),
    ("/backups", ("button", "Retention and schedule"), "Backup retention"),
    ("/backups", ("button", "Back up a site"), "Back up"),
]


def _api(base: str, path: str, payload: dict) -> dict:
    status, body = harness.api_call(base, path, payload, token=_TOKEN)
    assert status == 200, f"POST {path} -> {status}: {body}"
    return body["data"]


def _seed(base: str, tmp_path) -> dict:
    """Long values where Settings shows user data; returns what to undo."""
    data = _api(
        base,
        "/api/v1/integrations",
        {
            "op": "configure",
            "id": "remote_ssh",
            "settings": {"hub_name": "hub-example"},
            "secrets": {},
            "enable": True,
        },
    )
    assert data["integration"]["state"] == "on", data
    notices = " ".join(data["integration"]["notices"])
    assert "Hub key: ssh-ed25519" in notices and "cwcli remote allow --hub" in notices, notices
    _api(base, "/api/v1/settings", {"setting": "paths.add", "path": LONG_PATH})
    _api(base, "/api/v1/settings", {"setting": "catalog.source_add", "source": LONG_SOURCE})
    keygen = shutil.which("ssh-keygen")
    assert keygen, "ssh-keygen is needed to make a placeholder host key"
    key = tmp_path / "placeholder_host_key"
    subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    host_key = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    host = _api(
        base,
        "/api/v1/hosts",
        {
            "op": "add",
            "name": "build box",
            "address": LONG_ADDRESS,
            "user": "deploy",
            "port": 22,
            "host_key": host_key,
        },
    )["host"]
    return {"host": host["id"]}


def _unseed(base: str, seeded: dict) -> None:
    """The session home is shared with the other standalone tests."""
    _api(base, "/api/v1/hosts", {"op": "remove", "id": seeded["host"]})
    _api(base, "/api/v1/settings", {"setting": "catalog.source_remove", "source": LONG_SOURCE})
    _api(base, "/api/v1/settings", {"setting": "paths.remove", "path": LONG_PATH})
    _api(base, "/api/v1/integrations", {"op": "disable", "id": "remote_ssh"})


def _seed_sessions(base: str) -> None:
    """Two more browsers signed in, each with a long user agent, for Your account."""
    for suffix in ("desktop", "phone"):
        req = urllib.request.Request(
            base + "/api/v1/session",
            data=json.dumps(
                {"username": "admin", "password": _PASSWORD, "remember": True}
            ).encode(),
            headers={"Content-Type": "application/json", "User-Agent": f"{LONG_AGENT}-{suffix}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback
            assert resp.status == 200


def _check(chrome: Chrome, what: str) -> list[str]:
    """Measure the topmost dialog as served, then with TOKEN appended to its text."""
    _settled(chrome)
    failures = []
    for phase in ("as served", "with a long token in every text"):
        if phase != "as served":
            assert chrome.eval(_SEED) > 0, f"{what}: nothing to seed"
            time.sleep(0.2)
        got = chrome.eval(_MEASURE)
        assert got, f"{what}: no dialog is open"
        for line in got["past"]:
            failures.append(
                f"{what} ({phase}): past the dialog's edges "
                f"[{got['left']}, {got['right']}]: {line}"
            )
        for line in got["hidden"]:
            failures.append(f"{what} ({phase}): control clipped: {line}")
        if phase != "as served":
            chrome.eval(_RESTORE)
    return failures


def _press(chrome: Chrome, selector: str, text: str | None = None) -> None:
    """Press the first visible ``selector`` (whose text starts with ``text``) through
    its own mousedown and click, which reka-ui tabs and frappe-ui buttons both take.
    A pointer press at coordinates that lands during a dialog's enter transition is
    taken as a press outside the dialog and dismisses it, so no coordinates here."""
    found = chrome.eval(
        f"(() => {{ const text = {json.dumps(text)};"
        f" const el = [...document.querySelectorAll({json.dumps(selector)})].find((e) =>"
        " e.getClientRects().length && (text === null || e.textContent.trim().startsWith(text)));"
        " if (!el) return false; el.scrollIntoView({block: 'center'});"
        " for (const type of ['mousedown', 'mouseup']) el.dispatchEvent(new MouseEvent(type, {button: 0, bubbles: true, cancelable: true}));"
        " el.click(); return true })()"
    )
    assert found, f"nothing visible matches {selector!r} {text!r}"


def _open_dialog(chrome: Chrome, opener: tuple, title: str, what: str) -> None:
    before = chrome.eval(_COUNT)
    selector, text = opener
    _press(chrome, selector, text)
    chrome.wait(
        f"(() => {{ const got = {_MEASURE}; return !!got && got.count === {before} + 1"
        f" && got.title.startsWith({json.dumps(title)}) }})()",
        desc=f"{what} open",
    )


def _close_dialog(chrome: Chrome, what: str) -> None:
    before = chrome.eval(_COUNT)
    chrome.press("Escape")
    chrome.wait(f"{_COUNT} === {before} - 1", desc=f"{what} closed again")


def _settled(chrome: Chrome) -> None:
    """Past the open dialog's enter transition: a press that lands during it is taken as
    a press outside the dialog and dismisses it."""
    chrome.wait(
        "[...document.querySelectorAll('[data-slot=\"content\"]')].every("
        "(d) => !d.getClientRects().length || getComputedStyle(d).opacity === '1')",
        desc="the dialog's enter transition to end",
    )
    time.sleep(0.2)


def _settings(chrome: Chrome, base: str, section: str, shows: str) -> None:
    """Open the Settings modal on ``section`` (a deep link) and wait for its data."""
    chrome.navigate(f"{base}/settings/{section}")
    chrome.wait(_panel_shows(shows), desc=f"the {section} section with its data")
    _settled(chrome)


def _page(chrome: Chrome, base: str, route: str, button: str) -> None:
    chrome.navigate(base + route)
    chrome.wait(
        f"[...document.querySelectorAll('button')].some((b) => b.getClientRects().length"
        f" && b.textContent.trim().startsWith({json.dumps(button)}))",
        desc=f"{route} with its {button!r} button",
    )


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    """The web UI daemon alone, with the long values seeded for the whole module."""
    with harness.ui_daemon(_TOKEN) as base:
        seeded = _seed(base, tmp_path_factory.mktemp("cwe2e-dialog-seed"))
        try:
            yield base
        finally:
            _unseed(base, seeded)


@pytest.mark.standalone
@pytest.mark.parametrize("width, height", SIZES)
def test_settings_panes_and_dialogs_fit_the_dialog(chrome, ui, width, height):
    open_signed_in(chrome, ui, _PASSWORD)
    _seed_sessions(ui)
    chrome.viewport(width, height)
    failures: list[str] = []

    # Every pane, by its tab, in one open modal.
    chrome.navigate(f"{ui}/settings/{PANES[0][0]}")
    chrome.wait("document.querySelectorAll('[role=tab]').length >= 11", desc="the Settings modal")
    _settled(chrome)
    for _section, label, shows in PANES:
        _press(chrome, "[role=tab]", label)
        chrome.wait(_panel_shows(shows), desc=f"the {label} pane with its data")
        failures += _check(chrome, f"{width}px {label} pane")

    # Every dialog reachable without an instance.
    for where, opener, title in DIALOGS:
        what = f"{width}px {title!r} dialog"
        if where.startswith("/"):
            _page(chrome, ui, where, opener[1])
        else:
            _settings(chrome, ui, where, dict((s, t) for s, _, t in PANES)[where])
        _open_dialog(chrome, opener, title, what)
        failures += _check(chrome, what)
        _close_dialog(chrome, what)

    assert not failures, "\n".join(failures)


@pytest.fixture()
def ui_with_instance(running_instance):
    with harness.ui_daemon(_TOKEN) as base:
        yield base, running_instance.name


@pytest.mark.parametrize("width, height", SIZES)
def test_instance_dialogs_fit_the_dialog(chrome, ui_with_instance, width, height):
    base, project = ui_with_instance
    open_signed_in(chrome, base, _PASSWORD)
    chrome.viewport(width, height)
    failures: list[str] = []

    def opened(route: str, opener: tuple, title: str, *, tab: str | None = None) -> None:
        what = f"{width}px {title!r} dialog"
        _page(chrome, base, route, opener[1] if tab is None else tab)
        if tab:
            _press(chrome, "[role=tab]", tab)
            chrome.wait(
                f"[...document.querySelectorAll('button')].some((b) => b.getClientRects().length"
                f" && b.textContent.trim().startsWith({json.dumps(opener[1])}))",
                desc=f"the {tab} tab with its {opener[1]!r} button",
            )
        _open_dialog(chrome, opener, title, what)
        failures.append("\n".join(_check(chrome, what)))
        _close_dialog(chrome, what)

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
    _settings(chrome, base, "cache", project)
    _open_dialog(chrome, ("button", "Clear everything"), "Clear the whole cache", "clear cache")
    failures.append("\n".join(_check(chrome, f"{width}px clear-cache dialog")))
    _close_dialog(chrome, "clear cache")

    assert not any(failures), "\n".join(f for f in failures if f)
