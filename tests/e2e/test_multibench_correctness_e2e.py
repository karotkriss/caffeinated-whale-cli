"""Real-Docker E2E for the multibench correctness fixes.

Four defects that only show up against a real bench, each proven here by
asserting the POSITIVE before the negative - a check that would also pass
against a dead instance proves nothing:

  - ``restart --bench N`` relaunches the bench it was NAMED. The selector reached
    only the ``--process`` branch, so the whole-stack path silently relaunched a
    different bench (the resolver's "first" fallback) and left the named one down,
    at exit 0.
  - ``status``'s web probe names the bench's SITE as its ``Host`` header. Frappe
    routes by Host, so a host-less probe is correctly answered 404 by a perfectly
    healthy bench - and every read printed that 404.
  - ``stop --bench`` stops ONE bench's dev processes. There was no verb for it, so
    the only way to take one bench down was ``docker exec ... supervisorctl``.
  - the ``init`` banner and ``cwcli open`` report the bench's REAL host address.
    The banner hardcoded ``:8000``, a CONTAINER port, so under any ``--port`` base
    it named an address nothing answers on.

Runs on the shared session instance; every test that leaves it un-served relies
on ``_ensure_serving`` to restore sibling tests.
"""

from __future__ import annotations

import json
import re

import pytest

from . import harness
from .test_start_status_e2e import _ensure_serving, _wait_web_ready, _web_reachable

pytestmark = pytest.mark.e2e

BENCH = harness.DEFAULT_BENCH_PATH
_MARKER = f"{BENCH}/logs/.cwcli-supervisor.json"


def _supervisord_count(project: str) -> int:
    """How many supervisord supervisors are live in the container."""
    code, out = harness.exec_in_frappe(project, "ps -eo args | grep -c '[s]upervisord' || true")
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def _container_web_port(project: str) -> int:
    """The port THIS bench serves inside the container, from bench's own config."""
    code, out = harness.exec_in_frappe(project, f"cat {BENCH}/sites/common_site_config.json")
    assert code == 0, out
    return int(json.loads(out)["webserver_port"])


