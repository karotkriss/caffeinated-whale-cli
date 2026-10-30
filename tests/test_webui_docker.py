"""The page's "Docker is not reachable" state, decided under node.

A Docker Desktop restart left a running daemon reading nothing from Docker while
the page kept showing the last rows it had, plus errors on the site page. The
daemon now reports its Docker reachability; these rules decide which pages give
way to one "not reachable" state and when to re-read what the outage left stale.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "webui" / "src" / "data" / "docker.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

DOWN = {"reachable": False, "error": "Connection refused", "since": 1.0}
UP = {"reachable": True, "error": None, "since": 2.0}
NOT_ASKED = {"reachable": None, "error": None, "since": None}


def _call(name, *args):
    script = (
        f"import * as m from {json.dumps(MODULE.as_uri())};\n"
        f"console.log(JSON.stringify(m.{name}(...{json.dumps(list(args))})));\n"
    )
    out = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize("route", ["instances", "instance", "bench", "site", "sites"])
def test_every_page_reading_local_docker_gives_way_while_it_is_down(route):
    assert _call("dockerGates", DOWN, route) is True
    assert _call("dockerGates", UP, route) is False
    assert _call("dockerGates", NOT_ASKED, route) is False, "not asked yet is never down"
    assert _call("dockerGates", None, route) is False, "an older daemon sends no state"


@pytest.mark.parametrize("route", ["host-instance", "host-site", "jobs", "settings", "apps"])
def test_pages_that_do_not_read_local_docker_stay(route):
    assert _call("dockerGates", DOWN, route) is False


def test_the_lists_stay_when_they_also_hold_remote_hosts():
    assert _call("dockerGates", DOWN, "instances", {"hasHosts": True}) is False
    assert _call("dockerGates", DOWN, "sites", {"hasHosts": True}) is False
    assert _call("dockerGates", DOWN, "site", {"hasHosts": True}) is True


def test_only_a_return_from_down_rereads_the_details():
    assert _call("dockerCameBack", DOWN, UP) is True
    assert _call("dockerCameBack", NOT_ASKED, UP) is False
    assert _call("dockerCameBack", None, UP) is False
    assert _call("dockerCameBack", UP, UP) is False
    assert _call("dockerCameBack", DOWN, DOWN) is False
