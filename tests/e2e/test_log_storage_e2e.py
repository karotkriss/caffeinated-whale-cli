"""Log storage against a real instance (``cwcli axi storage``).

What only real Docker can prove, on the shared session instance:

* a fresh ``cwcli init`` writes no logging options of its own into the compose file;
* the Docker log sizes are measured (from the host or through the read-only root
  helper - whichever this daemon allows) and ``truncate-docker-logs`` empties them;
* ``prune-bench-logs`` removes rotated files under ``<bench>/logs`` and the site's
  ``sites/<site>/logs`` and keeps the current files beside them, on a RUNNING bench,
  which keeps serving;
* ``set-docker-log-rotation`` rewrites the compose file and the report reads it back.

Shared group: everything this test adds (a rotated file, a sentinel, the compose edit)
is restored in a ``finally``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from . import harness

# Explicit, like every E2E file: the root conftest auto-marks anything unmarked as
# `unit`, and a unit run must never reach the real session instance.
pytestmark = pytest.mark.e2e

_SIZE_RE = re.compile(r"^\s*docker_total: (\d+|null)", re.MULTILINE)


def _storage_logs(project: str) -> str:
    r = harness.run_cwcli("axi", "storage", "logs", project, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _docker_total(doc: str) -> int | None:
    m = _SIZE_RE.search(doc)
    assert m, doc
    return None if m.group(1) == "null" else int(m.group(1))


def _compose_path(project: str) -> Path:
    return Path(os.environ["CWCLI_HOME"]) / "projects" / project / "conf" / "docker-compose.yml"


def test_init_writes_no_rotation_and_the_report_measures_the_logs(running_instance):
    compose = _compose_path(running_instance.name).read_text()
    assert "logging:" not in compose and "max-size" not in compose
    doc = _storage_logs(running_instance.name)
    assert running_instance.bench in doc  # the bench's logs dir was read
    total = _docker_total(doc)
    assert total is not None, "the Docker log sizes were not measured:\n" + doc
    assert "method: host" in doc or "method: helper" in doc, doc


def test_truncate_empties_the_docker_logs_of_a_running_instance(running_instance):
    name = running_instance.name
    # MariaDB logged its startup, so at least one json-file log is non-empty.
    before = _docker_total(_storage_logs(name))
    assert before is not None
    refused = harness.run_cwcli("axi", "storage", "truncate-docker-logs", name, timeout=120)
    assert refused.returncode == 2 and "--yes" in refused.stdout, refused.stdout + refused.stderr
    r = harness.run_cwcli("axi", "storage", "truncate-docker-logs", name, "--yes", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{name}-frappe-1" in r.stdout or "frappe" in r.stdout
    after = _docker_total(_storage_logs(name))
    assert after is not None and after <= before, (before, after)
    if before > 0:
        assert after < before, (before, after)
    # The containers kept running and the site still answers.
    assert harness.frappe_container_id(name) is not None
    harness.wait_for_site_ready(name, running_instance.site)


def test_prune_removes_rotated_bench_logs_and_keeps_the_current_file(running_instance):
    name, bench = running_instance.name, running_instance.bench
    logs = f"{bench}/logs"
    site_logs = f"{bench}/sites/{running_instance.site}/logs"
    current, rotated = f"{logs}/cwe2e-storage.log", f"{logs}/cwe2e-storage.log.1"
    site_current, site_rotated = f"{site_logs}/cwe2e-site.log", f"{site_logs}/cwe2e-site.log.1"
    code, out = harness.exec_in_frappe(
        name,
        f"mkdir -p {site_logs} && printf 'current\\n' > {current} && "
        f"head -c 4096 /dev/zero > {rotated} && printf 'current\\n' > {site_current} && "
        f"head -c 2048 /dev/zero > {site_rotated}",
    )
    assert code == 0, out
    try:
        doc = _storage_logs(name)
        assert "cwe2e-storage.log.1" in doc and "cwe2e-site.log.1" in doc, doc
        refused = harness.run_cwcli("axi", "storage", "prune-bench-logs", name, timeout=120)
        assert refused.returncode == 2 and "--yes" in refused.stdout, (
            refused.stdout + refused.stderr
        )
        code, out = harness.exec_in_frappe(name, f"test -f {rotated} && test -f {current}")
        assert code == 0, "the refusal must not remove anything"
        r = harness.run_cwcli("axi", "storage", "prune-bench-logs", name, "--yes", timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "cwe2e-storage.log.1" in r.stdout and "cwe2e-site.log.1" in r.stdout
        for gone in (rotated, site_rotated):
            code, _ = harness.exec_in_frappe(name, f"test -f {gone}")
            assert code != 0, f"the rotated file {gone} survived the prune"
        for kept in (current, site_current):
            code, out = harness.exec_in_frappe(name, f"cat {kept}")
            assert code == 0 and "current" in out, f"{kept} must be kept untouched"
        harness.wait_for_site_ready(name, running_instance.site)
    finally:
        harness.exec_in_frappe(name, f"rm -f {current} {rotated} {site_current} {site_rotated}")


def test_set_rotation_rewrites_the_compose_file_and_the_report_reads_it_back(running_instance):
    name = running_instance.name
    path = _compose_path(name)
    original = path.read_text()
    try:
        r = harness.run_cwcli(
            "axi",
            "storage",
            "set-docker-log-rotation",
            name,
            "--max-size",
            "5m",
            "--max-file",
            "2",
            timeout=120,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "changed: true" in r.stdout and "rotation_on_recreate" in r.stdout
        text = path.read_text()
        assert text.count('max-size: "5m"') == text.count("driver: json-file") > 0
        doc = _storage_logs(name)
        assert "max_size: 5m" in doc and 'max_file: "2"' in doc, doc
        # The running containers keep the log settings they were created with.
        assert "json-file,5m," not in doc, doc
        bad = harness.run_cwcli(
            "axi", "storage", "set-docker-log-rotation", name, "--max-size", "huge", timeout=120
        )
        assert bad.returncode == 2, bad.stdout + bad.stderr
    finally:
        path.write_text(original)
    assert path.read_text() == original


@pytest.mark.standalone
def test_journal_report_says_whether_it_applies_here():
    """No instance needed: the host journal half of the report."""
    r = harness.run_cwcli("axi", "storage", "logs", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "journal:" in r.stdout
    assert "applies: true" in r.stdout or "applies: false" in r.stdout
    if "applies: true" in r.stdout:
        assert "journalctl --vacuum" in r.stdout
