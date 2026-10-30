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
    "key": "gitlab.example.com/ministry-of-housing-and-water/chpa/single-window",
    "name": "single_window",
    "title": "Single Window",
    "source": {"url": "https://gitlab.example.com/ministry-of-housing-and-water/chpa/single-window"},
    "installs": [],
}
CUSTOM = {
    "key": "gitlab.example.com/dev-tools/customization",
    "name": "customization",
    "source": {"url": "https://gitlab.example.com/dev-tools/customization"},
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
    on_bench, remote = _split(
        catalog=[SW, CUSTOM], benchApps=["frappe", "single_window"], project="p", bench="0"
    )
    assert on_bench == ["single_window"]
    assert remote == ["customization"]


def test_an_app_on_the_bench_by_repository_is_not_a_remote_pick_even_when_the_list_is_stale():
    """The bench's app list missed it (a detail read before the fetch), but the catalog
    entry for that repository records a checkout on this very bench."""
    sw = {**SW, "installs": [{"project": "p", "bench_index": 0, "remote": SW["key"]}]}
    on_bench, remote = _split(catalog=[sw, CUSTOM], benchApps=["frappe"], project="p", bench="0")
    assert on_bench == ["single_window"]
    assert remote == ["customization"]


def test_a_checkout_on_another_bench_or_instance_still_leaves_it_a_remote_pick():
    sw = {
        **SW,
        "installs": [
            {"project": "p", "bench_index": 1},
            {"project": "other", "bench_index": 0},
        ],
    }
    on_bench, remote = _split(catalog=[sw], benchApps=["frappe"], project="p", bench="0")
    assert on_bench == []
    assert remote == ["single_window"]


def test_a_remote_hosts_bench_ignores_this_machines_installs():
    sw = {**SW, "installs": [{"project": "p", "bench_index": 0}]}
    on_bench, remote = _split(catalog=[sw], benchApps=[], project="p", bench="0", host="h1")
    assert (on_bench, remote) == ([], ["single_window"])


@pytest.mark.parametrize(
    "detail, state",
    [
        (None, "reading"),
        ({"loading": True, "data": {"served_from": "full"}}, "reading"),
        ({"loading": False, "data": None}, "unknown"),
        ({"loading": False, "data": {"served_from": "cache"}}, "remembered"),
        ({"loading": False, "data": {"served_from": "partial"}}, "live"),
        ({"loading": False, "data": {"served_from": "full"}}, "live"),
    ],
)
def test_only_a_live_read_may_claim_what_the_bench_holds(detail, state):
    assert _run(f"inventoryState({json.dumps(detail)})") == state
