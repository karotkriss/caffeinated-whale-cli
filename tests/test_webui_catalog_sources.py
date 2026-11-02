"""The page's side of the app sources on a shared install, decided under node.

* A ``source`` frame lists a host the moment its scan signs in, and a progress tick
  rides on that row, so the sidebar and the Sources card show ``100 of 442`` during
  a first scan instead of nothing (item 1).
* The source filter names a forge nobody is signed in to as such (item 2), and the
  sidebar's scroll region fades at its bottom edge instead of slicing a row (item 3);
  both live in templates, so they are pinned on the source text.
"""

import json
import re
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


def test_the_source_filter_names_a_forge_nobody_is_signed_in_to():
    grid = (WEBUI / "components" / "AppsGrid.vue").read_text()
    assert "(not signed in)" in grid
    assert re.search(r"signedInHosts\.value\.has\(h\) \? hostLabel\(h\)", grid)


def test_the_sidebar_scroll_region_fades_at_its_bottom_edge():
    shell = (WEBUI / "components" / "Shell.vue").read_text()
    assert re.search(r'viewport-class="cw-fade-bottom[^"]*pb-10', shell)
    css = (WEBUI / "style.css").read_text()
    assert re.search(
        r"\.cw-fade-bottom \{[^}]*mask-image: linear-gradient\(to bottom, black calc\(100% - 2\.5rem\), transparent\)",
        css,
    )


def test_the_sidebar_and_sources_card_show_the_scan_progress():
    shell = (WEBUI / "components" / "Shell.vue").read_text()
    assert "progressText(s)" in shell and "s.progress.done }}/{{ s.progress.total" in shell
    apps = (WEBUI / "pages" / "Apps.vue").read_text()
    assert (
        "scanning ${hostLabel(scanningSource.host)} · ${progressText(scanningSource)} projects"
        in apps
    )
