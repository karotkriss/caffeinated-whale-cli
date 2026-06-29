"""Regression tests for issue #27: a just-installed app must be visible to
``cwcli inspect`` (and ``cwcli open --app``) WITHOUT a manual ``inspect -u``.

Root cause
----------
``inspect`` returned cached project data with no freshness check. An app
installed after the cache was written lands in the bench ``apps/`` directory
immediately, but the cache still held the old app list, so the app stayed
invisible until ``inspect --update`` rebuilt the cache. ``open --app <name>``
read the same stale cache and errored "App not found".

The fix (3-tier inspect)
------------------------
- **T1** - cache return, unchanged, instant (used with ``--no-refresh`` or when
  the containers are down).
- **T2** - a lightweight, read-only "partial inspect": on a cache hit with running
  containers, cheaply re-read only ``ls apps`` / ``ls sites`` for the KNOWN
  benches. It deliberately does NOT run the ``find`` instance-discovery, the deep
  per-site ``bench list-apps``, or any config re-read; cached per-site installed
  lists and configs are carried forward. It NEVER writes the cache: on no drift
  the cache is served unchanged, on drift it escalates. A freshly installed app
  shows up in ``apps/`` so this cheap ``ls`` catches it.
- **T3** - the full inspect, unchanged. T2 escalates to it on drift so the deep
  per-site lists (and any brand-new bench) are refreshed and persisted too.

These tests pin: the stale-cache bug is fixed by default, T2 stays cheap and
read-only when nothing changed, ``--no-refresh`` preserves the old instant cached
return, and ``open --app`` reuses the T2 pass in-memory without writing the cache.
"""

import json

import pytest

from caffeinated_whale_cli.commands import inspect as inspect_mod

BENCH = "/home/frappe/frappe-bench"


class FakeFrappeContainer:
    """A stand-in frappe container that answers the exact ``exec_run`` probes
    inspect issues, and records every command so tests can assert which tier ran.

    ``apps`` is the on-disk ``apps/`` listing (what ``ls apps`` returns) and
    ``sites`` maps each site name to its ``bench list-apps`` output lines.
    """

    def __init__(self, apps, sites, bench_path=BENCH):
        self.bench_path = bench_path
        self.apps = list(apps)
        self.sites = dict(sites)
        self.calls: list[str] = []
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        b = self.bench_path

        if "test -d" in cmd:  # _is_bench_directory
            return (0, b"")
        if cmd.startswith("find "):  # _find_bench_instances (full inspect only)
            root = cmd.split()[1].rstrip("/")
            if self.bench_path.startswith(root + "/"):
                return (0, f"{b}/apps".encode())
            return (0, b"")
        if cmd == f"ls -1 {b}/apps":  # _get_available_apps (cheap)
            return (0, "\n".join(self.apps).encode())
        if cmd == f"ls -1 {b}/sites":  # _get_sites (cheap)
            listing = ["apps.txt", "common_site_config.json", *self.sites.keys()]
            return (0, "\n".join(listing).encode())
        if cmd == f"cat {b}/sites/common_site_config.json":
            return (0, json.dumps({"default_site": next(iter(self.sites), "")}).encode())
        if cmd.startswith(f"cat {b}/sites/") and cmd.endswith("/site_config.json"):
            return (0, json.dumps({"db_name": "testdb"}).encode())
        if cmd.startswith("bench --site ") and "list-apps" in cmd:  # deep, full inspect only
            site = cmd.split()[2]
            return (0, "\n".join(self.sites.get(site, [])).encode())
        return (1, b"")

    # convenience predicates over recorded calls
    def ran_find(self) -> bool:
        return any(c.startswith("find ") for c in self.calls)

    def ran_list_apps(self) -> bool:
        return any(c.startswith("bench --site ") and "list-apps" in c for c in self.calls)

    def ran_ls_apps(self) -> bool:
        return any(c == f"ls -1 {self.bench_path}/apps" for c in self.calls)


