"""§5.1-§5.4 workspace bind-mount persistence E2E.

Proves the workspace map (frappe mount rewritten to ``../data:{bench_parent}:cached``)
end to end on real instances:

- a fresh default-parent instance keeps its bench, a host-written marker, and the
  s4 supervisor logs across a full ``docker compose down`` + ``up`` recreation,
  and its bench data lives directly on the host at ``{project}/data/`` (a bind
  mount, not a named volume);
- a custom ``--bench-parent`` builds a real running bench under that parent,
  ``status``/``axi logs`` see it, it survives recreation, and ``rm`` removes the
  host data dir;
- a pre-existing frozen-compose instance (the old whole-project ``..:/workspace``
  mount) is neither rewritten nor allowed a mismatched re-init.

The mount mechanism does not vary by Frappe major, so this runs once on the v16
leg - the ``v16_only`` precedent from ``test_init_e2e``. The default-parent case
reuses the shared session instance (no extra init); only the custom-parent case
pays for its own bench build.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = pytest.mark.e2e

# The workspace-mount behavior is version-agnostic; run it once, on the v16 leg.
v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="workspace-mount behavior is version-agnostic; runs only on the v16 leg",
)

MARKER = "CWE2E_PERSIST_MARKER"


def _project_dir(name: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / name


def _conf_dir(name: str) -> Path:
    return _project_dir(name) / "conf"


def _data_dir(name: str) -> Path:
    return _project_dir(name) / "data"


def _compose(name: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run ``docker compose`` against an instance's own compose file/project."""
    return subprocess.run(
        ["docker", "compose", "-p", name, "-f", "docker-compose.yml", *args],
        cwd=str(_conf_dir(name)),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )


def _recreate(name: str) -> None:
    """Full stack recreation: ``compose down`` then ``up -d``, waiting for frappe.

    The named ``mariadb-data`` volume and the ``{project}/data/`` bind mount both
    outlive ``down``; this is the real container-recreation the fix must survive.
    """
    down = _compose(name, "down")
    assert down.returncode == 0, down.stdout + down.stderr
    assert harness.frappe_container_id(name) is None, "containers not gone after `compose down`"
    up = _compose(name, "up", "-d")
    assert up.returncode == 0, up.stdout + up.stderr
    harness.wait_until(
        lambda: harness.frappe_container_id(name) is not None,
        timeout=180,
        interval=3,
        desc=f"{name} frappe container back up after recreation",
    )


@v16_only
def test_default_parent_persists_bench_across_recreation(running_instance):
    """A default (/workspace) instance keeps its bench, a host-written marker, and
    the supervisor logs across a `docker compose down`+`up`, and its data lives on
    the host `{project}/data/` bind mount."""
    inst = running_instance

    # This instance was created by the build under test: the frappe workspace is
    # the per-project host data/ bind mount, not the upstream whole-project mount.
    compose = (_conf_dir(inst.name) / "docker-compose.yml").read_text()
    assert "- ../data:/workspace:cached" in compose, compose
    assert "- ..:/workspace:cached" not in compose, compose
    assert "working_dir: /workspace" in compose, compose

    # Bench data lives on the host at {project}/data/ with direct access.
    host_bench = _data_dir(inst.name) / harness.DEFAULT_BENCH_NAME
    assert host_bench.is_dir(), f"bench not on host data dir: {host_bench}"

    # A marker written inside the container is immediately visible on the host -
    # proof it is a bind mount, not a named volume.
    code, out = harness.exec_in_frappe(inst.name, f"touch {shlex.quote(inst.bench)}/{MARKER}")
    assert code == 0, out
    assert (
        host_bench / MARKER
    ).exists(), "marker not visible on host - workspace is not bind-mounted"

    # The instance is supervised, so its per-process supervisor logs exist on the mount.
    code, out = harness.exec_in_frappe(
        inst.name, f"ls {shlex.quote(inst.bench)}/logs/*.supervisor.log"
    )
    assert code == 0, f"no supervisor logs before recreation: {out}"

    _recreate(inst.name)

    # The bench, the marker, and the supervisor logs all survived recreation.
    code, out = harness.exec_in_frappe(inst.name, f"test -f {shlex.quote(inst.bench)}/{MARKER}")
    assert code == 0, f"marker did not survive recreation: {out}"
    code, out = harness.exec_in_frappe(
        inst.name, f"ls {shlex.quote(inst.bench)}/logs/*.supervisor.log"
    )
    assert code == 0, f"supervisor logs did not survive recreation: {out}"

    # A container restart needs `cwcli start` to re-supervise; leave the shared
    # instance healthy for any later test regardless of collection order.
    started = harness.run_cwcli("start", inst.name, "--yes")
    assert started.returncode == 0, started.stdout + started.stderr
    harness.wait_for_site_ready(inst.name, inst.site)


