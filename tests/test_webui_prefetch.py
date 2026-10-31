"""The idle prefetch of route chunks, decided under node.

``webui/src/data/prefetch.js`` warms every lazy page's chunk through the router's own
loaders. What must never regress: each lazy loader runs once, an already-resolved route
(a component object, not a loader) is left alone, a failing loader (a tab older than a
rebuild) stops the sweep instead of throwing, ``prefetch.active`` is true exactly while
a loader runs, a failed preload during the sweep goes back to the request that made it (so a
real navigation's still reaches the router and reloads), and ``page()`` hands every route that
names a page the same loader, so the sweep loads each page once in production.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webui" / "src"
MODULE = SRC / "data" / "prefetch.js"
PAGES = SRC / "pages.js"
CHUNKS = SRC / "data" / "chunk-recovery.js"

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


def _run(script):
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _node(failing=False):
    return _run(
        f"import {{ prefetch, prefetchPages }} from {json.dumps(MODULE.as_uri())};\n"
        f"const FAILING = {json.dumps(failing)};\n{FAKE}"
    )


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


def test_routes_built_by_the_real_page_factory_share_one_loader_per_page():
    got = _run(
        f"""
import {{ routeLoaders }} from {json.dumps(MODULE.as_uri())};
import {{ page }} from {json.dumps(PAGES.as_uri())};
const names = ['Instances', 'Instance', 'Bench', 'Instance', 'Bench', 'Jobs', 'Job', 'Job'];
const router = {{ getRoutes: () => names.map((n) => ({{ components: {{ default: page(n) }} }})) }};
console.log(JSON.stringify({{
  same: page('Instance') === page('Instance'),
  distinct: page('Instance') !== page('Bench'),
  loaders: routeLoaders(router).length,
}}));
"""
    )
    assert got == {"same": True, "distinct": True, "loaders": 5}


# Vite's preload helper: a failed chunk dispatches a cancelable `vite:preloadError` and
# throws to its caller unless a listener prevented it. The listener is main.js's.
PRELOAD = """
const target = new EventTarget()
const reloads = []
target.addEventListener('vite:preloadError', (event) => {
  if (preloadErrorReloads(event)) reloads.push(event.payload.message)
})
const vitePreload = (url) => Promise.reject(new Error('Failed to fetch dynamically imported module: ' + url))
  .catch((err) => {
    const e = new Event('vite:preloadError', { cancelable: true })
    e.payload = err
    target.dispatchEvent(e)
    if (!e.defaultPrevented) throw err
  })
const navigate = () => vitePreload('/assets/Job-old.js').then(() => null, (err) => err.message)
"""


def test_a_failed_prefetch_never_reloads_but_a_real_navigation_still_does():
    got = _run(
        f"""
import {{ prefetchPages, preloadErrorReloads }} from {json.dumps(MODULE.as_uri())};
import {{ isChunkLoadError }} from {json.dumps(CHUNKS.as_uri())};
{PRELOAD}
let release
const gate = new Promise((r) => (release = r))
const router = {{ getRoutes: () => [
  {{ components: {{ default: () => gate.then(() => vitePreload('/assets/Apps-old.js')) }} }},
] }}
const sweep = prefetchPages(router, {{ schedule: (fn) => fn() }})
const during = await navigate()
release()
const swept = await sweep
const after = await navigate()
console.log(JSON.stringify({{
  routerSawDuring: during !== null && isChunkLoadError(new Error(during)),
  swept,
  reloads,
  routerSawAfter: after,
}}));
"""
    )
    assert got["routerSawDuring"] is True
    assert got["swept"] is False
    assert got["reloads"] == ["Failed to fetch dynamically imported module: /assets/Job-old.js"]
    assert got["routerSawAfter"] is None
