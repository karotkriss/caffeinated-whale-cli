"""§8.3 - prove the isolation rails and the leaked-resource backstop.

These guard the one unacceptable failure: touching the operator's real cwcli
state. The name-rail and port-allocator checks are pure logic; the HOME-rail and
backstop checks exercise the real mechanism (the latter against real Docker).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from . import harness

pytestmark = pytest.mark.e2e


def test_name_rail_refuses_unprefixed():
    with pytest.raises(RuntimeError):
        harness.assert_prefixed("myrealproject")
    assert harness.project_name("x").startswith(harness.CWE2E_PREFIX)


def test_home_rail_refuses_real_home(isolated_home):
    """Point HOME back at the operator's real home and confirm the rail refuses."""
    saved = os.environ["HOME"]
    os.environ["HOME"] = harness._REAL_HOME
    try:
        with pytest.raises(RuntimeError):
            harness.enforce_isolation()
    finally:
        os.environ["HOME"] = saved
    # sanity: with the isolated HOME restored, the rail passes again
    harness.enforce_isolation()


def test_home_rail_refuses_unset_home(isolated_home):
    """Delete HOME entirely and confirm the rail refuses rather than silently
    passing (realpath("") resolves to the CWD, not "", so the check must read
    the raw env value before calling realpath)."""
    saved = os.environ.pop("HOME")
    try:
        with pytest.raises(RuntimeError):
            harness.enforce_isolation()
    finally:
        os.environ["HOME"] = saved
    # sanity: with HOME restored, the rail passes again
    harness.enforce_isolation()


def test_port_allocator_spacing():
    alloc = harness.PortAllocator()
    p0, p1, p2 = alloc.next(), alloc.next(), alloc.next()
    # bases at least 1006 apart
    assert p1 - p0 >= 1006
    assert p2 - p1 >= 1006
    # each instance's web {p..p+5} and socketio {p+1000..p+1005} are disjoint,
    # and the next instance's web range clears this instance's socketio range.
    assert p0 + 5 < p0 + 1000
    assert p1 > p0 + 1005


def _create_fake_leaked_project(name: str) -> None:
    label = f"com.docker.compose.project={name}"
    subprocess.run(
        ["docker", "volume", "create", "--label", label, f"{name}-vol"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["docker", "run", "-d", "--label", label, "--name", f"{name}-c", "busybox", "sleep", "600"],
        check=True,
        capture_output=True,
    )


def test_backstop_sweeps_leaked_container(_docker_gate):
    """A deliberately leaked cwe2e- compose project (container + volume) is
    discovered and swept by the backstop's mechanism. Scoped with `only=` so it
    proves the sweep without touching the live session instance."""
    leaked = harness.project_name("leaktest")
    _create_fake_leaked_project(leaked)
    try:
        assert leaked in harness.cwe2e_projects(), "backstop discovery missed the leak"
        swept = harness.sweep_cwe2e(only=leaked)
        assert leaked in swept
        assert leaked not in harness.cwe2e_projects(), "leak survived the sweep"
    finally:
        harness.sweep_cwe2e(only=leaked)
