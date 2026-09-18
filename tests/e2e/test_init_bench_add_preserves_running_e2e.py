"""Real-Docker E2E regression: adding a bench must never disturb the frappe
container or any bench already serving inside it.

The defect (observed 2026-07-25, during the first real parallel multibench
delivery): ``init_instance`` unconditionally ran ``docker compose pull`` then
``docker compose up -d`` on EVERY ``cwcli init`` call, including one that only
adds a bench to an already-running instance. ``pull`` re-fetches every image,
including the compose template's unpinned ``redis:alpine`` tag and MariaDB's
own tag; without ``--no-deps``/``--force-recreate``, ``up -d`` silently
RECREATES any container whose freshly-pulled image no longer matches what is
running - for the frappe service that kills every already-serving bench's
supervisord, with nothing in the report to say so. The fix (``core/init.py``,
``_running_compose_services``) always skips the image pull once this project's
own frappe container is confirmed running. When every expected dependency is
also running, it skips ``up`` entirely. When MariaDB or either Redis service is
stopped, it starts only the missing siblings with ``up -d --no-deps`` so
Compose cannot touch or recreate frappe.

This is the container/process-level proof the fix requires: real Docker, no
mocks. It cannot force the ORIGINAL trigger (a genuine upstream image update
between two ``pull`` calls, which no hermetic test controls), so it instead
pins the property the fix makes UNCONDITIONALLY true - the frappe container's
identity and process the bench depends on are untouched by a bench-add,
regardless of what a `pull` would have fetched. Any regression that
reintroduces a pull or includes frappe in an ``up`` target on this path breaks
it the same way the original defect would have: a changed container id, a
changed start time, or a bench that stops answering.

Builds its own two-bench instance (the second bench cannot be un-added), so it
is ``standalone``, matching ``test_multibench_serving_e2e.py``'s ``two_benches``
precedent. Version-agnostic (the risk lives in ``init_instance``'s host-command
gating, not in anything Frappe-version-specific), so it runs once on the v16 leg.

A second test below covers the DEFAULT ``--start`` path the first one
deliberately excludes with ``--no-start``: after provisioning the new bench,
``cwcli init`` also launches its dev services (``core.start`` -> a fresh
supervisord for that bench alone). ``test_multibench_serving_e2e.py``'s
``two_benches`` fixture already exercises this path, but it calls
``_serve_both`` (start-if-not-serving) as its precondition, which would
silently RESTART a first bench that this defect had stopped rather than
catching it - so that fixture cannot prove the property this file exists to
pin. The test here checks bench 1's container identity and live HTTP answer
immediately after the add, before anything could paper over an interruption.
"""

from __future__ import annotations

import json

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW
from .test_start_status_e2e import _wait_web_ready

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="the pull/up skip lives in init_instance's host-command gating, not "
    "anything Frappe-version-specific; v16 leg only",
)


def _started_at(container_id: str) -> str:
    r = harness._docker("inspect", "-f", "{{.State.StartedAt}}", container_id)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


@v16_only
def test_adding_a_bench_leaves_the_first_bench_genuinely_serving(port_allocator):
    """Positive-first: bench 1 genuinely serves, then a real bench-add happens,
    then bench 1 is proven to still be the SAME container process, still serving
    - never restarted, never recreated, never even asked to stop."""
    harness.enforce_isolation()
    name = harness.project_name("addbench")
    port = port_allocator.next()
    common = (
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
    )
    try:
        first = harness.run_cwcli(
            "init", name, *common, "--auto-start", timeout=harness.INIT_TIMEOUT
        )
        assert first.returncode == 0, first.stdout + first.stderr
        harness.wait_for_site_ready(name, harness.DEFAULT_SITE, bench=harness.DEFAULT_BENCH_PATH)
        _wait_web_ready(name)

        # POSITIVE FIRST: bench 1 is genuinely up before anything touches the
        # instance again - a container that never came up would make every
        # assertion below pass vacuously.
        frappe_before = harness.frappe_container_id(name)
        assert frappe_before, "no frappe container found for the first bench"
        started_before = _started_at(frappe_before)
        code, _ = harness.exec_in_frappe(
            name, "curl -s --max-time 5 -o /dev/null http://localhost:8000"
        )
        assert code == 0, "bench 1's web server is not reachable before the add"

        # THE ACT UNDER TEST: add a second bench to the already-running instance -
        # the ordinary way a developer (or a second concurrent task) grows one.
        second = harness.run_cwcli(
            "init",
            name,
            *common,
            "--bench",
            "frappe-bench-2",
            "--site",
            "second.localhost",
            "--no-start",
            timeout=harness.INIT_TIMEOUT,
        )
        assert second.returncode == 0, second.stdout + second.stderr

        # THE PROOF: the frappe container is the SAME container - same id, same
        # start time - so it was never recreated, and therefore never lost the
        # supervisord process (and every bench under it) that was running inside
        # it. This is the discriminator the old unconditional pull+up could fail:
        # an image-drift-triggered recreate would change both.
        frappe_after = harness.frappe_container_id(name)
        assert frappe_after == frappe_before, (
            "the frappe container was recreated by adding a second bench - "
            "every already-serving bench's supervisord was lost with it"
        )
        assert (
            _started_at(frappe_after) == started_before
        ), "the frappe container was restarted by adding a second bench"

        # And bench 1 is still genuinely serving - not merely "up again" after a
        # restart, but never interrupted in the first place.
        code, _ = harness.exec_in_frappe(
            name, "curl -s --max-time 5 -o /dev/null http://localhost:8000"
        )
        assert code == 0, "bench 1 stopped serving after a second bench was added"
    finally:
        harness.cwcli_rm(name)


