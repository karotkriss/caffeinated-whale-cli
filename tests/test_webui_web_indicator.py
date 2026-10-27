"""The Bench page's per-bench web chip, decided under node.

The fleet's FAST tier checks the web port with a TCP connect so polling writes
nothing to the bench's access log; it reports ``web_listening`` and never an HTTP
code, so a chip keyed on the code alone went permanently blank.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "web-indicator.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _indicator(health, **opts):
    script = (
        f"import {{ webIndicator }} from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify(webIndicator({json.dumps(health)}, {json.dumps(opts)})));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ({"web_http_code": None, "web_listening": True}, {"label": "Listening", "ok": True}),
        (
            {"web_http_code": None, "web_listening": False},
            {"label": "Not listening", "ok": False},
        ),
        ({"web_http_code": "200", "web_listening": None}, {"label": "HTTP 200", "ok": True}),
        ({"web_http_code": "503", "web_listening": None}, {"label": "HTTP 503", "ok": False}),
        ({"web_http_code": None, "web_listening": None}, None),
        (None, None),
    ],
)
def test_the_web_chip(health, expected):
    assert _indicator(health) == expected


UNKNOWN = {"label": "unknown", "ok": None}


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ({"web_http_code": None, "web_listening": True}, UNKNOWN),
        ({"web_http_code": None, "web_listening": False}, UNKNOWN),
        ({"web_http_code": "200", "web_listening": None}, UNKNOWN),
        ({"web_http_code": "503", "web_listening": None}, UNKNOWN),
        ({"web_http_code": None, "web_listening": None}, None),
        (None, None),
    ],
)
def test_a_stale_probe_never_shows_a_live_verdict(health, expected):
    assert _indicator(health, stale="docker.error: Read timed out.") == expected
