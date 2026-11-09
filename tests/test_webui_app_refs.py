"""New site's per-app ref helpers (webui/src/data/app-refs.js), executed under node.

The picker shows each bench app at the branch its copy sits at and says which sites a
checkout moves, both from the live plan (the job's own decision), and sends the plan one
ref per target in target order.
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
        f"import {{ liveBranchOf, movesTo, sitesHolding, branchChangeNote, alignedRefs }} from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify({call} ?? null));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _plan(**node):
    """The plan endpoint's shape (core.app_deps.plan_as_dict) for one picked bench app."""
    base = {"app": "payments", "requested": True, "requested_as": "payments", "checkout": False}
    return {"nodes": [base | node]}


def test_the_bench_branch_is_the_one_the_plan_read_live():
    # A cached `installed_apps` entry naming another branch does not count: the job
    # decides on the copy's live .git/HEAD, which is what the plan reports.
    assert _run(f"liveBranchOf({json.dumps(_plan(ref='develop'))}, 'payments')") == "develop"
    assert _run(f"liveBranchOf({json.dumps(_plan(ref='develop'))}, 'hrms')") is None
    assert _run("liveBranchOf(null, 'payments')") is None
    # A checkout node names the ref asked for, not the branch the copy sits at.
    checkout = _plan(ref="develop", checkout=True)
    assert _run(f"liveBranchOf({json.dumps(checkout)}, 'payments')") is None


def test_the_note_names_every_site_the_checkout_moves():
    plan = json.dumps(_plan(ref="develop"))
    note = _run(f"branchChangeNote('payments', 'version-16', {json.dumps(BENCH)}, {plan})")
    assert note == (
        "Create site first checks out version-16 in the bench's copy of payments (now at develop), "
        "which moves it for every site on this bench: a.localhost, c.localhost."
    )
    assert _run(f"sitesHolding({json.dumps(BENCH)}, 'payments')") == ["a.localhost", "c.localhost"]


def test_a_planned_checkout_is_a_move_even_when_the_cache_names_that_branch():
    plan = json.dumps(_plan(ref="develop", checkout=True))
    assert _run(f"branchChangeNote('payments', 'develop', {json.dumps(BENCH)}, {plan})") == (
        "Create site first checks out develop in the bench's copy of payments, "
        "which moves it for every site on this bench: a.localhost, c.localhost."
    )


def test_the_ref_the_copy_already_sits_at_is_no_move():
    # The cache says version-16, but the copy is live on develop: picking develop moves nothing.
    plan = json.dumps(_plan(ref="develop"))
    assert _run(f"branchChangeNote('payments', 'develop', {json.dumps(BENCH)}, {plan})") == ""


def test_the_move_is_said_as_a_condition_while_the_plan_has_not_planned_the_pick():
    # A pick made in the picker is not in the plan yet; one changed in the picker was
    # planned at its old ref. Neither may read as "nothing moves".
    unsure = (
        "Unless the bench's copy of payments already sits at develop, Create site first "
        "checks it out there, which moves it for every site on this bench: a.localhost, c.localhost."
    )
    assert _run(f"branchChangeNote('payments', 'develop', {json.dumps(BENCH)}, null)") == unsure
    stale = json.dumps(_plan(ref="other", checkout=True))
    assert _run(f"branchChangeNote('payments', 'develop', {json.dumps(BENCH)}, {stale})") == unsure


def test_only_a_move_the_plan_confirms_counts_as_one():
    assert (
        _run(f"movesTo({json.dumps(_plan(ref='develop', checkout=True))}, 'payments', 'develop')")
        is True
    )
    assert _run(f"movesTo({json.dumps(_plan(ref='develop'))}, 'payments', 'version-16')") is True
    assert _run(f"movesTo({json.dumps(_plan(ref='develop'))}, 'payments', 'develop')") is False
    assert (
        _run(f"movesTo({json.dumps(_plan(ref='other', checkout=True))}, 'payments', 'develop')")
        is None
    )
    assert _run("movesTo(null, 'payments', 'develop')") is None
    assert _run("movesTo(null, 'payments', '')") is False


def test_the_note_says_when_no_site_has_the_app_yet():
    bench = {"sites": [{"name": "a.localhost", "installed_apps": ["frappe 16.1.0 version-16"]}]}
    plan = json.dumps(_plan(ref="version-16"))
    assert _run(f"branchChangeNote('payments', 'develop', {json.dumps(bench)}, {plan})") == (
        "Create site first checks out develop in the bench's copy of payments (now at version-16); "
        "no other site on this bench has it installed."
    )


def test_refs_ride_in_target_order_and_vanish_when_none_is_picked():
    targets = ["https://github.com/frappe/payments", "hrms"]
    assert _run(f"alignedRefs({json.dumps(targets)}, {{hrms: 'version-16-hotfix'}})") == [
        "",
        "version-16-hotfix",
    ]
    assert _run(f"alignedRefs({json.dumps(targets)}, {{}})") is None