def _published_host_port(project: str, container_port: int) -> int:
    """The host port that container port is published on, from Docker itself."""
    cid = harness.frappe_container_id(project)
    assert cid is not None, "the frappe container must be running"
    proc = harness._docker("port", cid, f"{container_port}/tcp")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return int(proc.stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def _bench_block(toon: str, bench_path: str) -> str:
    """The slice of an ``axi status`` document describing one bench."""
    assert bench_path in toon, toon
    return toon.split(bench_path, 1)[1]


# --------------------------------------------------------------------------- #
# 1. the web probe names the bench's site, so a healthy bench reads 200
# --------------------------------------------------------------------------- #
def test_status_probes_with_the_site_so_a_healthy_bench_is_not_404(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    port = _container_web_port(inst.name)

    # POSITIVE FIRST: the bench genuinely serves 200 for its own site. Without this
    # the "no 404" assertion below would pass against a bench serving nothing.
    code, served = harness.exec_in_frappe(
        inst.name,
        f'curl -s -o /dev/null -w "%{{http_code}}" -H "Host: {inst.site}" '
        f"http://localhost:{port}",
    )
    assert code == 0 and served.strip() == "200", f"setup: bench must serve 200, got {served!r}"

    # And the SAME request without a Host header is answered 404 - not a fault, just
    # Frappe correctly refusing to guess which site was meant. This is the number
    # status used to print.
    code, hostless = harness.exec_in_frappe(
        inst.name, f'curl -s -o /dev/null -w "%{{http_code}}" http://localhost:{port}'
    )
    assert (
        code == 0 and hostless.strip() == "404"
    ), f"setup: a host-less request is expected to be 404 on multi-tenant Frappe, got {hostless!r}"

    res = harness.run_cwcli("axi", "status", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    block = _bench_block(res.stdout, BENCH)
    assert f"web_site: {inst.site}" in block, res.stdout
    assert 'web_http_code: "200"' in block, res.stdout
    assert '"404"' not in block, res.stdout

    # The human surface attributes it the same way: the site AND the port.
    human = harness.run_cwcli("status", inst.name, "-v")
    assert human.returncode == 0, human.stdout + human.stderr
    assert f"web {inst.site}:{port} -> 200" in harness.strip_ansi(human.stderr), human.stderr


def test_the_status_probe_stays_a_pure_read(running_instance):
    """A read verb must never start anything - doubly so on a shared daemon, where a
    status that started containers would resurrect a deliberately stopped instance."""
    inst = running_instance
    _ensure_serving(inst.name)

    # Take the bench down deliberately, then read it twice.
    stop = harness.run_cwcli("axi", "stop", inst.name, "--bench", "0")
    assert stop.returncode == 0, stop.stdout + stop.stderr
    harness.wait_until(
        lambda: not _web_reachable(inst.name), desc="bench stops serving after a bench stop"
    )
    try:
        for _ in range(2):
            res = harness.run_cwcli("axi", "status", inst.name)
            assert res.returncode == 0, res.stdout + res.stderr
        assert not _web_reachable(inst.name), "status must not have restarted the bench"
        assert _supervisord_count(inst.name) == 0, "status must not have launched a supervisor"
    finally:
        _ensure_serving(inst.name)
        _wait_web_ready(inst.name)


# --------------------------------------------------------------------------- #
# 2. stop --bench: one bench down, containers and siblings untouched
# --------------------------------------------------------------------------- #
def test_axi_stop_bench_stops_the_bench_and_leaves_the_containers_running(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    # POSITIVE FIRST: the bench is genuinely serving and supervised before the stop.
    assert _web_reachable(inst.name), "setup: the bench must be serving"
    assert _supervisord_count(inst.name) == 1, "setup: exactly one supervisord"
    before = set(harness.project_containers(inst.name))
    assert before, "setup: the instance's containers are up"

    try:
        res = harness.run_cwcli("axi", "stop", inst.name, "--bench", "0")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "already_stopped: false" in res.stdout, res.stdout
        assert f"bench_path: {BENCH}" in res.stdout, res.stdout
        # The programs it ended are named, each once - discovery is per-PID and one
        # program can hold several processes.
        procs = re.search(r"stopped_processes\[\d+\]: (.+)", res.stdout)
        assert procs, res.stdout
        labels = procs.group(1).split(",")
        assert "web" in labels, res.stdout
        assert len(labels) == len(set(labels)), f"labels must be deduplicated: {labels}"

        harness.wait_until(
            lambda: not _web_reachable(inst.name), desc="the stopped bench stops serving"
        )
        assert _supervisord_count(inst.name) == 0, "the bench's supervisord is gone"
        # THE point: the containers are untouched. Stopping one bench is not
        # stopping the instance.
        assert set(harness.project_containers(inst.name)) == before, "containers must stay up"

        # A deliberate stop is not a fault: the marker is cleared, so status reads
        # `online` (never started) rather than `degraded` (started, supervisor died).
        st = harness.run_cwcli("axi", "status", inst.name)
        assert st.returncode == 0, st.stdout + st.stderr
        assert "container_running: true" in st.stdout, st.stdout
        assert "overall: online" in st.stdout, st.stdout
        assert "degraded" not in st.stdout, st.stdout

        # Idempotent: an agent can stop twice.
        again = harness.run_cwcli("axi", "stop", inst.name, "--bench", "0")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "already_stopped: true" in again.stdout, again.stdout
    finally:
        _ensure_serving(inst.name)
        _wait_web_ready(inst.name)


def test_human_stop_bench_is_the_inverse_of_start_bench(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    assert _web_reachable(inst.name), "setup: the bench must be serving"

    try:
        # Non-interactive (stdin closed): no prompt, exit 0.
        res = harness.run_cwcli("stop", inst.name, "--bench", "0")
        assert res.returncode == 0, res.stdout + res.stderr
        out = harness.strip_ansi(res.stdout)
        assert BENCH in out, out
        assert "untouched" in out, out
        harness.wait_until(
            lambda: not _web_reachable(inst.name), desc="the stopped bench stops serving"
        )
        assert harness.project_containers(inst.name), "containers must stay up"

        # `cwcli start --bench` brings the same bench back: the pair is symmetric.
        start = harness.run_cwcli("start", inst.name, "--bench", "0")
        assert start.returncode == 0, start.stdout + start.stderr
        _wait_web_ready(inst.name)
        assert _supervisord_count(inst.name) == 1, "exactly one supervisord after the restart"
    finally:
        _ensure_serving(inst.name)
        _wait_web_ready(inst.name)


def test_stopping_an_unknown_bench_refuses_rather_than_stopping_something_else(
    running_instance,
):
    inst = running_instance
    _ensure_serving(inst.name)

    res = harness.run_cwcli("axi", "stop", inst.name, "--bench", "no-such-bench")
    assert res.returncode != 0, res.stdout + res.stderr
    assert "error:" in res.stdout, res.stdout
    # Nothing was stopped on the way to refusing.
    assert _web_reachable(inst.name), "a refused stop must leave the bench serving"


# --------------------------------------------------------------------------- #
# 3. restart --bench relaunches the bench it was named
# --------------------------------------------------------------------------- #
def test_restart_bench_relaunches_the_named_bench(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)
    assert _web_reachable(inst.name), "setup: the bench must be serving"

    res = harness.run_cwcli("restart", inst.name, "--bench", "0")
    assert res.returncode == 0, res.stdout + res.stderr
    # The relaunched bench names ITSELF in the log path - the selector reached the
    # start rather than being dropped on the way to it.
    assert f"logs: {BENCH}/logs" in harness.strip_ansi(res.stdout), res.stdout

    _wait_web_ready(inst.name)
    assert _web_reachable(inst.name), "the named bench serves again after the restart"
    assert _supervisord_count(inst.name) == 1, "exactly one supervisord after the restart"


# --------------------------------------------------------------------------- #
# 4. the advertised address is the one that answers
# --------------------------------------------------------------------------- #
def test_open_reports_the_real_host_address_not_the_container_port(running_instance):
    inst = running_instance
    _ensure_serving(inst.name)

    container_port = _container_web_port(inst.name)
    host_port = _published_host_port(inst.name, container_port)

    res = harness.run_cwcli("open", inst.name, "--docker")
    combined = harness.strip_ansi(res.stdout + res.stderr)
    match = re.search(r"Web: (http://\S+)", combined)
    assert match, combined
    url = match.group(1)

    # The site is the host part (Frappe routes by Host) and the port is the
    # PUBLISHED one, which is the whole fix: 8000 is what the bench binds INSIDE.
    assert url == f"http://{inst.site}:{host_port}", url
    if host_port != container_port:
        assert str(container_port) not in url, url

    # And it is genuinely reachable: resolve the advertised URL back to a request.
    code, served = harness.exec_in_frappe(
        inst.name,
        f'curl -s -o /dev/null -w "%{{http_code}}" -H "Host: {inst.site}" '
        f"http://localhost:{container_port}",
    )
    assert code == 0 and served.strip() == "200", served


def test_the_init_banner_advertised_the_same_real_address(session_instance):
    """The banner from the session's own real ``cwcli init``, captured at creation."""
    inst = session_instance
    banner = harness.strip_ansi(inst.init_stdout)
    assert "Open:" in banner, banner

    match = re.search(r"Open:\s+(http://\S+)", banner)
    assert match, banner
    # The instance was created with an allocated --port base, so the advertised port
    # must be that base, never the container's 8000.
    assert match.group(1) == f"http://{inst.site}:{inst.port}", banner
