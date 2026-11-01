"""Page navigation as a View Transition, decided under node.

``webui/src/premium/page-transition.js`` starts a same-document View Transition from a
``beforeResolve`` guard so the browser holds the old page until the next one has rendered.
What must never regress: the hold-then-release handshake (the route changes only inside the
transition's update callback, and the callback's promise resolves only after the new page is
in the DOM); the fallbacks (no API, motion off, the first navigation, a query-only change
such as a tab switch) start no transition and let the swap happen atomically; a failed
navigation and a transition skipped by a newer one release without an unhandled rejection.
The module takes the router and the document as parameters, so fakes drive it here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "premium" / "page-transition.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# A router recording its hooks, a document with (or without) the API, and `settle` as the
# render flush the page waits for. `log` records the order of every step.
FAKE = """
const log = []
const unhandled = []
process.on('unhandledRejection', (reason) => unhandled.push(String(reason)))
const tick = () => new Promise((r) => setTimeout(r, 0))
const hooks = { beforeResolve: [], afterEach: [], onError: [] }
const router = {
  beforeResolve: (fn) => hooks.beforeResolve.push(fn),
  afterEach: (fn) => hooks.afterEach.push(fn),
  onError: (fn) => hooks.onError.push(fn),
}
const ends = []
const doc = { documentElement: { dataset: {} } }
if (SUPPORTED) doc.startViewTransition = (update) => {
  log.push('snapshot-old')
  const updateDone = Promise.resolve().then(update).then(() => log.push('snapshot-new'))
  const finished = new Promise((resolve) => ends.push(resolve))
  if (SKIPPED) ends[ends.length - 1]()
  return { ready: SKIPPED ? Promise.reject(new Error('AbortError: skipped')) : updateDone, finished, updateCallbackDone: updateDone }
}
let flush = null
const settle = () => { log.push('settle-requested'); return new Promise((r) => (flush = r)) }
const render = () => { log.push('rendered'); flush && flush() }
let motionOn = MOTION
installPageTransition(router, { motion: () => motionOn, settle, doc })
const page = (path, matched = true) => ({ path, matched: matched ? [{}] : [] })
// Drive one navigation the way vue-router does: the guard, then (unless it refused) the
// route change and afterEach, then the render flush.
async function navigate(to, from, { failure = null } = {}) {
  const result = hooks.beforeResolve[0](to, from)
  log.push(result === undefined ? 'guard-sync' : 'guard-waiting')
  await result
  log.push('route-changed')
  hooks.afterEach.forEach((fn) => fn(to, from, failure))
  await tick()
  log.push('flush')
  render()
  await tick()
  await tick()
}
"""


def _node(body, supported=True, motion=True, skipped=False):
    script = (
        f"import {{ installPageTransition, shouldTransition }} from {json.dumps(MODULE.as_uri())};\n"
        f"const SUPPORTED = {json.dumps(supported)};\nconst MOTION = {json.dumps(motion)};\n"
        f"const SKIPPED = {json.dumps(skipped)};\n{FAKE}\n{body}\n"
        "console.log(JSON.stringify({ log, unhandled, vt: doc.documentElement.dataset.vt ?? null }))\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_old_page_is_held_until_the_new_one_has_rendered():
    got = _node("await navigate(page('/apps'), page('/instances'))")
    # The old snapshot is taken before the route changes; the new one only after the render.
    assert got["log"] == [
        "snapshot-old",
        "guard-waiting",
        "route-changed",
        "settle-requested",
        "flush",
        "rendered",
        "snapshot-new",
    ]
    assert got["vt"] == "page" and got["unhandled"] == []


@pytest.mark.parametrize(
    "setup, to, frm",
    [
        pytest.param({"supported": False}, "page('/apps')", "page('/instances')", id="no-api"),
        pytest.param({"motion": False}, "page('/apps')", "page('/instances')", id="motion-off"),
        pytest.param({}, "page('/apps')", "page('/', false)", id="first-navigation"),
        pytest.param({}, "page('/instances/p')", "page('/instances/p')", id="tab-change"),
    ],
)
def test_the_fallbacks_swap_atomically_with_no_transition(setup, to, frm):
    got = _node(f"await navigate({to}, {frm})", **setup)
    assert "snapshot-old" not in got["log"]
    assert got["log"][0] == "guard-sync"  # the guard returns at once: nothing held
    assert got["vt"] is None


def test_a_failed_navigation_releases_the_held_page():
    got = _node("await navigate(page('/apps'), page('/instances'), { failure: { type: 8 } })")
    assert "settle-requested" not in got["log"]  # released at once, not after a render
    assert got["log"].index("snapshot-new") < got["log"].index("flush")
    assert got["unhandled"] == []


def test_a_router_error_releases_the_held_page():
    got = _node(
        """
const result = hooks.beforeResolve[0](page('/apps'), page('/instances'))
await result
hooks.onError.forEach((fn) => fn(new Error('chunk failed')))
await tick(); await tick()
"""
    )
    assert "snapshot-new" in got["log"] and got["unhandled"] == []


def test_a_transition_skipped_by_a_newer_one_leaves_no_unhandled_rejection():
    got = _node("await navigate(page('/apps'), page('/instances'))", skipped=True)
    assert got["unhandled"] == []


def test_a_second_navigation_releases_the_first_before_starting():
    got = _node(
        """
const first = hooks.beforeResolve[0](page('/apps'), page('/instances'))
await first
hooks.afterEach.forEach((fn) => fn(page('/apps'), page('/instances'), null))
// The user clicks again before the Apps page has rendered.
await navigate(page('/sites'), page('/apps'))
"""
    )
    # Both update callbacks resolved (two new snapshots) and nothing was left hanging.
    assert got["log"].count("snapshot-new") == 2 and got["unhandled"] == []


def test_should_transition_is_the_one_rule():
    got = _node(
        """
log.push(shouldTransition({ to: page('/a'), from: page('/b'), motion: true, supported: true }))
log.push(shouldTransition({ to: page('/a'), from: page('/a'), motion: true, supported: true }))
log.push(shouldTransition({ to: page('/a'), from: page('/b', false), motion: true, supported: true }))
log.push(shouldTransition({ to: page('/a'), from: page('/b'), motion: false, supported: true }))
log.push(shouldTransition({ to: page('/a'), from: page('/b'), motion: true, supported: false }))
"""
    )
    assert got["log"] == [True, False, False, False, False]
