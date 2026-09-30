"""The one-exec container probes, run for real under ``/bin/sh``.

``list_sites``, bench discovery and the T2 partial refresh each answer from ONE
``sh -c <script> sh <paths...>`` exec. The unit fakes emulate those scripts on
top of per-path answers (``tests/batched_probes.py``), so these tests pin the
scripts' actual shell semantics: a host container stand-in runs each exec with
the host's ``/bin/sh`` (dash on Debian/Ubuntu, the same shell the frappe image
uses) against a real bench tree under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.utils import bench_sites

as_root = os.geteuid() == 0


class HostShell:
    """Runs list-form execs on the host; stderr merges into output as with docker."""

    def __init__(self):
        self.calls: list = []

    def exec_run(self, cmd, workdir=None):
        assert isinstance(cmd, list), f"batched probes are list-form, got {cmd!r}"
        self.calls.append(cmd)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        return proc.returncode, proc.stdout


def make_bench(path, *, sites=(), apps=("frappe",), stray=("apps.txt", "currentsite.txt")):
    (path / "apps").mkdir(parents=True)
    for app in apps:
        (path / "apps" / app).mkdir()
    sites_dir = path / "sites"
    sites_dir.mkdir()
    (sites_dir / "common_site_config.json").write_text(json.dumps({}))
    for name in stray:
        (sites_dir / name).write_text("x")
    for site in sites:
        (sites_dir / site).mkdir()
        (sites_dir / site / "site_config.json").write_text(json.dumps({"db_name": "d"}))
    return str(path)


class TestListSites:
    def test_one_exec_classifies_every_entry(self, tmp_path):
        bench = make_bench(tmp_path / "b", sites=["a.localhost", 'it\'s "q" site'])
        (tmp_path / "b" / "sites" / "assets").mkdir()  # a readable dir with no config
        container = HostShell()

        sites = bench_sites.list_sites(container, bench)

        assert sorted(sites) == ["a.localhost", 'it\'s "q" site']
        assert len(container.calls) == 1

    def test_empty_sites_dir_is_no_sites_not_unknown(self, tmp_path):
        bench = make_bench(tmp_path / "b", stray=())
        assert bench_sites.list_sites(HostShell(), bench) == []

    def test_missing_sites_dir_is_none(self, tmp_path):
        assert bench_sites.list_sites(HostShell(), str(tmp_path / "nope")) is None

    @pytest.mark.skipif(as_root, reason="root reads a mode-000 dir")
    def test_unreadable_dir_fails_safe_to_a_site(self, tmp_path):
        bench = make_bench(tmp_path / "b")
        locked = tmp_path / "b" / "sites" / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            assert bench_sites.list_sites(HostShell(), bench) == ["locked"]
        finally:
            locked.chmod(0o755)


class TestDiscovery:
    @pytest.fixture(autouse=True)
    def _roots(self, monkeypatch, tmp_path):
        self.root = tmp_path / "root"
        self.root.mkdir()
        # Replace the defaults' container paths with tmp roots: a missing root, and
        # a real one given as a custom search path.
        monkeypatch.setattr(
            core_inspect.config_utils,
            "load_config",
            lambda: {"search_paths": {"custom_bench_paths": [str(self.root)]}},
        )

    def test_one_exec_finds_only_bench_shaped_dirs(self, tmp_path):
        good = make_bench(self.root / "good bench")
        (self.root / "not-a-bench" / "apps").mkdir(parents=True)  # no sites/
        container = HostShell()

        assert core_inspect.discover_benches(container) == [good]
        assert len(container.calls) == 1

    @pytest.mark.skipif(as_root, reason="root reads a mode-000 dir")
    def test_an_unreadable_subdir_no_longer_drops_the_whole_root(self, tmp_path):
        good = make_bench(self.root / "good")
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            # Precondition: find itself exits non-zero here, which used to discard
            # every bench found under this root.
            find = subprocess.run(
                ["find", str(self.root), "-maxdepth", "2", "-type", "d", "-name", "apps"],
                capture_output=True,
            )
            assert find.returncode != 0

            assert core_inspect.discover_benches(HostShell()) == [good]
        finally:
            locked.chmod(0o755)

    def test_a_failed_exec_is_no_benches(self):
        class Broken:
            def exec_run(self, cmd, workdir=None):
                return (126, b"OCI runtime exec failed: /workspace/x")

        assert core_inspect.discover_benches(Broken()) == []


class TestPartialRefresh:
    def test_one_exec_reads_every_bench(self, tmp_path):
        a = make_bench(tmp_path / "a", sites=["s1.localhost"], apps=("frappe", "erpnext"))
        b = make_bench(tmp_path / "b", sites=["s2.localhost"])
        cached = [
            {
                "path": a,
                "index": 0,
                "label": "main",
                "available_apps": ["frappe", "erpnext"],
                "sites": [
                    {
                        "name": "s1.localhost",
                        "installed_apps": ["frappe 16.0.0"],
                        "site_config": {"k": 1},
                    }
                ],
            },
            {
                "path": b,
                "index": 1,
                "available_apps": ["frappe"],
                "sites": [{"name": "s2.localhost", "installed_apps": []}],
            },
        ]
        container = HostShell()

        refreshed, drift = core_inspect.partial_refresh(container, cached)

        assert drift is False
        assert len(container.calls) == 1
        assert refreshed == [
            {
                "path": a,
                "sites": [
                    {
                        "name": "s1.localhost",
                        "installed_apps": ["frappe 16.0.0"],
                        "site_config": {"k": 1},
                    }
                ],
                "available_apps": ["erpnext", "frappe"],
                "index": 0,
                "label": "main",
            },
            {
                "path": b,
                "sites": [{"name": "s2.localhost", "installed_apps": []}],
                "available_apps": ["frappe"],
                "index": 1,
            },
        ]

    def test_a_vanished_bench_and_a_new_site_are_drift(self, tmp_path):
        a = make_bench(tmp_path / "a", sites=["s1.localhost", "new.localhost"])
        cached = [
            {"path": a, "available_apps": ["frappe"], "sites": [{"name": "s1.localhost"}]},
            {"path": str(tmp_path / "gone"), "available_apps": [], "sites": []},
        ]
        for c in cached[0]["sites"]:
            c["installed_apps"] = []

        refreshed, drift = core_inspect.partial_refresh(HostShell(), cached)

        assert drift is True
        assert [r["path"] for r in refreshed] == [a]
        assert sorted(s["name"] for s in refreshed[0]["sites"]) == [
            "new.localhost",
            "s1.localhost",
        ]

    def test_a_failed_exec_raises_so_callers_degrade_to_the_cache(self):
        class Broken:
            def exec_run(self, cmd, workdir=None):
                return (137, b"")

        with pytest.raises(CwcliError) as excinfo:
            core_inspect.partial_refresh(Broken(), [{"path": "/w/b"}])
        assert excinfo.value.kind is ErrorKind.DOCKER

    def test_a_missing_bench_record_raises_rather_than_reading_as_gone(self):
        class Truncated:
            def exec_run(self, cmd, workdir=None):
                return (0, b"B\t/w/a\nA\tfrappe\n")

        with pytest.raises(CwcliError, match="/w/b"):
            core_inspect.partial_refresh(Truncated(), [{"path": "/w/a"}, {"path": "/w/b"}])
