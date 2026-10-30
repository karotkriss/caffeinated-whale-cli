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
    "key": "gitlab.example.com/example-group/sub-group/field-service",
    "name": "field_service",
    "title": "Field Service",
    "source": {"url": "https://gitlab.example.com/example-group/sub-group/field-service"},
    "installs": [],
}
SW_URL = SW["source"]["url"]
CUSTOM = {
    "key": "gitlab.example.com/tools/customization",
    "name": "customization",
    "source": {"url": "https://gitlab.example.com/tools/customization"},
}


def _run(call):
    script = (
        f"import {{ benchAppFor, splitBenchApps, inventoryState }} from {json.dumps(MODULE.as_uri())};\n"
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
    on_bench, remote = _split(catalog=[SW, CUSTOM], benchApps=["frappe", "field_service"])
    assert on_bench == ["field_service"]
    assert remote == ["customization"]


def test_only_the_benchs_own_app_list_decides_what_it_holds():
    """The catalog's remembered installs are not a second answer: an app the freshly
    read bench does not list stays a remote pick (the fetch matches it by repository)."""
    sw = {**SW, "installs": [{"project": "p", "bench_index": 0, "remote": SW["key"]}]}
    on_bench, remote = _split(catalog=[sw, CUSTOM], benchApps=["frappe"])
    assert on_bench == []
    assert remote == ["field_service", "customization"]


def test_an_app_on_the_bench_by_repository_is_not_a_remote_pick_whatever_its_name():
    """The catalog names the repository's app differently from the bench's folder,
    but the bench detail's remote for that checkout is the catalog entry's repo."""
    sw = {**SW, "name": "field-service"}
    on_bench, remote = _split(
        catalog=[sw, CUSTOM],
        benchApps=["frappe", "field_service"],
        benchRemotes={"field_service": [SW["source"]["url"]]},
    )
    assert on_bench == ["field_service"]
    assert remote == ["customization"]


@pytest.mark.parametrize(
    "bench, held_as",
    [
        (
            {"benchApps": ["frappe", "field_service"], "benchRemotes": {"field_service": [SW_URL]}},
            "field_service",
        ),
        ({"benchApps": ["frappe", "field-service"]}, "field-service"),
        ({"benchApps": ["frappe"], "benchRemotes": None}, None),
    ],
)
def test_a_remote_pick_maps_to_the_bench_app_that_holds_its_repository(bench, held_as):
    """A remote pick a fresh read finds on the bench is kept as that bench app, found
    by repository whatever the catalog calls it (NewSiteDialog's picker watcher)."""
    sw = {**SW, "name": "field-service"}
    assert _run(f"benchAppFor({json.dumps(sw)}, {json.dumps(bench)})") == held_as


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
