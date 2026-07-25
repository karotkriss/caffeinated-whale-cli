"""A REMOVED bench must stop being reported as present - proven against a real one.

The defect this guards: after a bench directory was removed, the bench listing kept
reporting that bench across repeated probes while both the host filesystem and the
container agreed it was gone. Unit fakes cannot prove the fix, because a fake can
only agree with itself about what exists; the whole question is whether cwcli's
answer tracks a real filesystem. So this test removes a real directory from a real
container and reads the answer back through the real binary.

Every test here asserts the POSITIVE FIRST - that a live bench is still reported,
and reported ``present`` - before asserting that the removed one is not. That order
is load-bearing, not stylistic: a "verification" that answered "gone" about
everything would satisfy the removed-bench half perfectly while destroying the verb.
The goal is a cached answer that is either correct or labelled, never a cache
weakened into uselessness.

The bench under test is a decoy created for this test, a directory satisfying
exactly the shape cwcli calls a bench, registered as a search path so a real
``cwcli inspect`` discovers and caches it like any other. The session's real bench
stands beside it the whole time as the positive control, and every test restores the
search path and the cache it touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from . import harness

pytestmark = pytest.mark.e2e

#: A sibling of the session bench, so it lands on the same real bind mount.
DECOY = "/workspace/cwe2e-stale-bench"
DECOY_NAME = DECOY.rsplit("/", 1)[1]


def _decoy_host_path(inst) -> Path:
    """Where the decoy lives on the HOST, through the ``/workspace`` bind mount."""
    return Path(os.environ["CWCLI_HOME"]) / "projects" / inst.name / "data" / DECOY_NAME


def _exists_in_container(inst, path: str) -> bool:
    code, _ = harness.exec_in_frappe(inst.name, f"test -d {path}")
    return code == 0


def _gone_everywhere(inst) -> None:
    """Assert the removal is real on BOTH sides - the condition the report described.

    "The host filesystem and the container agree it is gone" is the exact state the
    tool was contradicting, so the test has to establish it rather than assume it.
    """
    assert not _exists_in_container(inst, DECOY), "the decoy survived inside the container"
    assert not _decoy_host_path(inst).exists(), "the decoy survived on the host"


def _create_decoy(inst) -> None:
    code, out = harness.exec_in_frappe(
        inst.name,
        f"mkdir -p {DECOY}/sites {DECOY}/apps && "
        f"printf '{{}}' > {DECOY}/sites/common_site_config.json",
    )
    assert code == 0, f"could not create the decoy bench: {out}"


def _remove_decoy(inst) -> None:
    harness.exec_in_frappe(inst.name, f"rm -rf {DECOY}")


def _bench_row(toon: str, bench_path: str) -> str:
    """The one TABLE row of an ``axi benches`` document describing ``bench_path``.

    Scoped to the ``benches[N]{...}`` block on purpose: a warning naming the same
    path is prose, and reading a state token out of prose is exactly what the
    per-row token exists to make unnecessary.
    """
    lines = toon.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("benches["))
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start + 1) if not line.startswith("  ")),
        len(lines),
    )
    rows = [line for line in lines[start + 1 : end] if f",{bench_path}," in line]
    assert len(rows) == 1, f"expected exactly one row for {bench_path} in:\n{toon}"
    return rows[0]


def _benches_toon(inst) -> str:
    result = harness.run_cwcli("axi", "benches", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    return harness.strip_ansi(result.stdout)


@pytest.fixture()
def instance_with_decoy_bench(running_instance):
    """The session instance plus a second, genuinely cached bench to remove.

    Yields ``(instance, real_bench_path)``. The decoy is registered as a search
    path and cached by a real full inspect, so cwcli reaches it by exactly the
    route it reaches any bench. Both the path and the cache are restored after.
    """
    inst = running_instance
    _create_decoy(inst)
    added = harness.run_cwcli("config", "paths", "add", DECOY)
    assert added.returncode == 0, added.stdout + added.stderr
    try:
        seeded = harness.run_cwcli("inspect", inst.name, "--update")
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr
        assert DECOY in _benches_toon(inst), "setup: the decoy bench was not cached"
        yield inst, harness.DEFAULT_BENCH_PATH
    finally:
        _remove_decoy(inst)
        harness.run_cwcli("config", "paths", "remove", DECOY)
        # Re-derive the cache from the live container so the shared session
        # instance is left exactly as found, with no decoy row behind it.
        harness.run_cwcli("inspect", inst.name, "--update")


def test_benches_stops_vouching_for_a_removed_bench(instance_with_decoy_bench):
    """``cwcli axi benches`` - the verb the defect was reported against."""
    inst, real_bench = instance_with_decoy_bench

    # POSITIVE FIRST: both benches exist and both are reported present. Without
    # this, "the removed one is absent" would pass against a check that simply
    # never says present.
    before = _benches_toon(inst)
    assert "verified: true" in before, before
    assert _bench_row(before, real_bench).endswith(",present")
    assert _bench_row(before, DECOY).endswith(",present")

    # Remove it for real, and confirm reality agrees on BOTH sides of the mount -
    # the condition the original report described.
    _remove_decoy(inst)
    _gone_everywhere(inst)

    # REPEATED probes: the defect was that the stale answer survived re-asking,
    # so one read is not enough to prove it gone.
    for probe in range(3):
        after = _benches_toon(inst)
        # The live bench is STILL reported, still present - the cache was not
        # weakened into uselessness to get the other answer right.
        real_row = _bench_row(after, real_bench)
        assert real_row.endswith(",present"), f"probe {probe}: {real_row}"
        row = _bench_row(after, DECOY)
        assert row.endswith(",absent"), f"probe {probe}: removed bench still vouched for: {row}"
        # Reported, never silently pruned; and the remedy is named.
        assert DECOY in after
        assert "inspect" in after


def test_status_distinguishes_a_removed_bench_from_a_never_started_one(
    instance_with_decoy_bench,
):
    """``cwcli axi status`` - the same class, reached by the other read verb.

    A removed bench has no supervisor marker and no supervisord, which is exactly
    what a bench that was simply never started looks like, so it reported
    ``overall: online`` - "here, just not up" - about a directory that was gone. No
    amount of live health probing settles that; only asking whether the directory
    is there does.
    """
    inst, real_bench = instance_with_decoy_bench

    result = harness.run_cwcli("axi", "status", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    before = harness.strip_ansi(result.stdout)
    # POSITIVE FIRST: every bench present, including the never-started decoy.
    assert before.count("bench_present: present") == 2, before

    _remove_decoy(inst)
    _gone_everywhere(inst)

    result = harness.run_cwcli("axi", "status", inst.name)
    assert result.returncode == 0, result.stdout + result.stderr
    after = harness.strip_ansi(result.stdout)

    # The real bench's health read is untouched: presence is reported ALONGSIDE
    # health, never folded into `overall`.
    assert "bench_present: present" in after, after
    assert "bench_present: absent" in after, after
    real_block = after.split(real_bench, 1)[1].split("bench_path:", 1)[0]
    assert "bench_present: present" in real_block, real_block


def test_a_stopped_project_says_unverified_rather_than_present(instance_with_decoy_bench):
    """The honest half of the rule: with nothing to ask, claim nothing.

    ``benches`` is what tells a caller which ``--bench`` to pass to ``cwcli start``,
    so it must keep answering on a project that is by definition stopped. It just
    may not report ``present``, and it must not report ``absent`` either - an
    unanswerable check is not a confirmation in either direction.
    """
    inst, real_bench = instance_with_decoy_bench
    try:
        stopped = harness.run_cwcli("stop", inst.name)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr

        toon = _benches_toon(inst)
        # Still a usable answer: the addressing this verb exists to serve is intact.
        assert "verified: false" in toon, toon
        for path in (real_bench, DECOY):
            row = _bench_row(toon, path)
            assert row.endswith(",unverified"), row
    finally:
        harness.run_cwcli("start", inst.name, "--yes")
        harness.wait_for_site_ready(inst.name, inst.site)
