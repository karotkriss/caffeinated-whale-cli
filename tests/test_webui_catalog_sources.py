"""The page's side of the app sources on a shared install, decided under node.

* A ``source`` frame lists a host the moment its scan signs in, and a progress tick
  rides on that row, so the sidebar and the Sources card show ``100 of 442`` during
  a first scan instead of nothing (item 1).
* A rescan of a host the page already knows keeps its remembered row (counts,
  account, icon) instead of the blank row the scan starts from.
* The source filter names a forge nobody is signed in to as such (item 2).
"""

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

WEBUI = Path(__file__).resolve().parents[1] / "webui" / "src"
MODULE = WEBUI / "data" / "catalog.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": "export const reactive = (o) => o\nexport const computed = (fn) => ({ get value() { return fn() } })",
    "./api": (
        "export async function getJson(url) { globalThis.__t.reads.push(url); return globalThis.__t.answer }\n"
        "export async function postJson() { return {} }"
    ),
}
HOOKS = """
const STUBS = %s
export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: `stub:${specifier}`, shortCircuit: true }
  if (specifier.startsWith('.') && !/\\.\\w+$/.test(specifier)) specifier += '.js'
  return next(specifier, context)
}
export async function load(url, context, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
  return next(url, context)
}
"""


def _run(script: str, tmp_path: Path) -> dict:
    """Run an ES-module script under node with the page's browser-side imports stubbed."""
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS % json.dumps(STUBS))
    register = f"import {{ register }} from 'node:module'; register({json.dumps(hooks.as_uri())})"
    out = subprocess.run(
        ["node", "--input-type=module", "--import", f"data:text/javascript,{quote(register)}"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_source_frame_lists_the_host_and_the_tick_rides_on_its_row(tmp_path):
    script = f"""
globalThis.__t = {{ reads: [], answer: {{ scanned_at: null, scanning: true, sources: [{{ kind: 'github', host: 'github.com', signed_in: false }}], apps: [] }} }}
const m = await import({json.dumps(MODULE.as_uri())})
await m.loadCatalog()
const before = m.activeSources.value.map((s) => s.host)
m.catalogLanded({{ source: {{ kind: 'gitlab', host: 'gitlab.example.com', account: 'svc', signed_in: true, progress: null }} }})
const listed = m.activeSources.value.map((s) => s.host)
m.catalogLanded({{ progress: {{ host: 'gitlab.example.com', done: 100, total: 442 }} }})
const row = m.sources.value.find((s) => s.host === 'gitlab.example.com')
console.log(JSON.stringify({{ before, listed, text: m.progressText(row), scanning: m.catalog.scanning, reads: globalThis.__t.reads.length }}))
"""
    out = _run(script, tmp_path)
    assert out["before"] == []
    assert out["listed"] == ["gitlab.example.com"]
    assert out["text"] == "100 of 442"
    assert out["scanning"] is True
    assert out["reads"] == 1, "no re-read was needed: the frames carried the row"


def test_a_frame_for_the_next_host_ends_the_previous_hosts_progress(tmp_path):
    script = f"""
globalThis.__t = {{ reads: [], answer: {{ scanned_at: null, scanning: true, sources: [{{ kind: 'gitlab', host: 'a.example.com', signed_in: true }}, {{ kind: 'gitlab', host: 'b.example.com', signed_in: true }}], apps: [] }} }}
const m = await import({json.dumps(MODULE.as_uri())})
await m.loadCatalog()
const text = (h) => m.progressText(m.sources.value.find((s) => s.host === h))
m.catalogLanded({{ progress: {{ host: 'a.example.com', done: 442, total: 442 }} }})
const before = text('a.example.com')
m.catalogLanded({{ progress: {{ host: 'b.example.com', done: 1, total: 10 }} }})
const afterTick = [text('a.example.com'), text('b.example.com')]
m.catalogLanded({{ source: {{ kind: 'marketplace', host: 'marketplace', signed_in: true }} }})
console.log(JSON.stringify({{ before, afterTick, afterSource: text('b.example.com') }}))
"""
    assert _run(script, tmp_path) == {
        "before": "442 of 442",
        "afterTick": [None, "1 of 10"],
        "afterSource": None,
    }


def test_a_tick_for_a_host_the_page_has_not_seen_re_reads_the_catalog(tmp_path):
    script = f"""
globalThis.__t = {{ reads: [], answer: {{ scanned_at: null, scanning: true, sources: [], apps: [] }} }}
const m = await import({json.dumps(MODULE.as_uri())})
await m.loadCatalog()
m.catalogLanded({{ progress: {{ host: 'gitlab.example.com', done: 20, total: 442 }} }})
await new Promise((r) => setTimeout(r, 0))
console.log(JSON.stringify({{ reads: globalThis.__t.reads.length, progress: m.catalog.progress }}))
"""
    out = _run(script, tmp_path)
    assert out["reads"] == 2
    assert out["progress"] == {"host": "gitlab.example.com", "done": 20, "total": 442}


def test_a_rescan_of_a_known_host_keeps_its_remembered_row(tmp_path):
    known = {
        "kind": "github",
        "host": "github.com",
        "signed_in": True,
        "account": "octo",
        "apps": 50,
        "repos": 120,
        "icon": {"url": "/icon.png"},
        "state": "verified",
        "scanned_at": 1700000000,
        "error": None,
    }
    remembered = {**known, "signed_in": False, "error": "gh is not signed in"}
    script = f"""
globalThis.__t = {{ reads: [], answer: {{ scanned_at: 1700000000, scanning: false, sources: [{json.dumps(remembered)}], apps: [] }} }}
const m = await import({json.dumps(MODULE.as_uri())})
await m.loadCatalog()
m.catalogLanded({{ source: {{ kind: 'github', host: 'github.com', signed_in: true, account: null, apps: 0, repos: 0, icon: null, state: 'unverified', scanned_at: null, error: null, progress: null }} }})
console.log(JSON.stringify({{ rows: m.sources.value, icon: m.hostIcon('github.com') }}))
"""
    out = _run(script, tmp_path)
    assert out["rows"] == [known]
    assert out["icon"] == "/icon.png"


def test_the_source_filter_names_a_forge_nobody_is_signed_in_to(tmp_path):
    """Only hosts some app came from are offered: a signed-in forge with no apps
    (gitlab.com here) would show nothing when picked."""
    answer = {
        "scanned_at": None,
        "scanning": False,
        "sources": [
            {"kind": "github", "host": "github.com", "signed_in": False},
            {"kind": "gitlab", "host": "gitlab.example.com", "signed_in": True},
            {"kind": "gitlab", "host": "gitlab.com", "signed_in": True},
            {"kind": "marketplace", "host": "marketplace", "signed_in": True},
        ],
        "apps": [
            {"name": "hrms", "source": {"kind": "github", "host": "github.com"}},
            {"name": "erp", "source": {"kind": "gitlab", "host": "gitlab.example.com"}},
            {"name": "shop", "source": {"kind": "marketplace", "host": "marketplace"}},
        ],
    }
    script = f"""
globalThis.__t = {{ reads: [], answer: {json.dumps(answer)} }}
const m = await import({json.dumps(MODULE.as_uri())})
await m.loadCatalog()
console.log(JSON.stringify(m.hostFilterOptions.value))
"""
    assert _run(script, tmp_path) == [
        {"label": "GitHub (not signed in)", "value": "github.com"},
        {"label": "gitlab.example.com", "value": "gitlab.example.com"},
    ]
