"""New site's per-app ref helpers (webui/src/data/app-refs.js), executed under node.

The picker shows each bench app at the branch its copy sits at, says which sites a
checkout moves, and sends the plan one ref per target in target order.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "app-refs.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

BENCH = {
    "sites": [
        {
            "name": "a.localhost",
            "installed_apps": ["frappe 16.1.0 version-16", "payments 0.0.1 version-16"],
        },
        {"name": "b.localhost", "installed_apps": ["frappe 16.1.0 version-16"]},
        {
            "name": "c.localhost",
            "installed_apps": ["frappe 16.1.0 version-16", "payments 0.0.1 version-16"],
        },
    ]
}


def _run(call):
    script = (
        f"import {{ benchBranchOf, sitesHolding, branchChangeNote, alignedRefs }} from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify({call} ?? null));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_bench_branch_comes_from_a_site_that_has_the_app():
    assert _run(f"benchBranchOf({json.dumps(BENCH)}, 'payments')") == "version-16"
    assert _run(f"benchBranchOf({json.dumps(BENCH)}, 'hrms')") is None
    assert _run("benchBranchOf(null, 'payments')") is None
    # An entry naming no branch (an older cache) is not a branch.
    bare = {"sites": [{"name": "a", "installed_apps": ["payments 0.0.1"]}]}
    assert _run(f"benchBranchOf({json.dumps(bare)}, 'payments')") is None


def test_the_note_names_every_site_the_checkout_moves():
    note = _run(f"branchChangeNote('payments', 'develop', {json.dumps(BENCH)})")
    assert note == (
        "Create site first checks out develop in the bench's copy of payments (now at version-16), "
        "which moves it for every site on this bench: a.localhost, c.localhost."
    )
    assert _run(f"sitesHolding({json.dumps(BENCH)}, 'payments')") == ["a.localhost", "c.localhost"]


def test_the_ref_the_bench_already_has_is_no_move():
    assert _run(f"branchChangeNote('payments', 'version-16', {json.dumps(BENCH)})") == ""


def test_the_note_says_when_no_site_has_the_app_yet():
    bench = {"sites": [{"name": "a.localhost", "installed_apps": ["frappe 16.1.0 version-16"]}]}
    assert _run(f"branchChangeNote('payments', 'develop', {json.dumps(bench)})") == (
        "Create site first checks out develop in the bench's copy of payments; "
        "no other site on this bench has it installed."
    )


def test_refs_ride_in_target_order_and_vanish_when_none_is_picked():
    targets = ["https://github.com/frappe/payments", "hrms"]
    assert _run(f"alignedRefs({json.dumps(targets)}, {{hrms: 'version-16-hotfix'}})") == [
        "",
        "version-16-hotfix",
    ]
    assert _run(f"alignedRefs({json.dumps(targets)}, {{}})") is None
