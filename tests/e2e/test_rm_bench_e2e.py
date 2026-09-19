"""``cwcli rm-bench`` / ``cwcli axi rm-bench`` E2E - the full destructive arc, real Docker.

Removing ONE bench from a multi-bench instance is a destructive-delete path, so
the captain standard requires it proven end to end on a real instance, never on
mocks (a prior dangerous-delete verb passed on mocks that hid a bug). This walks
the whole lifecycle against genuine throwaway benches and asserts every claim by
reading real state back out of Docker, the host filesystem, the shared MariaDB,
and the archived backup - never by trusting cwcli's own success text alone:

  1. the backup exists and genuinely contains the seeded data, taken BEFORE the
     deletion (the archived tar holds a non-empty, real Frappe database dump with
     the seeded record in it);
  2. the removed bench's directory is genuinely gone (Docker exec AND the host
     bind-mount path) and its site database is genuinely dropped from the shared
     MariaDB (a bench has no dedicated named volume - its data is its directory
     plus its databases, so those are what "gone" means);
  3. every OTHER bench survives intact and still serves;
  4. ``rm-bench`` REFUSES a bench that is still running.

Provisions its OWN two-bench instance (the shared session fixture is single-bench
and every other test depends on it), version-agnostic so it runs once on the v16
leg, and does the whole arc in ONE function so it pays for provisioning once -
the OOM-tight box wants one throwaway instance at a time.
"""

from __future__ import annotations

import gzip
import json
import os
import shlex
import tarfile
from pathlib import Path

import pytest

from . import harness
from .conftest import SESSION_ADMIN_PW

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

v16_only = pytest.mark.skipif(
    harness.FRAPPE_MAJOR != 16,
    reason="version-agnostic (the drop+delete mechanics do not vary by Frappe major); runs once",
)

MARKER = "CWE2E-RMBENCH-MARKER"

SURVIVOR_BENCH_PATH = harness.DEFAULT_BENCH_PATH  # /workspace/frappe-bench
SURVIVOR_SITE = harness.DEFAULT_SITE
# Deliberately sorts before the survivor: proves removal targets the bench NAMED,
# not the first by any incidental ordering.
TARGET_BENCH_NAME = "aaa-bench"
TARGET_BENCH_PATH = f"/workspace/{TARGET_BENCH_NAME}"
TARGET_SITE = "target.localhost"


def _init(name: str, port: int, *extra: str):
    return harness.run_cwcli(
        "init",
        name,
        "--port",
        str(port),
        "--frappe-branch",
        harness.FRAPPE_BRANCH,
        "--admin-password",
        SESSION_ADMIN_PW,
        "--auto-start",
        *extra,
        timeout=harness.INIT_TIMEOUT,
    )


def _seed_marker(project: str, site: str, bench: str, marker: str) -> None:
    """Insert a real ToDo via `bench execute frappe.client.insert` (the axi-rm pattern)."""
    kwargs = f'{{"doc": {{"doctype": "ToDo", "description": "{marker}", "status": "Open"}}}}'
    script = (
        f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} execute "
        f"frappe.client.insert --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"seeding the marker record failed: {out}"


