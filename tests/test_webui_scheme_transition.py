"""The light/dark switch's blend, decided under node.

``webui/src/premium/scheme-transition.js`` picks which View Transition plays and runs
the swap inside it. The paths that must never regress are the fallbacks and the press
guard: reduced motion (and effects off, and an OS-driven change) must play the plain
200 ms cross-fade, never the wipe; a browser without ``document.startViewTransition``
must swap instantly as the page always did; and a press during a blend, which lands on
``<html>``, must never reach an open dialog that would close on it, but ends the blend and
reaches the control under the pointer (``press-through.js``, decided in full in
``tests/test_webui_press_through.py``). The module takes the document as a parameter, so a
fake one drives it here with no browser.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "premium" / "scheme-transition.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

PRESS = {"reduced": False, "enabled": True, "pressed": True}
OS_CHANGE = {**PRESS, "pressed": False}

# A document with (or without) the View Transitions API that records what the module does. A skipped
# transition rejects `ready`, as the browser does when a newer transition replaces it before it animates.
# `end(i)` finishes the i-th transition. `press(type)` dispatches a native press the DOM's way: the
# window's capture listeners first, then, unless one of them stopped it, the document's listeners. The
# element under the pointer (the scheme control) records what is handed to it.
FAKE_DOC = """
const calls = { applied: 0, started: 0, skipped: 0, handed: [], unhandled: [] }
process.on('unhandledRejection', (reason) => calls.unhandled.push(String(reason)))
const tick = () => new Promise((r) => setTimeout(r, 0))
const registry = (list) => ({
  addEventListener: (type, fn) => list.push({ type, fn }),
  removeEventListener: (type, fn) => { const i = list.findIndex((l) => l.type === type && l.fn === fn); if (i >= 0) list.splice(i, 1) },
})
const windowListeners = []
const docListeners = []
const win = { ...registry(windowListeners), requestAnimationFrame: (fn) => fn() }
class Ev {
  constructor(type, init = {}) { Object.assign(this, init, { type, isTrusted: false }) }
  stopImmediatePropagation() { this.stopped = true }
  preventDefault() {}
}
const control = { dispatchEvent: (e) => calls.handed.push(e.type) }
const doc = {
  ...registry(docListeners),
  defaultView: win,
  documentElement: { dataset: {} },
  elementFromPoint: () => control,
}
const press = (type) => {
  const event = new Ev(type, { button: 0, clientX: 5, clientY: 5 })
  event.isTrusted = true
  windowListeners.filter((l) => l.type === type).forEach((l) => { if (!event.stopped) l.fn(event) })
  if (!event.stopped) docListeners.filter((l) => l.type === type).forEach((l) => l.fn(event))
  return event
}
const ends = []
const end = (i) => ends[i]()
if (SUPPORTED) doc.startViewTransition = (cb) => {
  calls.started += 1
  cb()
  const finished = new Promise((resolve) => ends.push(resolve))
  if (SKIPPED) end(ends.length - 1)
  return {
    ready: SKIPPED ? Promise.reject(new Error('AbortError: Transition was skipped')) : Promise.resolve(),
    finished,
    updateCallbackDone: Promise.resolve(),
    skipTransition: () => { calls.skipped += 1 },
  }
}
"""


def _node(body, supported=True, skipped=False):
    script = (
        f"import {{ transitionScheme }} from {json.dumps(MODULE.as_uri())};\n"
        f"const SUPPORTED = {json.dumps(supported)};\nconst SKIPPED = {json.dumps(skipped)};\n"
        f"const PRESS = {json.dumps(PRESS)};\nconst OS_CHANGE = {json.dumps(OS_CHANGE)};\n"
        f"{FAKE_DOC}\n{body}"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _run(choice, supported=True, skipped=False):
    return _node(
        f"const dir = transitionScheme(() => {{ calls.applied += 1 }}, {json.dumps(choice)}, doc);\n"
        "await tick();\n"
        "console.log(JSON.stringify({ dir, vt: doc.documentElement.dataset.vt ?? null, ...calls }));\n",
        supported,
        skipped,
    )


def test_a_press_with_effects_on_plays_the_wipe():
    got = _run(PRESS)
    assert got["dir"] == "wipe" and got["vt"] == "wipe"
    assert got["started"] == 1 and got["applied"] == 1


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param({**PRESS, "reduced": True}, id="reduced-motion"),
        pytest.param({**PRESS, "enabled": False}, id="visual-effects-off"),
        pytest.param(OS_CHANGE, id="os-driven-change"),
    ],
)
def test_the_fallbacks_play_the_cross_fade_not_the_wipe(choice):
    got = _run(choice)
    assert got["dir"] == "fade" and got["vt"] == "fade"
    assert got["started"] == 1 and got["applied"] == 1


@pytest.mark.parametrize(
    "choice",
    [pytest.param(PRESS, id="wipe"), pytest.param({**PRESS, "reduced": True}, id="fade")],
)
def test_a_skipped_transition_leaves_no_unhandled_rejection(choice):
    got = _run(choice, skipped=True)
    assert got["started"] == 1 and got["applied"] == 1
    assert got["unhandled"] == []


# An open frappe-ui dialog: reka-ui's DismissableLayer closes it on a pointerdown that reaches the
# document's listeners from outside the dialog, as a press on <html> does.
DIALOG = """
const dialog = { open: true }
doc.addEventListener('pointerdown', () => { dialog.open = false })
const GESTURE = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']
"""


@pytest.mark.parametrize(
    "choice", [pytest.param(PRESS, id="wipe"), pytest.param(OS_CHANGE, id="fade")]
)
def test_a_press_during_the_blend_reaches_the_control_and_leaves_the_dialog_open(choice):
    got = _node(
        DIALOG
        + f"transitionScheme(() => {{}}, {json.dumps(choice)}, doc)\n"
        + """
GESTURE.forEach(press)
await tick()
const openDuring = dialog.open
end(0)
await tick()
press('pointerdown')
console.log(JSON.stringify({ openDuring, skipped: calls.skipped, handed: calls.handed, openAfter: dialog.open }))
"""
    )
    assert got == {
        "openDuring": True,
        "skipped": 1,
        "handed": ["pointerdown", "mousedown", "pointerup", "mouseup", "click"],
        "openAfter": False,
    }


def test_a_blend_that_replaces_another_still_hands_presses_over_until_it_ends():
    got = _node(
        DIALOG
        + """
transitionScheme(() => {}, PRESS, doc)
transitionScheme(() => {}, OS_CHANGE, doc)
end(0) // the browser skips the first blend when the second starts
await tick()
GESTURE.forEach(press)
await tick()
const openDuringSecond = dialog.open
end(1)
await tick()
press('pointerdown')
console.log(JSON.stringify({ openDuringSecond, handed: calls.handed.length, openAfter: dialog.open }))
"""
    )
    assert got == {"openDuringSecond": True, "handed": 5, "openAfter": False}


def test_without_view_transitions_the_swap_is_instant():
    got = _run(PRESS, supported=False)
    assert got["dir"] == "instant"
    assert got["applied"] == 1 and got["started"] == 0
    assert got["vt"] is None
