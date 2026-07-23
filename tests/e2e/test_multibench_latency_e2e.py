"""``report-status-per-bench`` task 5.5, the six-bench half: how ``cwcli status``
scales with bench count on a real instance whose benches genuinely serve.

The open question from that change's Decision 7 is whether the per-bench probe
cost justifies hoisting ``discover_stack``'s container-wide ``ps`` to one call per
invocation. It was deliberately left unspecified with a measurement attached
rather than pre-optimised, and the measurement needs benches that are actually
running: a bench that is not serving skips the ``supervisorctl`` read and gets an
instant connection refusal in place of a real HTTP response, so it measures less
than the thing being asked about.

Six is the meaningful ceiling: an instance publishes six web ports at init, and a
seventh bench binds a port inside the container that nothing outside can reach
(that is what ``cwcli scale`` widens).

**This is OPT-IN and skips by default.** It builds up to six full benches - each
one a real ``bench init`` virtualenv plus a real site, roughly 1.5 GB of disk and a
couple of minutes of build apiece - which is far too much to spend on every pull
request for a number that only needs taking when the probe changes. Set
``CWE2E_LATENCY_BENCHES=6`` to run it; the ``latency_benches`` input on the E2E
workflow does exactly that, so the measurement is taken on the same CI runners
every other real-instance proof runs on, not on somebody's laptop. The recorded
numbers live in ``docs/e2e/multibench-serving-status.md``.

The two-bench half of 5.5 is NOT here: it rides the two-serving-bench fixture in
``test_multibench_serving_e2e.py``, so it costs nothing extra and runs on every
v16 leg.
"""

from __future__ import annotations

import os

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW
from .test_multibench_serving_e2e import (
    FIRST_BENCH_PATH,
    FIRST_SITE,
    _assigned_web_port,
    _http_code,
    _median_seconds,
    _wait_code,
)

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]


def _requested_bench_count() -> int:
    """How many benches the operator asked for, 0 (skip) when unset or unparseable."""
    try:
        return int(os.environ.get("CWE2E_LATENCY_BENCHES", "") or 0)
    except ValueError:
        return 0


BENCH_COUNT = _requested_bench_count()

opt_in = pytest.mark.skipif(
    BENCH_COUNT < 2,
    reason=(
        "the six-bench latency measurement builds a real bench per data point "
        "(~1.5 GB and ~2 min each), so it is opt-in: set CWE2E_LATENCY_BENCHES=6 "
        "(or run the E2E workflow with the latency_benches input) to take it"
    ),
)


@opt_in
@pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="probe cost is version-agnostic; the measurement is taken on the v16 leg",
)
def test_status_latency_by_bench_count(port_allocator, capsys):
    """Measure instance-wide ``cwcli axi status`` latency at 1..N serving benches.

    The instance grows one real bench at a time and is re-measured after each, so
    a single (expensive) instance yields the whole curve rather than one point,
    and the per-bench marginal cost is read off the differences instead of being
    inferred from a single total.

    Every bench is genuinely serving before it is counted, asserted rather than
    assumed - a measurement taken over benches that were not up would understate
    exactly the cost the hoist question is about.
    """
    name = harness.project_name("mblat")
    port = port_allocator.next()
    common = (
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
        "--auto-start",
    )
    rows: list[tuple[int, float, int]] = []
    try:
        for n in range(1, BENCH_COUNT + 1):
            if n == 1:
                bench_path, site = FIRST_BENCH_PATH, FIRST_SITE
                res = harness.run_cwcli("init", name, *common, timeout=harness.INIT_TIMEOUT)
            else:
                bench_name = f"{harness.DEFAULT_BENCH_NAME}-{n}"
                bench_path, site = f"/workspace/{bench_name}", f"bench{n}.localhost"
                res = harness.run_cwcli(
                    "init",
                    name,
                    *common,
                    "--bench",
                    bench_name,
                    "--site",
                    site,
                    timeout=harness.INIT_TIMEOUT,
                )
            assert res.returncode == 0, res.stdout + res.stderr
            harness.wait_for_site_ready(name, site, bench=bench_path)

            # The new bench must genuinely serve on its OWN port before it counts
            # towards a measurement, and its port must be distinct from every
            # bench already counted.
            web_port = _assigned_web_port(name, bench_path)
            assert web_port not in {p for _, _, p in rows}, (
                f"bench {n} was assigned port {web_port}, already in use by an "
                f"earlier bench - the instance is not really {n} serving benches"
            )
            _wait_code(name, web_port, "200", site)
            assert _http_code(name, web_port, site) == "200"

            insp = harness.run_cwcli("inspect", name, "--update")
            assert insp.returncode == 0, insp.stdout + insp.stderr

            median = _median_seconds(lambda: harness.run_cwcli("axi", "status", name))
            rows.append((n, median, web_port))
            with capsys.disabled():
                print(f"\n[cwcli-latency] axi status over {n} serving bench(es): {median:.2f}s")

        with capsys.disabled():
            print(f"\n[cwcli-latency] {harness.FRAPPE_BRANCH} instance-wide `cwcli axi status`")
            previous = None
            for n, median, web_port in rows:
                marginal = "" if previous is None else f"  (+{median - previous:.2f}s)"
                print(f"[cwcli-latency]   {n} bench(es), port {web_port}: {median:.2f}s{marginal}")
                previous = median
    finally:
        harness.cwcli_rm(name)
