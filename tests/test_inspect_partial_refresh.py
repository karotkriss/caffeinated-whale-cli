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
- **T2** - a lightweight "partial inspect": on a cache hit with running
  containers, cheaply re-read ``ls apps`` / ``ls sites`` / configs for the KNOWN
  benches only. It deliberately does NOT run the ``find`` instance-discovery or
  the deep per-site ``bench list-apps``; cached per-site installed lists are
  carried forward. A freshly installed app shows up in ``apps/`` so this cheap
  ``ls`` catches it.
- **T3** - the full inspect, unchanged. T2 escalates to it on drift so the deep
  per-site lists (and any brand-new bench) are refreshed too.

These tests pin: the stale-cache bug is fixed by default, T2 stays cheap when
nothing changed, ``--no-refresh`` preserves the old instant cached return, and
the helper ``open --app`` reuses refreshes the available-apps list.
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

    Returns ``(store, install_container)`` where ``store`` is the fake cache dict
    and ``install_container(container)`` makes inspect use that container.
    """
    store: dict[str, dict] = {}

    def fake_get(name):
        return store.get(name)

    def fake_cache(name, benches):
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

    return store, install_container


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
        store, install_container = patched_inspect
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
        # And the cache is now fresh in both places.
        cached = store["proj"]["bench_instances"][0]
        assert "newapp" in cached["available_apps"]
        assert "newapp 1.0.0 develop" in cached["sites"][0]["installed_apps"]


class TestPartialPassStaysCheapWhenNothingChanged:
    """No drift -> serve the cheap refresh; never pay for find / list-apps."""

    def test_no_drift_skips_find_and_deep_list_apps(self, patched_inspect, capsys):
        store, install_container = patched_inspect
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


class TestNoRefreshOptOut:
    """``--no-refresh`` restores the old instant cached return (may be stale)."""

    def test_no_refresh_returns_cache_verbatim_and_touches_no_container(
        self, patched_inspect, capsys
    ):
        store, install_container = patched_inspect
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

        refreshed, drift = inspect_mod._partial_inspect_known_benches(container, cached_benches)

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

        _refreshed, drift = inspect_mod._partial_inspect_known_benches(container, cached_benches)

        assert drift is False


class TestOpenAppRefreshHelper:
    """``open --app`` reuses ``refresh_known_benches_cache`` so a just-installed
    app becomes available without a manual inspect."""

    def test_refresh_makes_new_app_available(self, monkeypatch):
        store: dict[str, dict] = {}
        store["proj"] = {
            "project_name": "proj",
            "bench_instances": [
                {
                    "path": BENCH,
                    "available_apps": ["frappe"],
                    "sites": [
                        {"name": "dev.local", "installed_apps": ["frappe 15.0.0 version-15"]}
                    ],
                }
            ],
            "last_updated": "earlier",
        }
        monkeypatch.setattr(
            inspect_mod.db_utils, "get_cached_project_data", lambda name: store.get(name)
        )
        monkeypatch.setattr(
            inspect_mod.db_utils,
            "cache_project_data",
            lambda name, benches: store.__setitem__(
                name, {"project_name": name, "bench_instances": benches, "last_updated": "now"}
            ),
        )

        container = FakeFrappeContainer(
            apps=["frappe", "newapp"], sites={"dev.local": ["frappe 15.0.0 version-15"]}
        )

        drift = inspect_mod.refresh_known_benches_cache(container, "proj")

        assert drift is True
        # The membership check `open --app` performs now sees the new app.
        assert "newapp" in store["proj"]["bench_instances"][0]["available_apps"]
        # Still cheap: no find discovery, no deep per-site listing.
        assert not container.ran_find()
        assert not container.ran_list_apps()

    def test_returns_none_without_cache(self, monkeypatch):
        monkeypatch.setattr(inspect_mod.db_utils, "get_cached_project_data", lambda name: None)
        container = FakeFrappeContainer(apps=["frappe"], sites={})
        assert inspect_mod.refresh_known_benches_cache(container, "proj") is None
