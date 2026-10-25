"""The web UI's log auto-follow: the pure decision under node, plus the wiring pin.

The captain watched an instance being created and the job page never followed the
output: the page sat at the top while new events landed below the fold, and the
newest output lines were hidden a second time inside the block's own 20rem
scroller. Every log view now renders through ``FollowPane``, which pins to the
bottom while the reader is there and pauses only on a deliberate scroll up.
"""

import json
import re
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


def test_every_log_view_renders_through_the_follow_pane():
    """A view that scrolls itself can drift back to "scroll it yourself"; none may."""
    for view in ("pages/Job.vue", "components/LogsPane.vue"):
        assert "<FollowPane" in (WEBUI / view).read_text(encoding="utf-8"), view


def test_the_job_output_block_has_no_scroller_of_its_own():
    """A nested scroller hides the newest line even when the pane is pinned to the end."""
    source = (WEBUI / "pages/Job.vue").read_text(encoding="utf-8")
    pre_tags = re.findall(r"<pre[^>]*>", source)
    assert pre_tags, "the job page renders output blocks as <pre>"
    assert not any("overflow" in tag or "max-h" in tag for tag in pre_tags), pre_tags
