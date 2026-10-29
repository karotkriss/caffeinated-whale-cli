"""The web UI's short browser-on-OS label for a session row, executed under node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "user-agent.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# The daemon stores the first 80 characters of an agent, so these are what a row holds.
CHROME_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
HEADLESS_LINUX = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/130.0.0.0 Safari/537.36"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"
EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
SAFARI_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"


def _label(agent):
    script = (
        f"import {{ agentLabel }} from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify(agentLabel({json.dumps(agent)})));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    ("agent", "label"),
    [
        (CHROME_WINDOWS, "Chrome on Windows"),
        (HEADLESS_LINUX, "Headless Chrome on Linux"),
        (FIREFOX, "Firefox on Windows"),
        (EDGE, "Edge on Windows"),
        (IPHONE, "Safari on iOS"),
        (SAFARI_MAC, "Safari on macOS"),
        ("curl/8.5.0", "curl/8.5.0"),
        ("", "unknown browser"),
        (None, "unknown browser"),
    ],
)
def test_full_agents_get_a_browser_and_os(agent, label):
    assert _label(agent) == label


@pytest.mark.parametrize(
    ("agent", "label"),
    [
        (CHROME_WINDOWS[:80], "Browser on Windows"),
        (HEADLESS_LINUX[:80], "Headless Chrome on Linux"),
        (FIREFOX[:80], "Firefox on Windows"),
        (SAFARI_MAC[:80], "Browser on macOS"),
    ],
)
def test_the_stored_80_character_prefix_keeps_the_os_and_never_guesses_the_browser(agent, label):
    assert _label(agent) == label