@v16_only
def test_custom_bench_parent_persists_and_rm_removes_data(port_allocator):
    """A custom `--bench-parent` builds a real running bench under that parent
    (from the same host data/ dir), `status`/`axi logs` see it, it survives a
    recreation, and `rm` removes the host data dir."""
    name = harness.project_name("wsparent")
    port = port_allocator.next()
    parent = "/opt/benches"
    bench = f"{parent}/{harness.DEFAULT_BENCH_NAME}"

    result = harness.run_cwcli(
        "init",
        name,
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--bench-parent",
        parent,
        "--admin-password",
        SESSION_ADMIN_PW,
        "--auto-start",
        timeout=harness.INIT_TIMEOUT,
    )
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        harness.wait_for_site_ready(name, harness.DEFAULT_SITE, bench=bench)

        # The compose mounts the host data/ dir at the custom parent; working_dir follows.
        compose = (_conf_dir(name) / "docker-compose.yml").read_text()
        assert f"- ../data:{parent}:cached" in compose, compose
        assert f"working_dir: {parent}" in compose, compose

        # The bench really built under the custom parent, on the host data dir.
        host_bench = _data_dir(name) / harness.DEFAULT_BENCH_NAME
        assert host_bench.is_dir(), f"custom-parent bench not on host: {host_bench}"
        code, out = harness.exec_in_frappe(name, f"test -d {shlex.quote(bench)}")
        assert code == 0, f"bench missing in container at {bench}: {out}"

        # start/status/logs resolve the custom-parent bench with no `--bench`
        # selector needed (a single cached bench), proven by the honest
        # "running" token - `status`'s stdout is a bare lifecycle token by
        # contract and never echoes a path, so a wrong bench resolution would
        # surface here as "online" (marker unreadable at the wrong path), not
        # as a missing substring.
        st = harness.run_cwcli("status", name)
        assert st.returncode == 0, st.stdout + st.stderr
        assert harness.strip_ansi(st.stdout).strip() == "running", st.stdout + st.stderr
        lg = harness.run_cwcli("axi", "logs", name, "-n", "5")
        assert lg.returncode == 0, lg.stdout + lg.stderr

        # Persists across a full recreation, exactly as the default parent does.
        code, out = harness.exec_in_frappe(name, f"touch {shlex.quote(bench)}/{MARKER}")
        assert code == 0, out
        _recreate(name)
        code, out = harness.exec_in_frappe(name, f"test -f {shlex.quote(bench)}/{MARKER}")
        assert code == 0, f"custom-parent marker did not survive recreation: {out}"

        # rm removes the host data dir (it lives under the project directory).
        assert _data_dir(name).exists()
        rmres = harness.run_cwcli("rm", name, "--yes", "--volumes", "--no-backup", timeout=600)
        assert rmres.returncode == 0, rmres.stdout + rmres.stderr
        assert not _project_dir(name).exists(), "rm left the project/data dir behind"
    finally:
        harness.cwcli_rm(name)  # idempotent safety net if the test aborted early


@v16_only
def test_preexisting_frozen_compose_is_not_rewritten_and_mismatch_refused(port_allocator):
    """Backward-compat: an instance created BEFORE this change (the old
    whole-project `..:/workspace:cached` mount) is neither rewritten nor allowed a
    mismatched re-init - the frozen-compose boundary, proven against the real binary."""
    name = harness.project_name("wsfrozen")
    conf = _conf_dir(name)
    conf.mkdir(parents=True, exist_ok=True)
    old_compose = (
        "services:\n"
        "  frappe:\n"
        "    image: docker.io/frappe/bench:latest\n"
        "    volumes:\n"
        "      - ..:/workspace:cached\n"
        "    working_dir: /workspace/development\n"
        "    ports:\n"
        "      - 8000-8005:8000-8005\n"
        "      - 9000-9005:9000-9005\n"
        "volumes:\n"
        "  mariadb-data:\n"
    )
    compose_path = conf / "docker-compose.yml"
    compose_path.write_text(old_compose)
    try:
        # A mismatched --bench-parent is refused (USAGE) naming the mounted parent,
        # before any container work. A free port keeps a port conflict from masking it.
        res = harness.run_cwcli(
            "init",
            name,
            "--port",
            str(port_allocator.next()),
            "--bench-parent",
            "/opt/elsewhere",
            "--admin-password",
            SESSION_ADMIN_PW,
        )
        assert res.returncode != 0, res.stdout + res.stderr
        assert "/workspace" in harness.strip_ansi(res.stdout + res.stderr), res.stdout + res.stderr
        assert harness.frappe_container_id(name) is None, "mismatch should not start containers"

        # The frozen compose was left byte-for-byte unchanged.
        assert compose_path.read_text() == old_compose, "frozen compose was rewritten"
    finally:
        harness.cwcli_rm(name)