def _count_marker(project: str, site: str, bench: str, marker: str) -> int:
    kwargs = f'{{"doctype": "ToDo", "filters": {{"description": "{marker}"}}}}'
    script = (
        f"cd {shlex.quote(bench)} && bench --site {shlex.quote(site)} execute "
        f"frappe.client.get_count --kwargs '{kwargs}'"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"reading the marker count back failed: {out}"
    stripped = out.strip()
    if not stripped:
        # bench's `execute` only prints a truthy return, so a genuine 0 prints nothing.
        return 0
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.lstrip("-").isdigit():
            return int(line)
    raise AssertionError(f"could not parse an integer count from: {out!r}")


def _db_name(project: str, bench: str, site: str) -> str:
    code, out = harness.exec_in_frappe(
        project, f"cat {shlex.quote(bench)}/sites/{shlex.quote(site)}/site_config.json"
    )
    assert code == 0, out
    return json.loads(out)["db_name"]


def _database_exists(project: str, db_name: str) -> bool:
    """True if `db_name` still exists in the instance's shared MariaDB (read via the
    frappe container's own mysql client against the `mariadb` service host)."""
    script = (
        "mysql -h mariadb -uroot -p123 -N -e " f"\"SHOW DATABASES LIKE '{db_name}'\" 2>/dev/null"
    )
    code, out = harness.exec_in_frappe(project, script)
    assert code == 0, f"could not query MariaDB: {out}"
    return db_name in out


def _web_port(project: str, bench: str) -> int:
    code, out = harness.exec_in_frappe(project, f"cat {bench}/sites/common_site_config.json")
    assert code == 0, out
    return int(json.loads(out)["webserver_port"])


def _http_code(project: str, port: int, site: str) -> str:
    _code, out = harness.exec_in_frappe(
        project,
        f'curl -s --max-time 10 -o /dev/null -w "%{{http_code}}" '
        f'-H "Host: {site}" http://localhost:{port}',
    )
    lines = [line for line in out.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else "000"


def _bench_indices(project: str) -> dict[str, int]:
    res = harness.run_cwcli("axi", "benches", project)
    assert res.returncode == 0, res.stdout + res.stderr
    indices: dict[str, int] = {}
    seen_header = False
    for line in harness.strip_ansi(res.stdout).splitlines():
        if line.startswith("benches["):
            seen_header = True
            continue
        if not seen_header or not line.startswith(" "):
            continue
        parts = [p.strip() for p in line.strip().split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            indices[parts[1]] = int(parts[0])
    return indices


def _host_bench_dir(project: str, bench_name: str) -> Path | None:
    """The host path of a bench directory, whichever bind-mount layout is in use."""
    root = Path(os.environ["CWCLI_HOME"]) / "projects" / project
    for candidate in (root / "data" / bench_name, root / bench_name):
        if candidate.exists():
            return candidate
    return None


def _dropped_site_archive(project: str) -> Path:
    archive_dir = Path(os.environ["CWCLI_HOME"]) / "archive" / f"{project}_dropped_sites"
    tars = sorted(archive_dir.glob("*.tar"))
    assert tars, f"no dropped-site archive found under {archive_dir}"
    return tars[-1]


def _sql_dump_bytes_from_tar(tar_path: Path) -> bytes:
    """Extract the (gzipped) Frappe database dump from a drop-site archive tar and
    return its DECOMPRESSED SQL bytes - proving the backup holds a real dump."""
    with tarfile.open(tar_path, "r") as tar:
        members = [
            m
            for m in tar.getmembers()
            if m.isfile() and m.name.endswith("database.sql.gz") and "backups" in m.name
        ]
        assert members, f"no database dump inside {tar_path}: {[m.name for m in tar.getmembers()]}"
        extracted = tar.extractfile(members[-1])
        assert extracted is not None
        return gzip.decompress(extracted.read())


@v16_only
def test_rm_bench_removes_one_bench_and_leaves_the_rest_serving(port_allocator):
    harness.enforce_isolation()
    name = harness.project_name("rmbench")
    port = port_allocator.next()

    try:
        # --- build a real two-bench instance ---
        first = _init(name, port)
        assert first.returncode == 0, first.stdout + first.stderr
        harness.wait_for_site_ready(name, SURVIVOR_SITE, bench=SURVIVOR_BENCH_PATH)

        second = _init(name, port, "--bench", TARGET_BENCH_NAME, "--site", TARGET_SITE)
        assert second.returncode == 0, second.stdout + second.stderr
        harness.wait_for_site_ready(name, TARGET_SITE, bench=TARGET_BENCH_PATH)

        insp = harness.run_cwcli("inspect", name, "--update")
        assert insp.returncode == 0, insp.stdout + insp.stderr
        indices = _bench_indices(name)
        assert TARGET_BENCH_PATH in indices and SURVIVOR_BENCH_PATH in indices, indices
        target_index = indices[TARGET_BENCH_PATH]

        target_port = _web_port(name, TARGET_BENCH_PATH)
        survivor_port = _web_port(name, SURVIVOR_BENCH_PATH)
        assert target_port != survivor_port, (target_port, survivor_port)
        target_db = _db_name(name, TARGET_BENCH_PATH, TARGET_SITE)
        survivor_db = _db_name(name, SURVIVOR_BENCH_PATH, SURVIVOR_SITE)

        # --- seed identifiable data into the TARGET bench's site; prove it landed ---
        _seed_marker(name, TARGET_SITE, TARGET_BENCH_PATH, MARKER)
        assert _count_marker(name, TARGET_SITE, TARGET_BENCH_PATH, MARKER) == 1, "seed did not land"

        # both benches genuinely serving (the baseline every later claim stands on)
        harness.wait_until(
            lambda: _http_code(name, target_port, TARGET_SITE) == "200",
            timeout=300,
            desc="target bench serving",
        )
        harness.wait_until(
            lambda: _http_code(name, survivor_port, SURVIVOR_SITE) == "200",
            timeout=300,
            desc="survivor bench serving",
        )

        # --- 4. REFUSE while the target bench is running ---
        refused = harness.run_cwcli("axi", "rm-bench", name, "--bench", str(target_index), "--yes")
        assert refused.returncode != 0, refused.stdout + refused.stderr
        combined = harness.collapse_ws(harness.strip_ansi(refused.stdout + refused.stderr)).lower()
        assert "running" in combined, combined
        assert "cwcli stop" in combined, combined
        # nothing was touched: the target still serves and its DB is intact
        assert _http_code(name, target_port, TARGET_SITE) == "200"
        assert _database_exists(name, target_db)

        # --- stop the target bench, then remove it for real ---
        stopped = harness.run_cwcli("stop", name, "--bench", str(target_index))
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr

        removed = harness.run_cwcli("axi", "rm-bench", name, "--bench", str(target_index), "--yes")
        assert removed.returncode == 0, removed.stdout + removed.stderr
        out = removed.stdout
        assert f"bench_path: {TARGET_BENCH_PATH}" in out, out
        assert "ok: true" in out, out
        assert "dir_removed: true" in out, out
        assert f"sites_dropped[1]: {TARGET_SITE}" in out, out
        assert "archived_host_paths[1]:" in out, out

        # --- 1. the backup exists BEFORE deletion and genuinely holds the seeded data ---
        archive_tar = _dropped_site_archive(name)
        assert archive_tar.stat().st_size > 0
        sql = _sql_dump_bytes_from_tar(archive_tar)
        assert len(sql) > 0, "the archived database dump was empty"
        assert b"CREATE TABLE" in sql, "the archived dump is not a real SQL backup"
        assert MARKER.encode() in sql, "the seeded record is not in the archived backup"

        # --- 2. the removed bench's directory AND database are genuinely gone ---
        code, _ = harness.exec_in_frappe(name, f"test -d {shlex.quote(TARGET_BENCH_PATH)}")
        assert code != 0, "the target bench directory still exists inside the container"
        assert _host_bench_dir(name, TARGET_BENCH_NAME) is None, "target bench dir survives on host"
        assert not _database_exists(name, target_db), "the target site's database was not dropped"

        # --- 3. every OTHER bench survives intact and still serves ---
        code, _ = harness.exec_in_frappe(name, f"test -d {shlex.quote(SURVIVOR_BENCH_PATH)}")
        assert code == 0, "the survivor bench directory was removed"
        assert _host_bench_dir(name, "frappe-bench") is not None, "survivor bench dir gone on host"
        assert _database_exists(name, survivor_db), "the survivor site's database was dropped"
        assert _http_code(name, survivor_port, SURVIVOR_SITE) == "200", "survivor stopped serving"
    finally:
        harness.cwcli_rm(name)
