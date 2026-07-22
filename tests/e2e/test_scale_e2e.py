"""Scale E2E - the regression proof for ``cwcli scale``'s "never destroys the DB"
guarantee, mirroring the H4 method of the ``multibench-scale-verify`` report.

The headline claim ``cwcli scale`` makes is that widening an instance's published
port range past the six-bench host-reachable ceiling is database-safe: only the
frappe service is recreated (``docker compose up -d --no-deps frappe``), leaving
MariaDB, Redis, and the DB volume untouched. This proves it end to end on a real
instance:

- a cache-free DB marker (a throwaway ``cwe2e_marker`` table) is seeded through the
  bench, the range is expanded with ``cwcli scale --to 8``, then the marker is read
  back intact - the H4 proof that the DB survived;
- the MariaDB container id is UNCHANGED across the expansion (the volume was never
  touched) while the frappe container id CHANGES (it was recreated with the new
  port map);
- the newly-covered host ports (``8006``/``8007``) become published mappings, so a
  bench past the old six-port ceiling would be reachable from the host;
- a second ``scale --to 8`` is a clean idempotent no-op (``expanded: false``).

Port-map/DB-safety is version-agnostic, so this runs once on the v16 leg (the
``v16_only`` precedent from ``test_workspace_persistence_e2e``) and reuses the
shared session instance - no extra ``init`` cost.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from . import harness
from .test_restore_e2e import _read_marker, _seed_marker
from .test_start_status_e2e import _wait_web_ready

pytestmark = pytest.mark.e2e

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="scale's port-map/DB-safety is version-agnostic; runs only on the v16 leg",
)

MARKER = "SCALE_SURVIVED"


def _conf_dir(name: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / name / "conf"


def _compose_text(name: str) -> str:
    return (_conf_dir(name) / "docker-compose.yml").read_text()


def _host_port(name: str, container_port: int) -> str | None:
    """The host mapping for a container port, or None if unpublished."""
    cid = harness.frappe_container_id(name)
    assert cid, f"no frappe container for {name}"
    r = harness._docker("port", cid, str(container_port))
    out = r.stdout.strip()
    return out or None


@v16_only
def test_scale_expands_ports_and_the_database_survives(running_instance):
    """`cwcli scale --to 8` widens the published range, the DB survives (H4), the
    MariaDB container is untouched, the frappe container is recreated, and the
    newly-covered host ports are published."""
    inst = running_instance

    # 1. Seed a cache-free DB marker through the bench (real MariaDB write).
    _seed_marker(inst, MARKER)
    assert MARKER in _read_marker(inst)

    # 2. The instance starts at the init default: six published web ports.
    before = _compose_text(inst.name)
    assert f"{inst.port}-{inst.port + 5}:8000-8005" in before, before
    assert _host_port(inst.name, 8006) is None, "8006 should not be published before scaling"

    # 3. Record the container ids: MariaDB must survive, frappe must be recreated.
    mariadb_before = harness._docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.project={inst.name}",
        "--filter", "label=com.docker.compose.service=mariadb",
    ).stdout.split()
    assert mariadb_before, "no mariadb container found before scale"
    frappe_before = harness.frappe_container_id(inst.name)

    # 4. Expand to eight published ports (past the six-bench ceiling).
    result = harness.run_cwcli("scale", inst.name, "--to", "8", "--yes", timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr

    # 5. The compose file was widened, host base preserved.
    after = _compose_text(inst.name)
    assert f"{inst.port}-{inst.port + 7}:8000-8007" in after, after
    assert f"{inst.port + 1000}-{inst.port + 1007}:9000-9007" in after, after

    # 6. The newly-covered host ports are now published (host-reachable).
    assert _host_port(inst.name, 8006) is not None, "8006 not published after scale"
    assert _host_port(inst.name, 8007) is not None, "8007 not published after scale"

    # 7. MariaDB was never touched (same container id); frappe was recreated.
    mariadb_after = harness._docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.project={inst.name}",
        "--filter", "label=com.docker.compose.service=mariadb",
    ).stdout.split()
    assert mariadb_after == mariadb_before, "MariaDB container changed - the DB was at risk"
    assert harness.frappe_container_id(inst.name) != frappe_before, "frappe was not recreated"

    # 8. The DB survived: the marker reads back intact (the H4 proof).
    assert MARKER in _read_marker(inst), "DB marker did not survive the expansion"

    # 9. The bench serves again after the recreation (scale relaunched supervisord).
    _wait_web_ready(inst.name)
    st = harness.run_cwcli("status", inst.name)
    assert harness.strip_ansi(st.stdout).strip() == "running", st.stdout + st.stderr

    # 10. Idempotent: re-running when the range already covers is a safe no-op.
    again = harness.run_cwcli("scale", inst.name, "--to", "8", timeout=120)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already publishes" in harness.strip_ansi(again.stdout), again.stdout


@v16_only
def test_axi_scale_emits_toon_and_is_idempotent(running_instance):
    """The agent sibling: `axi scale` emits a TOON port map, and a no-op expansion
    reports `expanded: false` at exit 0 without needing `--yes`."""
    inst = running_instance
    # Widen to 8 via the agent surface itself (idempotent if already there), so the
    # no-op assertion below holds regardless of test order and is pure-axi.
    harness.run_cwcli("axi", "scale", inst.name, "--to", "8", "--yes", timeout=600)

    result = harness.run_cwcli("axi", "scale", inst.name, "--to", "8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    out = harness.strip_ansi(result.stdout)
    assert "expanded: false" in out, out
    assert "port_map" in out, out