@v16_only
def test_adding_and_starting_a_second_bench_leaves_the_first_genuinely_serving(port_allocator):
    """The scenario the previous test deliberately does NOT cover: the ordinary,
    default `cwcli init`/`cwcli axi init` call (`--start` is the default) also
    starts the NEW bench's own dev services (`core.start` -> supervisord launch)
    after provisioning it. That launch is scoped to the new bench's own resolved
    path (a fresh supervisord config/socket/launcher under its OWN `logs/` dir)
    and never touches a sibling bench's supervisord, but that property was never
    proven on real Docker - the sibling test above passes `--no-start` on the add
    specifically to isolate the container-recreation defect, so it never exercises
    this code path at all.

    Positive-first, same as above: bench 1 is proven genuinely serving before the
    add, the add uses the DEFAULT start behavior (no `--no-start`), and bench 1's
    container identity plus its live HTTP answer are checked immediately after -
    not merely "serving again" after some cleanup restarted it, but never
    interrupted. Bench 2 is also proven serving, so a silently-skipped or
    silently-failed start on the new bench cannot pass this test by accident.
    """
    harness.enforce_isolation()
    name = harness.project_name("addbenchstart")
    port = port_allocator.next()
    common = (
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
    )
    second_site = "second.localhost"
    second_bench_path = "/workspace/frappe-bench-2"
    try:
        first = harness.run_cwcli(
            "init", name, *common, "--auto-start", timeout=harness.INIT_TIMEOUT
        )
        assert first.returncode == 0, first.stdout + first.stderr
        harness.wait_for_site_ready(name, harness.DEFAULT_SITE, bench=harness.DEFAULT_BENCH_PATH)
        _wait_web_ready(name)

        frappe_before = harness.frappe_container_id(name)
        assert frappe_before, "no frappe container found for the first bench"
        started_before = _started_at(frappe_before)
        code, _ = harness.exec_in_frappe(
            name, "curl -s --max-time 5 -o /dev/null http://localhost:8000"
        )
        assert code == 0, "bench 1's web server is not reachable before the add"

        # THE ACT UNDER TEST: add and START a second bench - the actual default
        # behavior of `cwcli init` / `cwcli axi init` (`--start` unless overridden).
        second = harness.run_cwcli(
            "init",
            name,
            *common,
            "--bench",
            "frappe-bench-2",
            "--site",
            second_site,
            timeout=harness.INIT_TIMEOUT,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        harness.wait_for_site_ready(name, second_site, bench=second_bench_path)

        # The second bench's container-internal port comes from bench's OWN
        # make_ports, read back from its config rather than assumed, so this
        # test does not silently depend on a specific port-assignment order.
        code, out = harness.exec_in_frappe(
            name, f"cat {second_bench_path}/sites/common_site_config.json"
        )
        assert code == 0, out
        second_port = json.loads(out)["webserver_port"]

        # THE PROOF: same container, never recreated or restarted.
        frappe_after = harness.frappe_container_id(name)
        assert frappe_after == frappe_before, (
            "the frappe container was recreated by adding and starting a second "
            "bench - every already-serving bench's supervisord was lost with it"
        )
        assert (
            _started_at(frappe_after) == started_before
        ), "the frappe container was restarted by adding and starting a second bench"

        # Bench 1 answered continuously - not restarted back to life afterward.
        code, _ = harness.exec_in_frappe(
            name, "curl -s --max-time 5 -o /dev/null http://localhost:8000"
        )
        assert code == 0, "bench 1 stopped serving after a second bench was added and started"

        # Bench 2 genuinely came up too, so the add did real work rather than
        # silently skipping or failing the new bench's own start.
        code, _ = harness.exec_in_frappe(
            name,
            f'curl -s --max-time 5 -o /dev/null -H "Host: {second_site}" '
            f"http://localhost:{second_port}",
        )
        assert code == 0, "the newly added second bench never started serving"
    finally:
        harness.cwcli_rm(name)
