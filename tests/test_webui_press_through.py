"""A press during a View Transition, handed to the live page, decided under node.

While a transition runs Chrome aims every press at ``<html>``: the element under the pointer
never sees it, and an open dialog takes it for a press outside itself and closes.
``webui/src/premium/press-through.js`` stops such a press before the page sees it, skips the
transition, and once the new DOM is in and has had a frame dispatches the press, its release
and its click again to the element under the pointer. What must never regress: the page (the
dialog's outside-press handler) never sees the native press; the gesture reaches the NEW
page's element in order; and once the transition and the gesture are over, presses pass
untouched. A real browser proves the same in ``tests/e2e/test_webui_navigation_e2e.py``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "premium" / "press-through.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# A window whose capture listeners run first, a document whose `elementFromPoint` answers the
# old page's element until the transition's update lands and the new page's after, and a
# transition whose update and end the test drives. `press(type)` dispatches a native press:
# the window's capture listeners, then, unless one stopped it, the page, where an open dialog
# closes on any pointerdown (a press on <html> is outside it).
FAKE = """
const log = []
const unhandled = []
process.on('unhandledRejection', (reason) => unhandled.push(String(reason)))
const tick = () => new Promise((r) => setTimeout(r, 0))
class Ev {
  constructor(type, init = {}) {
    Object.assign(this, init)
    Object.assign(this, { type, isTrusted: false, stopped: false, defaultPrevented: false })
  }
  stopImmediatePropagation() { this.stopped = true }
  preventDefault() { this.defaultPrevented = true }
}
const listeners = []
const frames = []
const frame = () => frames.splice(0).forEach((fn) => fn())
const win = {
  addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
  removeEventListener: (type, fn, capture) => {
    const i = listeners.findIndex((l) => l.type === type && l.fn === fn && l.capture === capture)
    if (i >= 0) listeners.splice(i, 1)
  },
  requestAnimationFrame: (fn) => frames.push(fn),
}
// `focusable` marks an element that is (or sits in) a field a press focuses.
const element = (name) => {
  const el = {
    focusable: false,
    dispatchEvent: (e) => log.push(`${name}:${e.type}:${e.isTrusted ? 'native' : 'handed'}:${e.clientX},${e.clientY}`),
    closest: () => (el.focusable ? el : null),
    focus: () => log.push(`${name}:focus`),
  }
  return el
}
const oldPage = element('old'), newPage = element('new')
let under = oldPage
const doc = { defaultView: win, documentElement: element('html'), elementFromPoint: () => under }
const dialog = { open: true }
const press = (type, { button = 0, trusted = true } = {}) => {
  const e = new Ev(type, { button, clientX: 10, clientY: 20 })
  e.isTrusted = trusted
  listeners.filter((l) => l.type === type && l.capture).forEach((l) => { if (!e.stopped) l.fn(e) })
  if (!e.stopped) {
    log.push(`page:${type}`)
    if (type === 'pointerdown') dialog.open = false
  }
  return e
}
let update, end
const vt = {
  skipTransition() { log.push('skip') },
  updateCallbackDone: new Promise((r) => (update = () => { under = newPage; r() })),
  finished: new Promise((r) => (end = r)),
}
passPressesThrough(vt, doc)
const GESTURE = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']
"""


def _node(body):
    script = (
        f"import {{ passPressesThrough }} from {json.dumps(MODULE.as_uri())};\n{FAKE}\n{body}\n"
        "console.log(JSON.stringify({ log, unhandled, open: dialog.open, ...out }))\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


HANDED = [
    f"new:{t}:handed:10,20" for t in ("pointerdown", "mousedown", "pointerup", "mouseup", "click")
]


def test_a_press_during_a_transition_reaches_the_new_page_and_not_the_dialog():
    got = _node(
        """
