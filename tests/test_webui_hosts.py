"""What the web UI makes of what a remote host sends, executed under node: the page
prefills the Add host form from a pairing string (webui/src/data/hosts.js; the daemon
validates it again) and counts a quiet host's instances as unknown, it renders a remote
site's icon (webui/src/data/site-icons.js) from a URL that remote chose, and a remote
host's app row never opens this machine's app panel (webui/src/data/app-panel.js).

The real modules are imported; only their browser-side imports (vue, the session
store) are stubbed, the way tests/test_webui_start_gate.py loads the page's data modules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import hosts

DATA = Path(__file__).resolve().parents[1] / "webui" / "src" / "data"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": "export const reactive = (o) => o\nexport const computed = (fn) => ({ get value() { return fn() } })",
    "./session": "export const session = {}\nexport const refuseBelow = () => false",
}

HOOKS = """
const STUBS = %s
export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: `stub:${specifier}`, shortCircuit: true }
  // Vite resolves the page's extensionless imports; node needs the file name.
  if (specifier.startsWith('.') && !/\\.\\w+$/.test(specifier)) specifier += '.js'
  return next(specifier, context)
}
export async function load(url, context, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
  return next(url, context)
}
"""


@pytest.fixture
def node(tmp_path):
    """Run ``body`` (the inside of an async function) with the stubs registered and
    return what it returns, through JSON."""
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS % json.dumps(STUBS))
    register = tmp_path / "register.mjs"
    register.write_text(f"import {{ register }} from 'node:module'\nregister({json.dumps(hooks.as_uri())})\n")

    def _run(body: str):
        script = f"const out = await (async () => {{ {body} }})()\nconsole.log(JSON.stringify(out ?? null))\n"
        done = subprocess.run(
            ["node", "--import", register.as_uri(), "--input-type=module"],
            input=script,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(done.stdout)

    return _run


def _read(node, text: str):
    return node(
        f"const {{ readPairing }} = await import({json.dumps((DATA / 'hosts.js').as_uri())})\n"
        f"return readPairing({json.dumps(text)})"
    )


def test_the_page_reads_what_the_remote_printed(node):
    fields = {"v": 1, "hub": "hub-x", "cap": "operator", "user": "frappe", "address": "10.9.8.7", "port": 2222, "name": "Build box", "host_key": "ssh-ed25519 AAAA"}
    assert _read(node, hosts.make_pairing(fields)) == fields


@pytest.mark.parametrize("text", ["", "cwcli-remote:v1:", "cwcli-remote:v1:!!!", "words", "cwcli-remote:v2:e30"])
def test_anything_else_reads_as_nothing(node, text):
    assert _read(node, text) is None


def test_a_remote_icon_can_name_only_the_remotes_own_logo_route(node):
    """A remote chooses its icon URLs. Rewritten under the hub's forward as they came, a
    path that climbs out of it (`..` the browser resolves) would have this page fetch a
    route of the hub itself, with the signed-in user's cookie, whenever Sites renders."""
    urls = [
        "/api/v1/apps/logo/0123abcd4567ef89.png",
        "/api/v1/../../../../api/v1/apps/refs?url=https://attacker.example/x",
        "/api/v1/apps/logo/../../hosts/other/backups/download?key=k",
        "/api/v1/apps/logo/..",
        "/api/v1/apps/logo/%2e%2e",
        "/api/v1/backups/download?key=k",
        "https://attacker.example/x.png",
        "",
    ]
    seen = node(
        f"const icons = await import({json.dumps((DATA / 'site-icons.js').as_uri())})\n"
        "const seen = {}\n"
        f"for (const url of {json.dumps(urls)}) {{\n"
        "  icons.siteIconsLanded({ icons: { 'p/s.localhost': { logo: { url, kind: 'favicon' } } } }, 'build box')\n"
        "  seen[url] = icons.siteIcon('p', 's.localhost', 'build box')?.url ?? null\n"
        "}\n"
        "icons.siteIconsLanded({ icons: { 'p/s.localhost': { logo: { url: '/api/v1/apps/logo/a.png' } } } })\n"
        "seen.local = icons.siteIcon('p', 's.localhost')?.url ?? null\n"
        "return seen"
    )
    assert seen.pop("/api/v1/apps/logo/0123abcd4567ef89.png") == "/api/v1/hosts/build%20box/apps/logo/0123abcd4567ef89.png"
    assert seen.pop("local") == "/api/v1/apps/logo/a.png"  # this machine's own daemon, as it sent it
    assert seen == {url: None for url in urls[1:]}


def test_a_remote_hosts_app_row_never_opens_this_machines_app_panel(node):
    """The app panel is a view over this machine's apps catalog, and its buttons act on this
    machine's instances: a remote host's app row gets no opener (no pointer, nothing that
    opens it), while a local row still opens the panel on that app."""
    seen = node(
        f"const panel = await import({json.dumps((DATA / 'app-panel.js').as_uri())})\n"
        "const remote = panel.appOpener('build box', 'frappe')\n"
        "const local = panel.appOpener(null, 'frappe')\n"
        "const before = panel.appPanel.open\n"
        "local()\n"
        "return { remote: remote ?? null, before, open: panel.appPanel.open, app: panel.appPanel.app.name }"
    )
    assert seen == {"remote": None, "before": False, "open": True, "app": "frappe"}


def test_the_instances_headline_counts_a_quiet_hosts_instances_as_unknown_never_running(node):
    """A host that is not readable is carried by its last snapshot, which may still say
    `container_running: true`; the headline counts those instances as unknown."""
    counts = node(
        f"const h = await import({json.dumps((DATA / 'hosts.js').as_uri())})\n"
        "h.replaceHosts([{ id: 'quiet', name: 'Quiet box', state: 'unreachable', readable: false }, { id: 'live', name: 'Live box', state: 'connected', readable: true }])\n"
        "return h.runningCounts([\n"
        "  { project: 'a', host: null, container_running: true },\n"
        "  { project: 'b', host: null, container_running: false },\n"
        "  { project: 'c', host: 'live', container_running: true },\n"
        "  { project: 'd', host: 'quiet', container_running: true },\n"
        "  { project: 'e', host: 'quiet', container_running: false },\n"
        "])"
    )
    assert counts == {"up": 2, "unknown": 2}
