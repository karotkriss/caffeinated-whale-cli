"""The web UI's reading of a pairing string (webui/src/data/hosts.js), executed under
node: the page prefills the Add host form from it and the daemon validates it again."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from caffeinated_whale_cli.core import hosts

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "hosts.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _read(text: str):
    # hosts.js imports vue and the api module; stub both so the pure reader runs alone.
    script = (
        "const src = await (await import('node:fs/promises')).readFile(%s, 'utf8');\n"
        "const body = src.split('export function readPairing')[1];\n"
        "const fn = new Function('text', 'return (function readPairing' + body.split('\\n}\\n')[0] + '\\n})(text)');\n"
        "globalThis.atob = (b) => Buffer.from(b, 'base64').toString('binary');\n"
        "console.log(JSON.stringify(fn(%s)));\n"
    ) % (json.dumps(str(MODULE)), json.dumps(text))
    out = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_page_reads_what_the_remote_printed():
    fields = {"v": 1, "hub": "hub-x", "cap": "operator", "user": "frappe", "address": "10.9.8.7", "port": 2222, "name": "Build box", "host_key": "ssh-ed25519 AAAA"}
    assert _read(hosts.make_pairing(fields)) == fields


@pytest.mark.parametrize("text", ["", "cwcli-remote:v1:", "cwcli-remote:v1:!!!", "words", "cwcli-remote:v2:e30"])
def test_anything_else_reads_as_nothing(text):
    assert _read(text) is None
