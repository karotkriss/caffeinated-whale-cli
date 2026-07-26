"""Real-Docker E2E for ``cwcli axi url``: the host URL PLUS a fresh HTTP probe.

Unit tests (``tests/test_core_url.py``) fake the container entirely, which proves
the RESOLVE logic but nothing about the two things that can only be observed
against a real instance: that the reported URL is the port Docker ACTUALLY
publishes (not a guessed offset), and that the reported ``http_code`` is a
genuinely fresh observation of a real Frappe process rather than a canned fake
response. This is the single-bench half, against the shared session instance -
cheap, since that instance already exists for every other E2E module. The
multi-bench half (proving bench N's probe targets bench N's OWN port, never a
neighbour's - the exact defect class ``report-status-per-bench`` fixed for
``status``) is added to ``test_multibench_serving_e2e.py``, reusing its
already-built two-genuinely-serving-benches fixture rather than paying for a
second one here.
"""

from __future__ import annotations

import subprocess

import pytest

from . import harness

pytestmark = [pytest.mark.e2e]


def _docker_port(container_id: str, container_port: int) -> int:
    """The REAL host port ``container_port/tcp`` is published on, read straight
    from Docker via the CLI - independently of ``resolve_host_web_url``'s own
    docker-py read, so a passing test is not just checking cwcli against itself."""
    r = subprocess.run(
        ["docker", "port", container_id, f"{container_port}/tcp"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    line = r.stdout.strip().splitlines()[0]
    return int(line.rsplit(":", 1)[1])


def _parse_flat_fields(toon: str) -> dict[str, str]:
    """The document's flat ``key: value`` head, stopping at the first block header.

    ``UrlProbe`` has no nested table, only an optional trailing ``warnings[N]:``
    block, so everything before it is a scalar field - unquoted here the same way
    the other E2E modules unquote a TOON scalar for comparison.
    """
    values: dict[str, str] = {}
    for line in toon.splitlines():
        if not line or line.startswith(" "):
            continue
        if line.endswith(":"):
            break  # a block header (e.g. "warnings[1]:") - fields precede it
        if ": " not in line:
            continue
        key, _, value = line.partition(": ")
        values[key.strip()] = value.strip().strip('"')
    return values


def test_resolves_the_real_published_port_and_probes_it_fresh(running_instance):
    """The headline claim: the reported URL is the ACTUAL Docker-published port,
    and ``http_code``/``reachable`` are a fresh, real observation - not a guess."""
    inst = running_instance
    cid = harness.frappe_container_id(inst.name)
    assert cid, "the shared session instance must be running for this test"
    ground_truth_port = _docker_port(cid, 8000)

    res = harness.run_cwcli("axi", "url", inst.name)
    assert res.returncode == 0, res.stdout + res.stderr
    out = harness.strip_ansi(res.stdout)
    fields = _parse_flat_fields(out)

    assert fields["project"] == inst.name, out
    assert fields["site"] == inst.site, out
    assert fields["url"] == f"http://{inst.site}:{ground_truth_port}", out
    assert fields["reachable"] == "true", out
    assert fields["http_code"] == "200", out

    # An INDEPENDENT probe against the exact URL cwcli reported, made directly
    # from the host - not through cwcli at all - so the assertion above is not
    # merely comparing cwcli's answer to itself.
    direct = subprocess.run(
        [
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-H",
            f"Host: {inst.site}",
            f"http://localhost:{ground_truth_port}/",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert direct.stdout.strip() == "200", direct.stdout + direct.stderr


def test_an_explicit_site_is_sent_as_the_host_header(running_instance):
    """``--site`` changes the ``Host`` header (and so the reported URL), and the
    same real Frappe instance answers accordingly - a host-less/wrong-site
    request to a genuinely healthy bench is not 200 (Frappe routes by Host)."""
    inst = running_instance

    res = harness.run_cwcli("axi", "url", inst.name, "--site", inst.site)
    assert res.returncode == 0, res.stdout + res.stderr
    fields = _parse_flat_fields(harness.strip_ansi(res.stdout))
    assert fields["site"] == inst.site
    assert fields["http_code"] == "200"

    bogus = harness.run_cwcli("axi", "url", inst.name, "--site", "no-such-site.localhost")
    assert bogus.returncode == 1, bogus.stdout + bogus.stderr


def test_missing_project_is_a_typed_not_found(running_instance):
    res = harness.run_cwcli("axi", "url", "cwe2e-does-not-exist-axiurl")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "not found" in harness.strip_ansi(res.stdout).lower()
