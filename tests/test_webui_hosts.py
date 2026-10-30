"""The web UI's reading of a pairing string (webui/src/data/hosts.js), executed under
node: the page prefills the Add host form from it and the daemon validates it again.

The real module is imported; only its browser-side imports (vue, the api module) are
stubbed, the way tests/test_webui_start_gate.py loads the page's data modules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import hosts

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "hosts.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUBS = {
    "vue": "export const reactive = (o) => o\nexport const computed = (fn) => ({ get value() { return fn() } })",
    "./api": "export const getJson = async () => ({})\nexport const postJson = async () => ({})",
}

HOOKS = """
const STUBS = %s
export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: `stub:${specifier}`, shortCircuit: true }
  return next(specifier, context)
}
export async function load(url, context, next) {
  if (url.startsWith('stub:')) return { format: 'module', source: STUBS[url.slice(5)], shortCircuit: true }
  return next(url, context)
}
"""


@pytest.fixture
def read(tmp_path):
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS % json.dumps(STUBS))
    register = tmp_path / "register.mjs"
    register.write_text(f"import {{ register }} from 'node:module'\nregister({json.dumps(hooks.as_uri())})\n")

    def _read(text: str):
        script = (
            f"const {{ readPairing }} = await import({json.dumps(MODULE.as_uri())})\n"
            f"console.log(JSON.stringify(readPairing({json.dumps(text)})))\n"
        )
        out = subprocess.run(
            ["node", "--import", register.as_uri(), "--input-type=module"],
            input=script,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(out.stdout)

    return _read


def test_the_page_reads_what_the_remote_printed(read):
    fields = {"v": 1, "hub": "hub-x", "cap": "operator", "user": "frappe", "address": "10.9.8.7", "port": 2222, "name": "Build box", "host_key": "ssh-ed25519 AAAA"}
    assert read(hosts.make_pairing(fields)) == fields


@pytest.mark.parametrize("text", ["", "cwcli-remote:v1:", "cwcli-remote:v1:!!!", "words", "cwcli-remote:v2:e30"])
def test_anything_else_reads_as_nothing(read, text):
    assert read(text) is None