@pytest.fixture()
def patched_inspect(monkeypatch):
    """Wire inspect's collaborators to an in-memory cache + a fake container.

    Returns ``(store, install_container, writes)`` where ``store`` is the fake
    cache dict, ``install_container(container)`` makes inspect use that container,
    and ``writes`` records each ``cache_project_data`` call so a test can assert
    whether a tier persisted (T2 no-drift must NOT write; T3 must).
    """
    store: dict[str, dict] = {}
    writes: list[str] = []

    def fake_get(name):
        return store.get(name)

    def fake_cache(name, benches):
        writes.append(name)
        store[name] = {
            "project_name": name,
            "bench_instances": benches,
            "last_updated": "now",
        }

    monkeypatch.setattr(inspect_mod.db_utils, "get_cached_project_data", fake_get)
    monkeypatch.setattr(inspect_mod.db_utils, "cache_project_data", fake_cache)
    # Containers are "running"; never prompt, never start (safe, non-disruptive).
    monkeypatch.setattr(inspect_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(inspect_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(inspect_mod.config_utils, "load_config", lambda: {})

    holder: dict = {}

    def install_container(container):
        holder["c"] = container
        monkeypatch.setattr(inspect_mod, "get_project_containers", lambda name: [container])

    return store, install_container, writes


def _run_inspect(**overrides):
    """Invoke ``inspect`` with every option explicit.

    ``inspect`` is a Typer command, so calling it directly leaves any omitted
    parameter as its ``typer.Option(...)`` default object (which is truthy) -
    e.g. ``update`` would silently force a full inspect. Real callers
    (``recache_project``, ``auto_inspect``) always pass all params explicitly;
    the tests do the same.
    """
    kwargs = dict(
        project_name="proj",
        verbose=False,
        json_output=True,
        update=False,
        no_refresh=False,
        show_apps=False,
        interactive=False,
        prompt_to_start=True,
    )
    kwargs.update(overrides)
    inspect_mod.inspect(**kwargs)


def _seed_cache(store, available_apps, installed_apps):
    """Seed the fake cache with one bench/one site, as a prior inspect would."""
    store["proj"] = {
        "project_name": "proj",
        "bench_instances": [
            {
                "path": BENCH,
                "available_apps": list(available_apps),
                "sites": [{"name": "dev.local", "installed_apps": list(installed_apps)}],
                "common_site_config": {"default_site": "dev.local"},
            }
        ],
        "last_updated": "earlier",
    }


class TestStaleCacheBugIsFixed:
    """The original report: install an app, then plain ``inspect`` can't see it."""

    def test_just_installed_app_is_found_without_update(self, patched_inspect, capsys):
        store, install_container, writes = patched_inspect
        # Cache was written when only 'frappe' existed.
        _seed_cache(store, available_apps=["frappe"], installed_apps=["frappe 15.0.0 version-15"])
        # On disk now: an app was installed - present in apps/ and on the site.
        container = FakeFrappeContainer(
            apps=["frappe", "newapp"],
            sites={"dev.local": ["frappe 15.0.0 version-15", "newapp 1.0.0 develop"]},
        )
        install_container(container)

        _run_inspect()

        out = capsys.readouterr().out
        # The just-installed app is visible WITHOUT a manual `inspect -u`.
        assert "newapp" in out
        # Drift was detected by the cheap pass, which escalated to a full inspect:
        assert container.ran_find(), "drift should escalate to the full inspect (find discovery)"
        assert container.ran_list_apps(), "escalation should refresh deep per-site installed apps"
        # Only the full inspect (T3) persisted; the read-only T2 pass never writes.
        assert writes == ["proj"]
        # And the cache is now fresh in both places.
        cached = store["proj"]["bench_instances"][0]
        assert "newapp" in cached["available_apps"]
        assert "newapp 1.0.0 develop" in cached["sites"][0]["installed_apps"]


class TestPartialPassStaysCheapWhenNothingChanged:
    """No drift -> serve the cheap refresh; never pay for find / list-apps."""

    def test_no_drift_skips_find_and_deep_list_apps(self, patched_inspect, capsys):
        store, install_container, writes = patched_inspect
        _seed_cache(store, available_apps=["frappe"], installed_apps=["frappe 15.0.0 version-15"])
        # Disk matches the cache exactly.
        container = FakeFrappeContainer(
            apps=["frappe"],
            sites={"dev.local": ["frappe 15.0.0 version-15"]},
        )
        install_container(container)

        _run_inspect()

        out = capsys.readouterr().out
        assert "frappe" in out and "dev.local" in out
        # The cheap freshness read happened...
        assert container.ran_ls_apps()
        # ...but the expensive instance-discovery and per-site boot did NOT.
        assert not container.ran_find(), "partial pass must skip `find` discovery"
        assert not container.ran_list_apps(), "partial pass must skip deep `bench list-apps`"
        # No config files were re-read on this read-only pass.
        assert not any(c.startswith("cat ") for c in container.calls)
        # And no-drift is a pure read: the cache was served unchanged, never written.
        assert writes == []


class TestNoRefreshOptOut:
    """``--no-refresh`` restores the old instant cached return (may be stale)."""

    def test_no_refresh_returns_cache_verbatim_and_touches_no_container(
        self, patched_inspect, capsys
    ):
        store, install_container, writes = patched_inspect
        _seed_cache(store, available_apps=["frappe"], installed_apps=["frappe 15.0.0 version-15"])
        container = FakeFrappeContainer(
            apps=["frappe", "newapp"],
            sites={"dev.local": ["frappe 15.0.0 version-15", "newapp 1.0.0 develop"]},
        )
        install_container(container)

        _run_inspect(no_refresh=True)

        out = capsys.readouterr().out
        # Stale cache served as-is: the new app is NOT shown.
        assert "newapp" not in out
        # And no container work happened at all on this fast path.
        assert container.calls == []
        # The fast path is read-only too: nothing persisted.
        assert writes == []


class TestPartialInspectHelper:
    """Unit-level contract of the T2 helper itself."""

    def test_detects_drift_carries_installed_apps_and_skips_deep(self):
        container = FakeFrappeContainer(
            apps=["frappe", "newapp"],
            sites={"dev.local": ["frappe 15.0.0 version-15", "newapp 1.0.0 develop"]},
        )
        cached_benches = [
            {
                "path": BENCH,
                "available_apps": ["frappe"],
                "sites": [{"name": "dev.local", "installed_apps": ["frappe 15.0.0 version-15"]}],
            }
        ]

        refreshed, drift = inspect_mod.partial_inspect_known_benches(container, cached_benches)

        assert drift is True
        assert refreshed[0]["available_apps"] == ["frappe", "newapp"]
        # Per-site installed list is CARRIED FORWARD (cheap pass does not deep-list).
        assert refreshed[0]["sites"][0]["installed_apps"] == ["frappe 15.0.0 version-15"]
        # It must not have re-discovered instances or booted bench per site.
        assert not container.ran_find()
        assert not container.ran_list_apps()

    def test_no_drift_when_disk_matches_cache(self):
        container = FakeFrappeContainer(
            apps=["frappe"], sites={"dev.local": ["frappe 15.0.0 version-15"]}
        )
        cached_benches = [
            {
                "path": BENCH,
                "available_apps": ["frappe"],
                "sites": [{"name": "dev.local", "installed_apps": ["frappe 15.0.0 version-15"]}],
            }
        ]

        _refreshed, drift = inspect_mod.partial_inspect_known_benches(container, cached_benches)

        assert drift is False


class TestOpenAppInMemoryRefresh:
    """``open --app`` reuses ``partial_inspect_known_benches`` IN-MEMORY: it
    surfaces a just-installed app for the membership check WITHOUT writing the
    cache, so the next plain ``inspect`` can still self-heal via escalate-on-drift
    (refreshing the deep per-site installed lists too)."""

    def test_returns_fresh_apps_carries_config_and_never_writes_cache(self, monkeypatch):
        writes: list = []
        monkeypatch.setattr(
            inspect_mod.db_utils,
            "cache_project_data",
            lambda *a, **k: writes.append(a),
        )

        cached_benches = [
            {
                "path": BENCH,
                "available_apps": ["frappe"],
                "common_site_config": {"default_site": "dev.local"},
                "sites": [
                    {
                        "name": "dev.local",
                        "installed_apps": ["frappe 15.0.0 version-15"],
                        "site_config": {"db_name": "olddb"},
                    }
                ],
            }
        ]
        # A new app is on disk (in apps/), but the site has not been booted/listed.
        container = FakeFrappeContainer(
            apps=["frappe", "newapp"], sites={"dev.local": ["frappe 15.0.0 version-15"]}
        )

        refreshed, drift = inspect_mod.partial_inspect_known_benches(container, cached_benches)

        assert drift is True
        # The membership check `open --app` performs now sees the new app, in-memory.
        assert refreshed[0]["available_apps"] == ["frappe", "newapp"]
        # Per-site installed apps and BOTH configs are carried forward from the cache,
        # never re-read (so a transient unreadable config can't drop them).
        assert refreshed[0]["sites"][0]["installed_apps"] == ["frappe 15.0.0 version-15"]
        assert refreshed[0]["sites"][0]["site_config"] == {"db_name": "olddb"}
        assert refreshed[0]["common_site_config"] == {"default_site": "dev.local"}
        assert not any(c.startswith("cat ") for c in container.calls), "no config re-reads"
        # Still cheap: no find discovery, no deep per-site listing.
        assert not container.ran_find()
        assert not container.ran_list_apps()
        # Pure read: the helper never persisted to the cache.
        assert writes == []


class TwoBenchContainer:
    """Models two cached benches where the FIRST has vanished from disk and the
    second is still present (with a freshly installed app). Used to pin that the
    partial pass drops the vanished bench and that callers must match by path."""

    def __init__(self, present_path, present_apps, present_sites):
        self.present_path = present_path
        self.present_apps = list(present_apps)
        self.present_sites = dict(present_sites)
        self.calls: list[str] = []
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        p = self.present_path
        if "test -d" in cmd:  # only the present bench passes the directory check
            return (0, b"") if f"test -d {p}/sites" in cmd else (1, b"")
        if cmd == f"ls -1 {p}/apps":
            return (0, "\n".join(self.present_apps).encode())
        if cmd == f"ls -1 {p}/sites":
            listing = ["apps.txt", "common_site_config.json", *self.present_sites.keys()]
            return (0, "\n".join(listing).encode())
        return (1, b"")


class TestPartialPassDropsVanishedBench:
    """A cached bench whose directory has vanished is dropped from the refreshed
    list, so the result is index-shifted - callers must select by path, not [0]."""

    def test_vanished_bench_dropped_and_path_match_finds_correct_apps(self):
        present = "/home/frappe/bench-two"
        cached_benches = [
            {
                "path": "/home/frappe/bench-one",  # vanished on disk
                "available_apps": ["frappe"],
                "sites": [{"name": "one.local", "installed_apps": ["frappe 15.0.0 version-15"]}],
            },
            {
                "path": present,
                "available_apps": ["frappe"],
                "sites": [{"name": "two.local", "installed_apps": ["frappe 15.0.0 version-15"]}],
            },
        ]
        container = TwoBenchContainer(
            present_path=present,
            present_apps=["frappe", "newapp"],
            present_sites={"two.local": ["frappe 15.0.0 version-15"]},
        )

        refreshed, drift = inspect_mod.partial_inspect_known_benches(container, cached_benches)

        # The vanished bench trips drift and is dropped -> refreshed is index-shifted.
        assert drift is True
        assert len(refreshed) == 1
        assert refreshed[0]["path"] == present
        # Indexing refreshed[0] to answer about bench-one would pick the WRONG bench;
        # matching by path yields the surviving bench's fresh apps (guards open --app).
        match = next((b for b in refreshed if b["path"] == present), None)
        assert match is not None
        assert match["available_apps"] == ["frappe", "newapp"]
        # There is no refreshed entry at all for the vanished bench.
        assert all(b["path"] != "/home/frappe/bench-one" for b in refreshed)


class TestDriftEscalationDegradesWhenBenchNotDiscoverable:
    """On drift the full inspect runs, but if the bench can no longer be discovered
    (e.g. its custom search path was removed), a plain ``inspect`` degrades to the
    cached data instead of hard-failing with ``typer.Exit(1)``."""

    def test_undiscoverable_bench_serves_cache_without_writing(self, patched_inspect, capsys):
        store, install_container, writes = patched_inspect
        # The bench lives outside the default search roots, so `find` discovers nothing.
        custom_bench = "/opt/benches/custom-bench"
        store["proj"] = {
            "project_name": "proj",
            "bench_instances": [
                {
                    "path": custom_bench,
                    "available_apps": ["frappe"],
                    "sites": [
                        {"name": "dev.local", "installed_apps": ["frappe 15.0.0 version-15"]}
                    ],
                    "common_site_config": {"default_site": "dev.local"},
                }
            ],
            "last_updated": "earlier",
        }
        # On disk: a new app is present (trips drift), the known bench still exists.
        container = FakeFrappeContainer(
            apps=["frappe", "newapp"],
            sites={"dev.local": ["frappe 15.0.0 version-15", "newapp 1.0.0 develop"]},
            bench_path=custom_bench,
        )
        install_container(container)

        # Must NOT raise typer.Exit - degrade to cached data instead.
        _run_inspect()

        out = capsys.readouterr().out
        # Drift escalated to a full inspect: the `find` discovery actually ran...
        assert container.ran_find(), "drift should escalate to the full inspect (find discovery)"
        # ...found nothing, and degraded to the cached bench (still shown, exit code 0).
        assert custom_bench in out
        # The degrade path is a pure read: the cache is left intact for the next inspect.
        assert writes == []
