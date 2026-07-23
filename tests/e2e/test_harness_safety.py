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

# `standalone`: none of these needs the shared session instance (see the marker's
# entry in pyproject.toml).
pytestmark = [pytest.mark.e2e, pytest.mark.standalone]


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


def test_cwcli_home_rail_refuses_outside_isolated_root(isolated_home):
    """A CWCLI_HOME pointing at/under the operator's real home must be refused even
    when HOME itself is isolated - otherwise cwcli would write real state through a
    rail that only checked HOME. CWCLI_HOME must resolve INSIDE the isolated HOME."""
    saved = os.environ["CWCLI_HOME"]
    try:
        # CWCLI_HOME == the real home (HOME stays isolated) -> refused
        os.environ["CWCLI_HOME"] = harness._REAL_HOME
        with pytest.raises(RuntimeError):
            harness.enforce_isolation()
        # a subdirectory of the real home is refused too
        os.environ["CWCLI_HOME"] = os.path.join(harness._REAL_HOME, ".cwcli")
        with pytest.raises(RuntimeError):
            harness.enforce_isolation()
        # any absolute path outside the isolated HOME is refused
        os.environ["CWCLI_HOME"] = "/tmp/cwe2e-not-under-isolated-home"
        with pytest.raises(RuntimeError):
            harness.enforce_isolation()
    finally:
        os.environ["CWCLI_HOME"] = saved
    # sanity: with the isolated CWCLI_HOME (inside HOME) restored, the rail passes
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


def test_supervised_program_parsing_is_not_silently_permissive():
    """The stack-settle read must not degrade to "everything is fine".

    `_ensure_serving` waits for the supervised stack by parsing `supervisorctl
    status`. If that parse silently yielded nothing, every caller would sail past
    a half-started stack again - the exact race the per-group CI split exposed,
    back but invisible. Pure logic, so it is checked here rather than against a
    real bench.
    """
    from .test_start_status_e2e import _parse_supervised_programs

    parsed = _parse_supervised_programs(
        "web                    RUNNING   pid 191, uptime 0:01:23\n"
        "schedule               BACKOFF   Exited too quickly (process log may have details)\n"
        "watch                  STARTING\n"
        "worker_default         FATAL     Exited too quickly (process log may have details)\n"
        "veteran                RUNNING   pid 5, uptime 1 day, 2:03:04\n"
        "unix:///tmp/x.sock refused connection\n"
    )
    assert parsed == {
        "web": ("RUNNING", 83),
        "schedule": ("BACKOFF", 0),
        "watch": ("STARTING", 0),
        "worker_default": ("FATAL", 0),
        "veteran": ("RUNNING", 93784),
    }, parsed

    # A daemon that is not running yields NO programs (a honcho bench, or a
    # Procfile with no schedule): "nothing to wait for", never a false program.
    assert _parse_supervised_programs("error: could not connect\n") == {}


def test_supervised_program_read_fails_closed_for_a_live_daemon(monkeypatch):
    from .test_start_status_e2e import _supervised_programs

    def failed_live_read(project, script, workdir=None):
        return (
            1,
            "unix:///tmp/x.sock refused connection\n"
            "__CWCLI_MANAGER_PROVENANCE__=supervisord\n",
        )

    monkeypatch.setattr(harness, "exec_in_frappe", failed_live_read)
    with pytest.raises(AssertionError, match="could not read supervisor status"):
        _supervised_programs("cwe2e-test-live")


@pytest.mark.parametrize("provenance", ["honcho", "absent"])
def test_supervised_program_read_allows_an_absent_daemon(monkeypatch, provenance):
    from .test_start_status_e2e import _supervised_programs

    def failed_absent_read(project, script, workdir=None):
        return (
            1,
            "unix:///tmp/x.sock no such file\n"
            f"__CWCLI_MANAGER_PROVENANCE__={provenance}\n",
        )

    monkeypatch.setattr(harness, "exec_in_frappe", failed_absent_read)
    assert _supervised_programs("cwe2e-test-honcho") == {}


def test_supervised_program_read_fails_closed_for_a_crashed_expected_daemon(monkeypatch):
    from .test_start_status_e2e import _supervised_programs

    def failed_expected_read(project, script, workdir=None):
        return (
            1,
            "unix:///tmp/x.sock no such file\n"
            "__CWCLI_MANAGER_PROVENANCE__=expected\n",
        )

    monkeypatch.setattr(harness, "exec_in_frappe", failed_expected_read)
    with pytest.raises(AssertionError, match="could not read supervisor status"):
        _supervised_programs("cwe2e-test-crashed")


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


def test_reclaim_root_owned_makes_teardown_removable(_docker_gate, tmp_path):
    """Reproduce the exact leak - the Docker daemon (root) writing a root-owned
    path into a bind-mounted host dir - and prove ``reclaim_root_owned`` lets the
    ordinary non-root ``shutil.rmtree`` remove it. Scoped strictly to this test's
    own ``tmp_path``; never touches the session instance or shared /tmp."""
    import shutil

    home = tmp_path / "cwe2e-home"
    workspace = home / ".cwcli" / "projects" / "cwe2e-reclaim" / "conf"
    workspace.mkdir(parents=True)
    bind_root = workspace.parent  # what compose mounts as /workspace

    # Mimic `compose up`'s working_dir creation + a root process writing there.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{bind_root}:/workspace",
            "busybox",
            "sh",
            "-c",
            "mkdir -p /workspace/development && touch /workspace/development/f",
        ],
        check=True,
        capture_output=True,
    )
    leaked = bind_root / "development" / "f"
    assert leaked.stat().st_uid == 0, "precondition: the leak must be a genuine root-owned file"
    # A non-root rmtree cannot remove it as-is: the parent (root-owned) blocks unlink.
    shutil.rmtree(home, ignore_errors=True)
    assert leaked.exists(), "precondition: root-owned file survives a non-root rmtree"

    assert harness.reclaim_root_owned(home) is True, "reclaim did not run over the root-owned tree"
    shutil.rmtree(home, ignore_errors=False)
    assert not home.exists(), "reclaimed tree must be fully removable by the non-root user"
