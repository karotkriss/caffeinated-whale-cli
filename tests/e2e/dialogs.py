"""Measure the topmost web UI dialog in a real browser: is anything past its edges?

Shared by the two dialog-overflow E2E modules (``test_webui_modal_overflow_e2e.py`` and
``test_webui_overflow_instance_dialogs_e2e.py``) so both measure the same way.
"""

from __future__ import annotations

import json
import time

import pytest

from .browser import Chrome

DAEMON_TOKEN = "cwe2e-webui-dialogs-token"
PASSWORD = "cwe2e-webui-dialogs-password"
SIZES = [pytest.param(1280, 800, id="desktop"), pytest.param(390, 844, id="phone")]

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
    node.textContent = node.textContent + ' ' + _DAEMON_TOKEN__
  }
  return seeded.length
})()
""".replace(
        "_DAEMON_TOKEN__", json.dumps(TOKEN)
    )
)
_RESTORE = (
    "(() => { for (const [n, t] of window.__seeded || []) n.textContent = t;"
    " window.__seeded = []; return true })()"
)
_COUNT = "document.querySelectorAll('[data-slot=\"content\"]').length"
_PANEL = "document.querySelector('[role=\"tabpanel\"]:not([hidden])')"


def panel_shows(text: str) -> str:
    return f"(() => {{ const p = {_PANEL}; return !!p && p.textContent.includes({json.dumps(text)}) }})()"


def check(chrome: Chrome, what: str) -> list[str]:
    """Measure the topmost dialog as served, then with TOKEN appended to its text."""
    settled(chrome)
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


def press(chrome: Chrome, selector: str, text: str | None = None) -> None:
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


def open_dialog(chrome: Chrome, opener: tuple, title: str, what: str) -> None:
    before = chrome.eval(_COUNT)
    selector, text = opener
    press(chrome, selector, text)
    chrome.wait(
        f"(() => {{ const got = {_MEASURE}; return !!got && got.count === {before} + 1"
        f" && got.title.startsWith({json.dumps(title)}) }})()",
        desc=f"{what} open",
    )


def close_dialog(chrome: Chrome, what: str) -> None:
    before = chrome.eval(_COUNT)
    chrome.press("Escape")
    chrome.wait(f"{_COUNT} === {before} - 1", desc=f"{what} closed again")


def settled(chrome: Chrome) -> None:
    """Past the open dialog's enter transition: a press that lands during it is taken as
    a press outside the dialog and dismisses it."""
    chrome.wait(
        "[...document.querySelectorAll('[data-slot=\"content\"]')].every("
        "(d) => !d.getClientRects().length || getComputedStyle(d).opacity === '1')",
        desc="the dialog's enter transition to end",
    )
    time.sleep(0.2)


def settings(chrome: Chrome, base: str, section: str, shows: str) -> None:
    """Open the Settings modal on ``section`` (a deep link) and wait for its data."""
    chrome.navigate(f"{base}/settings/{section}")
    chrome.wait(panel_shows(shows), desc=f"the {section} section with its data")
    settled(chrome)


def page(chrome: Chrome, base: str, route: str, button: str) -> None:
    chrome.navigate(base + route)
    chrome.wait(
        f"[...document.querySelectorAll('button')].some((b) => b.getClientRects().length"
        f" && b.textContent.trim().startsWith({json.dumps(button)}))",
        desc=f"{route} with its {button!r} button",
    )
