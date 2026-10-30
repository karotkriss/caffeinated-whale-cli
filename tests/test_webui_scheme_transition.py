"""The light/dark switch's blend, decided under node.

``webui/src/premium/scheme-transition.js`` picks which View Transition plays and runs
the swap inside it. The two paths that must never regress are the fallbacks: reduced
motion (and effects off, and an OS-driven change) must play the plain 200 ms cross-fade,
never the wipe, and a browser without ``document.startViewTransition`` must swap
instantly as the page always did. The module takes the document as a parameter, so a
fake one drives it here with no browser.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "premium" / "scheme-transition.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# A document with (or without) the View Transitions API that records what the module does. A skipped
# transition rejects `ready`, as the browser does when a newer transition replaces it before it animates.
FAKE_DOC = """
const calls = { animate: [], applied: 0, started: 0, unhandled: [] }
process.on('unhandledRejection', (reason) => calls.unhandled.push(String(reason)))
const doc = {
  documentElement: { dataset: {}, animate: (frames, opts) => { calls.animate.push({ frames, opts }); return {} } },
}
if (SUPPORTED) doc.startViewTransition = (cb) => {
  calls.started += 1
  cb()
  return { ready: SKIPPED ? Promise.reject(new Error('AbortError: Transition was skipped')) : Promise.resolve() }
}
"""


def _run(choice, supported=True, skipped=False):
    script = (
        f"import {{ transitionScheme, WIPE_MS }} from {json.dumps(MODULE.as_uri())};\n"
        f"const SUPPORTED = {json.dumps(supported)};\nconst SKIPPED = {json.dumps(skipped)};\n{FAKE_DOC}\n"
        f"const dir = transitionScheme(() => {{ calls.applied += 1 }}, {json.dumps(choice)}, doc);\n"
        "await new Promise((r) => setTimeout(r, 0));\n"  # let `ready.then` run
        "console.log(JSON.stringify({ dir, vt: doc.documentElement.dataset.vt ?? null, ...calls, WIPE_MS }));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


PRESS = {"reduced": False, "enabled": True, "origin": True}


def test_a_press_with_effects_on_plays_the_wipe():
    got = _run(PRESS)
    assert got["dir"] == "wipe" and got["vt"] == "wipe"
    assert got["started"] == 1 and got["applied"] == 1
    (anim,) = got["animate"]
    assert anim["opts"] == {
        "duration": 560,
        "easing": "cubic-bezier(0.23, 1, 0.32, 1)",
        "pseudoElement": "::view-transition-new(root)",
    }
    assert anim["frames"]["maskPosition"] == ["100% 0", "0 0"]
    assert got["WIPE_MS"] == 560


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param({**PRESS, "reduced": True}, id="reduced-motion"),
        pytest.param({**PRESS, "enabled": False}, id="visual-effects-off"),
        pytest.param({**PRESS, "origin": None}, id="os-driven-change"),
    ],
)
def test_the_fallbacks_play_the_cross_fade_not_the_wipe(choice):
    got = _run(choice)
    assert got["dir"] == "fade" and got["vt"] == "fade"
    assert got["started"] == 1 and got["applied"] == 1
    assert got["animate"] == []  # the fade is premium.css's 200 ms rule, no JS animation


@pytest.mark.parametrize(
    "choice",
    [pytest.param(PRESS, id="wipe"), pytest.param({**PRESS, "reduced": True}, id="fade")],
)
def test_a_skipped_transition_leaves_no_unhandled_rejection(choice):
    got = _run(choice, skipped=True)
    assert got["started"] == 1 and got["applied"] == 1
    assert got["animate"] == []
    assert got["unhandled"] == []


def test_without_view_transitions_the_swap_is_instant():
    got = _run(PRESS, supported=False)
    assert got["dir"] == "instant"
    assert got["applied"] == 1 and got["started"] == 0
    assert got["vt"] is None and got["animate"] == []
