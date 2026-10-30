"""The New site dialog's bench-versus-remote split, executed under node.

An app the bench already holds must never be offered as a remote pick: fetching it
again makes bench get-app clone into apps/<repo slug> and fail renaming that clone
onto the existing apps/<app name> (webui/src/data/bench-apps.js).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "bench-apps.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

SW = {
    "key": "dev.egov.gy/ministry-of-housing-and-water/chpa/single-window",
    "name": "single_window",
    "title": "Single Window",
    "source": {"url": "https://dev.egov.gy/ministry-of-housing-and-water/chpa/single-window"},
    "installs": [],
}
CUSTOM = {
    "key": "dev.egov.gy/dev-tools/customization",
    "name": "customization",
    "source": {"url": "https://dev.egov.gy/dev-tools/customization"},
}


def _run(call):
    script = (
        f"import {{ splitBenchApps, inventoryState }} from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify({call}));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _split(**args):
    out = _run(f"splitBenchApps({json.dumps(args)})")
    return out["onBench"], [a["name"] for a in out["remote"]]


def test_an_app_on_the_bench_by_name_is_not_a_remote_pick():
    on_bench, remote = _split(catalog=[SW, CUSTOM], benchApps=["frappe", "single_window"])
    assert on_bench == ["single_window"]
    assert remote == ["customization"]


def test_only_the_benchs_own_app_list_decides_what_it_holds():
    """The catalog's remembered installs are not a second answer: an app the freshly
    read bench does not list stays a remote pick (the fetch matches it by repository)."""
    sw = {**SW, "installs": [{"project": "p", "bench_index": 0, "remote": SW["key"]}]}
    on_bench, remote = _split(catalog=[sw, CUSTOM], benchApps=["frappe"])
    assert on_bench == []
    assert remote == ["single_window", "customization"]


@pytest.mark.parametrize(
    "detail, state",
    [
        (None, "reading"),
        ({"loading": True, "data": {"served_from": "full"}}, "reading"),
        ({"loading": False, "data": None}, "unknown"),
        ({"loading": False, "data": {"served_from": "cache"}}, "remembered"),
        ({"loading": False, "data": {"served_from": "partial"}}, "live"),
        ({"loading": False, "data": {"served_from": "full"}}, "live"),
        ({"loading": False, "data": {"served_from": "full"}, "error": {}}, "remembered"),
    ],
)
def test_only_a_live_read_may_claim_what_the_bench_holds(detail, state):
    assert _run(f"inventoryState({json.dumps(detail)})") == state