const events = GESTURE.map((type) => press(type))
log.push('released')
const openDuring = dialog.open
update(); await tick()
log.push('new page in')
frame(); end(); await tick()
const out = { openDuring, stopped: events.map((e) => e.stopped), prevented: events.map((e) => e.defaultPrevented) }
"""
    )
    # Nothing reached the page: the dialog stayed open. The transition was skipped at the
    # press, and the whole gesture went to the element under the pointer once the new page
    # was in and had a frame, never to the old page's element.
    assert got["openDuring"] is True and got["open"] is True
    assert got["stopped"] == [True] * 5
    assert got["prevented"] == [False, True, False, False, False]  # mousedown: focus stays
    assert got["log"] == ["skip", "released", "new page in", *HANDED]
    assert got["unhandled"] == []


def test_a_release_after_the_handover_is_delivered_at_once():
    got = _node(
        """
press('pointerdown'); press('mousedown')
update(); await tick(); frame()
log.push('handed over')
press('pointerup'); press('mouseup'); press('click')
const out = {}
"""
    )
    assert got["log"] == ["skip", *HANDED[:2], "handed over", *HANDED[2:]]


def test_after_the_transition_and_the_gesture_presses_pass_untouched():
    got = _node(
        """
GESTURE.forEach((type) => press(type))
update(); await tick(); frame(); end(); await tick()
const next = press('pointerdown')
const out = { stopped: next.stopped, listeners: listeners.length }
"""
    )
    assert got["log"][-1] == "page:pointerdown" and got["stopped"] is False
    assert got["listeners"] == 0


def test_a_transition_that_ends_with_no_press_leaves_presses_alone():
    got = _node(
        """
update(); end(); await tick()
press('pointerdown')
const out = { listeners: listeners.length }
"""
    )
    assert got["log"] == ["page:pointerdown"] and got["listeners"] == 0


def test_a_press_a_script_dispatched_passes_untouched():
    got = _node("press('pointerdown', { trusted: false })\nconst out = {}")
    assert got["log"] == ["page:pointerdown"]


def test_another_button_during_a_transition_is_stopped_not_handed_over():
    got = _node(
        """
const events = ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].map((type) => press(type, { button: 2 }))
const out = { stopped: events.map((e) => e.stopped) }
"""
    )
    # Neither the page nor the dialog sees it, and the transition keeps running.
    assert got["log"] == [] and got["open"] is True
    assert got["stopped"] == [True] * 4


def test_another_button_replacing_a_pending_press_is_stopped_too():
    got = _node(
        """
press('pointerdown'); press('mousedown')
const second = press('pointerdown', { button: 1 })
update(); await tick(); frame()
const out = { stopped: second.stopped }
"""
    )
    assert got["stopped"] is True and got["open"] is True
    assert "page:pointerdown" not in got["log"]


def test_a_press_released_off_the_page_does_not_hold_the_next_one():
    got = _node(
        """
press('pointerdown'); press('mousedown') // released outside the window: no click comes
update(); await tick(); frame(); end(); await tick()
press('pointerdown')
const out = { listeners: listeners.length }
"""
    )
    assert got["log"] == ["skip", *HANDED[:2], "page:pointerdown"]
    assert got["listeners"] == 0


def test_a_handed_press_on_a_field_focuses_it_as_a_native_press_would():
    got = _node(
        """
newPage.focusable = true
GESTURE.forEach((type) => press(type))
update(); await tick(); frame()
const out = {}
"""
    )
    assert got["log"] == ["skip", *HANDED[:2], "new:focus", *HANDED[2:]]


def test_a_handed_press_whose_mousedown_the_page_cancels_moves_no_focus():
    got = _node(
        """
newPage.focusable = true
const dispatch = newPage.dispatchEvent
newPage.dispatchEvent = (e) => { if (e.type === 'mousedown') e.preventDefault(); dispatch(e) }
GESTURE.forEach((type) => press(type))
update(); await tick(); frame()
const out = {}
"""
    )
    assert "new:focus" not in got["log"] and got["log"][-1] == HANDED[-1]
