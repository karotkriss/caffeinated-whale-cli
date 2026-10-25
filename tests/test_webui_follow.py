"""The web UI's log auto-follow: the pure decisions, run under node.

The captain watched an instance being created and the job page never followed the
output: the page sat at the top while new events landed below the fold, and the
newest output lines were hidden a second time inside the block's own 20rem
scroller. Every log view now renders through ``FollowPane``, a windowed list of
lines that pins to the bottom while the reader is there and pauses only on a
deliberate scroll up, fed by a terminal that processes only newly appended text.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parents[1] / "webui" / "src"
MODULE = WEBUI / "data" / "follow.js"


def _call(name, *args):
    script = (
        f"import {{ {name} }} from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify({name}(...{json.dumps(args)})));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _feed(*chunks):
    """Feed one terminal each chunk in turn; report the lines each feed finished."""
    script = (
        f"import {{ feed, openLine }} from {json.dumps(MODULE.as_uri())};\n"
        "const term = { lines: [], open: '' };\n"
        "const steps = [];\n"
        f"for (const chunk of {json.dumps(chunks)}) {{\n"
        "  const before = term.lines.length;\n"
        "  feed(term, chunk);\n"
        "  steps.push({ added: term.lines.slice(before), open: openLine(term) });\n"
        "}\n"
        "console.log(JSON.stringify(steps));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _at_bottom(scroll_top, scroll_height, client_height):
    return _call(
        "atBottom",
        {"scrollTop": scroll_top, "scrollHeight": scroll_height, "clientHeight": client_height},
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("scroll_top", "scroll_height", "client_height", "expected"),
    [
        (1200, 1600, 400, True),  # pinned to the end
        (1190.5, 1600, 400, True),  # a fractional offset or one line short still follows
        (1000, 1600, 400, False),  # a deliberate scroll up pauses the follow
        (0, 1600, 400, False),  # the unfixed page: sitting at the top while output lands below
        (0, 300, 400, True),  # content shorter than the pane has nowhere else to be
        (1230, 1600, 400, True),  # touch overscroll past the end
    ],
)
def test_the_follow_decision(scroll_top, scroll_height, client_height, expected):
    assert _at_bottom(scroll_top, scroll_height, client_height) is expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("following", "last_top", "scroll_top", "expected"),
    [
        (True, 1200, 1200, True),  # the pin's own queued scroll, read after more output landed
        (True, 1100, 1200, True),  # the pin moved down to an end that has grown since
        (True, 1200, 1000, False),  # the reader scrolled up
        (False, 1000, 1000, False),  # paused stays paused while output lands
        (False, 1000, 1150, False),  # scrolling down but short of the end stays paused
    ],
)
def test_only_a_real_upward_scroll_pauses_the_follow(following, last_top, scroll_top, expected):
    pane = {"scrollTop": scroll_top, "scrollHeight": 1900, "clientHeight": 400}
    assert _call("nextFollowing", following, last_top, pane) is expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_reaching_the_end_resumes_the_follow():
    pane = {"scrollTop": 1500, "scrollHeight": 1900, "clientHeight": 400}
    assert _call("nextFollowing", False, 1000, pane) is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_appended_text_yields_only_the_new_lines():
    assert _feed("first\nsec", "ond\nthird\n", "fourth") == [
        {"added": ["first"], "open": "sec"},
        {"added": ["second", "third"], "open": ""},
        {"added": [], "open": "fourth"},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_progress_bar_keeps_only_its_last_repaint():
    assert _feed("10%\r", "55%\r100%", "\ndone\n") == [
        {"added": [], "open": ""},
        {"added": [], "open": "100%"},
        {"added": ["100%", "done"], "open": ""},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_escape_sequences_are_stripped_even_when_split_across_chunks():
    steps = _feed("\x1b[1mbold\x1b[0m plain\n\x1b[3", "2mgreen\x1b[0m\n")
    assert [step["added"] for step in steps] == [["bold plain"], ["green"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_real_init_sized_block_keeps_every_line():
    """No cap: all 30,000 lines of a real init block stay reachable."""
    lines = [
        f"MO file created at /workspace/frappe-bench/sites/assets/locale/{n}.mo"
        for n in range(30000)
    ]
    text = "\n".join(lines) + "\n"
    steps = _feed(text[:400_000], text[400_000:])
    assert steps[0]["added"] + steps[1]["added"] == lines
    assert steps[1]["open"] == ""
