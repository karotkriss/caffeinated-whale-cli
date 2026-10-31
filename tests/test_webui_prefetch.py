"""The idle prefetch of route chunks, decided under node.

``webui/src/data/prefetch.js`` warms every lazy page's chunk through the router's own
loaders. What must never regress: each lazy loader runs once, an already-resolved route
(a component object, not a loader) is left alone, a failing loader (a tab older than a
rebuild) stops the sweep instead of throwing, and ``prefetch.active`` is true exactly while
a loader runs - main.js reads it to keep a prefetch failure from reloading the page.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "prefetch.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

FAKE = """
const calls = []
const activeDuring = []
const loader = (name, fail = false) => {
  const fn = () => { calls.push(name); activeDuring.push(prefetch.active); return fail ? Promise.reject(new Error('404')) : Promise.resolve({}) }
  return fn
}
const shared = loader('Instance')
const router = { getRoutes: () => [
  { components: { default: loader('Instances') } },
  { components: { default: { render: () => null } } },   // a resolved component, not a loader
  { components: { default: shared } },
  { components: { default: shared } },                   // the same page under /hosts/:host
  { components: { default: FAILING ? loader('Jobs', true) : loader('Jobs') } },
  { components: { default: loader('Backups') } },
  { redirect: '/instances' },
] }
const schedule = (fn) => setTimeout(fn, 0)
const ok = await prefetchPages(router, { schedule })
console.log(JSON.stringify({ ok, calls, activeDuring, activeAfter: prefetch.active }))
"""


def _node(failing=False):
    script = (
        f"import {{ prefetch, prefetchPages }} from {json.dumps(MODULE.as_uri())};\n"
        f"const FAILING = {json.dumps(failing)};\n{FAKE}"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_every_lazy_page_is_loaded_once_and_resolved_routes_are_left_alone():
    got = _node()
    assert got["ok"] is True
    assert got["calls"] == ["Instances", "Instance", "Jobs", "Backups"]
    assert got["activeDuring"] == [True] * 4 and got["activeAfter"] is False


def test_a_failing_chunk_stops_the_sweep_quietly():
    got = _node(failing=True)
    assert got["ok"] is False
    assert got["calls"] == ["Instances", "Instance", "Jobs"]  # Backups never attempted
    assert got["activeAfter"] is False
