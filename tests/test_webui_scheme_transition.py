"""The light/dark switch's blend, decided under node.

``webui/src/premium/scheme-transition.js`` picks which View Transition plays and runs
the swap inside it. The paths that must never regress are the fallbacks and the press
guard: reduced motion (and effects off, and an OS-driven change) must play the plain
200 ms cross-fade, never the wipe; a browser without ``document.startViewTransition``
must swap instantly as the page always did; and a press during a blend, which lands on
``<html>``, must never reach an open dialog that would close on it. The module takes the
document as a parameter, so a fake one drives it here with no browser.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "premium" / "scheme-transition.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

PRESS = {"reduced": False, "enabled": True, "origin": True}
OS_CHANGE = {**PRESS, "origin": None}

# A document with (or without) the View Transitions API that records what the module does. A skipped
# transition rejects `ready`, as the browser does when a newer transition replaces it before it animates.
# `end(i)` finishes the i-th transition. `press(type)` dispatches a press on <html> (where every press
# lands while a blend runs) the DOM's way: the document's capture listeners, then, unless one of them
# stopped propagation, its bubble listeners.
FAKE_DOC = """
const calls = { applied: 0, started: 0, unhandled: [] }
process.on('unhandledRejection', (reason) => calls.unhandled.push(String(reason)))
const tick = () => new Promise((r) => setTimeout(r, 0))
const listeners = []
const key = (type, fn, options) => ({ type, fn, capture: typeof options === 'object' ? !!options?.capture : !!options })
const at = (l) => listeners.findIndex((m) => m.type === l.type && m.fn === l.fn && m.capture === l.capture)
const doc = {
  documentElement: { dataset: {} },
  addEventListener: (...args) => { const l = key(...args); if (at(l) < 0) listeners.push(l) },
  removeEventListener: (...args) => { const i = at(key(...args)); if (i >= 0) listeners.splice(i, 1) },
}
const press = (type) => {
  const event = {
    type, target: doc.documentElement, stopped: false, defaultPrevented: false,
    stopPropagation() { this.stopped = true }, preventDefault() { this.defaultPrevented = true },
  }
  const run = (capture) => listeners.filter((l) => l.type === type && l.capture === capture).forEach((l) => l.fn(event))
  run(true)
  if (!event.stopped) run(false)
  return event
}
const ends = []
const end = (i) => ends[i]()
if (SUPPORTED) doc.startViewTransition = (cb) => {
  calls.started += 1
  cb()
  const finished = new Promise((resolve) => ends.push(resolve))
  if (SKIPPED) end(ends.length - 1)
  return { ready: SKIPPED ? Promise.reject(new Error('AbortError: Transition was skipped')) : Promise.resolve(), finished }
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
# document's bubble listeners from outside the dialog, as a press on <html> does.
DIALOG = """
const dialog = { open: true }
doc.addEventListener('pointerdown', () => { dialog.open = false })
const swallowed = (type) => { const event = press(type); return event.stopped && event.defaultPrevented }
"""


@pytest.mark.parametrize(
    "choice", [pytest.param(PRESS, id="wipe"), pytest.param(OS_CHANGE, id="fade")]
)
def test_an_open_dialog_stays_open_when_pressed_during_the_blend(choice):
    got = _node(
        DIALOG
        + f"transitionScheme(() => {{}}, {json.dumps(choice)}, doc)\n"
        + """
const during = ['pointerdown', 'mousedown', 'click'].map(swallowed)
const openDuring = dialog.open
end(0)
await tick()
const after = swallowed('pointerdown')
console.log(JSON.stringify({ during, openDuring, after, openAfter: dialog.open }))
"""
    )
    assert got == {
        "during": [True, True, True],
        "openDuring": True,
        "after": False,
        "openAfter": False,
    }


def test_a_blend_that_replaces_another_keeps_presses_swallowed_until_it_ends():
    got = _node(
        DIALOG
        + """
transitionScheme(() => {}, PRESS, doc)
transitionScheme(() => {}, OS_CHANGE, doc)
end(0) // the browser skips the first blend when the second starts
await tick()
press('pointerdown')
const openDuringSecond = dialog.open
end(1)
await tick()
press('pointerdown')
console.log(JSON.stringify({ openDuringSecond, openAfter: dialog.open }))
"""
    )
    assert got == {"openDuringSecond": True, "openAfter": False}


def test_without_view_transitions_the_swap_is_instant():
    got = _run(PRESS, supported=False)
    assert got["dir"] == "instant"
    assert got["applied"] == 1 and got["started"] == 0
    assert got["vt"] is None
