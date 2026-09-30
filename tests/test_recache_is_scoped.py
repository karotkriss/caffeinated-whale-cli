"""Every post-mutation recache names the ONE bench the verb changed.

A bench-scoped recache (``cache.recache_project(..., bench_path=...)``) re-reads
only that bench, where the unscoped form re-inspects every bench of the project.
These drive each frontend with its core stubbed and pin the bench it hands the
recache. ``rm`` is deliberately absent: it removes the whole instance, and its
pre-backup recache must read every bench. The human ``apps`` verbs, ``axi apps
checkout``, human and ``axi init``, the Console checkout and ``update`` are
pinned where their own recache tests live.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli import main
from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import rm_bench as rm_bench_mod
from caffeinated_whale_cli.commands import rm_site as rm_site_mod
from caffeinated_whale_cli.core import apps as core_apps
from caffeinated_whale_cli.core import rm_bench as core_rm_bench
from caffeinated_whale_cli.core import rm_site as core_rm_site
from caffeinated_whale_cli.core.apps import AppResult, AppsReport
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.utils import cache

BENCH = "/workspace/frappe-bench-2"
runner = CliRunner()


class _Docker:
    def ping(self):
        return True


@pytest.fixture()
def recaches(monkeypatch):
    calls: list[tuple] = []

    def fake_recache(project, verbose=False, *, bench_path=None):
        calls.append((project, bench_path))
        return True

    monkeypatch.setattr(cache, "recache_project", fake_recache)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("docker.from_env", lambda: _Docker())
    return calls


def _drop_site(*_a, **_k):
    outcome = core_rm_site.DropSiteOutcome(
        project="proj",
        site="a.localhost",
        bench_path=BENCH,
        archived_host_path="/host/a.tar",
        archive_pruned_in_container=True,
        ok=True,
    )
    return Result(status=Status.OK, data=outcome)


def _remove_bench(*_a, **_k):
    outcome = core_rm_bench.BenchRemovalOutcome(
        project="proj",
        bench_path=BENCH,
        sites_dropped=["a.localhost"],
        sites_failed=[],
        archived_host_paths=["/host/a.tar"],
        dir_removed=True,
        ok=True,
    )
    return Result(status=Status.OK, data=outcome)


def _apps_report(*_a, **_k):
    report = AppsReport(
        project="proj",
        bench_path=BENCH,
        results=[AppResult(app="hrms", site="a.localhost", action="install", ok=True)],
        ok=True,
    )
    return Result(status=Status.OK, data=report)


def _wire_human(monkeypatch, module):
    monkeypatch.setattr(module, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(module, "resolve_bench_path", lambda *a, **k: BENCH)


def test_human_rm_site(monkeypatch, recaches):
    _wire_human(monkeypatch, rm_site_mod)
    monkeypatch.setattr(core_rm_site, "drop_site", _drop_site)

    result = runner.invoke(main.app, ["rm-site", "proj", "a.localhost", "--yes"])

    assert result.exit_code == 0, result.output
    assert recaches == [("proj", BENCH)]


def test_human_rm_bench(monkeypatch, recaches):
    _wire_human(monkeypatch, rm_bench_mod)
    monkeypatch.setattr(core_rm_bench, "remove_bench", _remove_bench)

    result = runner.invoke(main.app, ["rm-bench", "proj", "--bench", "2", "--yes"])

    assert result.exit_code == 0, result.output
    assert recaches == [("proj", BENCH)]


def test_axi_rm_site(monkeypatch, recaches):
    monkeypatch.setattr(core_rm_site, "drop_site", _drop_site)

    result = runner.invoke(axi_mod.app, ["rm-site", "proj", "a.localhost", "--yes"])

    assert result.exit_code == 0, result.output
    assert recaches == [("proj", BENCH)]


def test_axi_rm_bench(monkeypatch, recaches):
    monkeypatch.setattr(core_rm_bench, "remove_bench", _remove_bench)

    result = runner.invoke(axi_mod.app, ["rm-bench", "proj", "--bench", "2", "--yes"])

    assert result.exit_code == 0, result.output
    assert recaches == [("proj", BENCH)]


def test_axi_apps_install(monkeypatch, recaches):
    monkeypatch.setattr(core_apps, "install_apps", _apps_report)

    result = runner.invoke(axi_mod.app, ["apps", "install", "proj", "hrms", "--site", "a"])

    assert result.exit_code == 0, result.output
    assert recaches == [("proj", BENCH)]
