"""The web UI's log auto-follow: the pure decisions, run under node.

The captain watched an instance being created and the job page never followed the
output: the page sat at the top while new events landed below the fold, and the
newest output lines were hidden a second time inside the block's own 20rem
scroller. Every log view now renders through ``FollowPane``, which pins to the
bottom while the reader is there and pauses only on a deliberate scroll up.
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
    ("text", "max_lines", "expected"),
    [
        ("a\nb\nc", 2, {"hidden": 1, "text": "b\nc"}),
        ("a\nb", 2, {"hidden": 0, "text": "a\nb"}),  # at the cap, nothing is hidden
        ("only", 5, {"hidden": 0, "text": "only"}),
        ("a\nb\nc\nd\n", 2, {"hidden": 3, "text": "d\n"}),  # a trailing newline is a kept line
    ],
)
def test_the_rendered_tail_of_a_block(text, max_lines, expected):
    """The job page renders only a block's last lines; the count it hides is exact."""
    assert _call("tailLines", text, max_lines) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_real_init_sized_block_keeps_its_last_two_thousand_lines():
    lines = [
        f"MO file created at /workspace/frappe-bench/sites/assets/locale/{n}.mo"
        for n in range(30000)
    ]
    assert _call("tailLines", "\n".join(lines)) == {
        "hidden": 28000,
        "text": "\n".join(lines[28000:]),
    }


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
def test_show_all_renders_every_line_of_a_capped_block():
    """Earlier output stays reachable: the reason a job failed may sit above the tail."""
    text = "\n".join(f"line {n}" for n in range(2500))
    assert _call("blockView", text, {})["hidden"] == 500
    assert _call("blockView", text, {"all": True}) == {
        "hidden": 0,
        "text": text,
        "lines": 2500,
        "total": 2500,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("held", [None, 3])
def test_the_counts_match_the_lines_each_view_renders(held):
    """ "Show all N lines" and "Show last N lines" name what that view renders."""
    raw = "\n\n\x1b[32m\x1b[0m\n" + "\n".join(f"line {n}" for n in range(2500)) + "\n\n"
    collapsed = _call("blockView", raw, {"held": held})
    full = _call("blockView", raw, {"all": True})
    assert full["lines"] == len(full["text"].split("\n")) == 2500
    assert collapsed["total"] == full["lines"]
    assert collapsed["lines"] == len(collapsed["text"].split("\n"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_terminal_keeps_the_last_repaint_of_a_progress_line():
    assert _call("terminal", "\x1b[1mDone\x1b[0m\n10%\r55%\r100%") == "Done\n100%"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_paused_block_keeps_its_top_while_it_grows():
    """While the reader is scrolled up, new lines land below and none leave the top."""
    before = "\n".join(f"line {n}" for n in range(2500))
    held = _call("tailLines", before)["hidden"]
    after = before + "".join(f"\nline {n}" for n in range(2500, 2600))
    view = _call("blockView", after, {"held": held})
    assert view["hidden"] == 500
    assert view["text"] == "\n".join(f"line {n}" for n in range(500, 2600))
    assert _call("blockView", after, {})["hidden"] == 600  # following again trims the top


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("text", "n", "expected"),
    [
        ("a\nb\nc", 0, {"hidden": 0, "text": "a\nb\nc"}),
        ("a\nb\nc", 2, {"hidden": 2, "text": "c"}),
        ("a", 3, {"hidden": 0, "text": "a"}),  # never more than the block holds
    ],
)
def test_dropping_a_blocks_first_lines(text, n, expected):
    assert _call("dropLines", text, n) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("sizes", "following", "expected"),
    [
        ([25000, 10], True, True),  # following trims to the cap, so it always reads on
        ([19999, 10], False, True),  # paused under the limit keeps reading
        ([20000, 10], False, False),  # paused at the limit stops growing, trims nothing
    ],
)
def test_a_paused_logs_buffer_is_bounded(sizes, following, expected):
    assert _call("canAppend", [["x"] * n for n in sizes], following) is expected
