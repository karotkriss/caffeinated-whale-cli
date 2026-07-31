"""``core.inspect``: the tier machine's contract, pinned AT THE CORE.

The command-surface behavior is pinned by ``test_inspect_characterization.py``
(byte-identical ``--json``/cache shapes) and ``test_inspect_partial_refresh.py``
(the T2 pass); this file pins the core function's own envelope contract - the
forks the CLI pre-resolves away, the write discipline per tier, the two disclosed
hardenings, silence, and DTO serializability.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict
from enum import Enum

import pytest
from docker.errors import APIError

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/development/frappe-bench"


class FakeFrappeContainer:
    """Answers the exact probes the tier machine issues, recording every call.

    Modeled on ``test_inspect_partial_refresh.FakeFrappeContainer``; adds the
    marker/currentsite probes and a ``status``/``reload``/``start`` surface so the
    core's run-state resolution works against it.
    """

    def __init__(self, apps, sites, bench_path=BENCH, status="running"):
        self.bench_path = bench_path
        self.apps = list(apps)
        self.sites = dict(sites)
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.name = "proj-frappe-1"
        self.calls: list = []
        self.start_calls = 0

    def reload(self):
        pass

    def start(self):
        self.start_calls += 1
        self.status = "running"

    def exec_run(self, cmd, workdir=None):
        self.calls.append(cmd)
        b = self.bench_path

        if isinstance(cmd, (list, tuple)):  # marker reads are list-form
            return (1, b"")
        if cmd.startswith("sh -c '") and "echo SITE" in cmd and "site_config.json" in cmd:
            entry = shlex.split(cmd)[-1].rsplit("/", 1)[-1]
            return (0, b"SITE\n") if entry in self.sites else (0, b"NOTASITE\n")
        if "test -d" in cmd:
            return (0, b"")
        if cmd.startswith("find "):
            root = cmd.split()[1].rstrip("/")
            if self.bench_path.startswith(root + "/"):
                return (0, f"{b}/apps".encode())
            return (0, b"")
        if cmd == f"ls -1 {b}/apps":
            return (0, "\n".join(self.apps).encode())
        if cmd == f"ls -1 {b}/sites":
            listing = ["apps.txt", "common_site_config.json", *self.sites.keys()]
            return (0, "\n".join(listing).encode())
        if cmd == f"cat {b}/sites/common_site_config.json":
            return (0, json.dumps({"default_site": next(iter(self.sites), "")}).encode())
        if cmd.startswith(f"cat {b}/sites/") and cmd.endswith("/site_config.json"):
            return (0, json.dumps({"db_name": "testdb"}).encode())
        if cmd.startswith("bench --site ") and "list-apps" in cmd:
            site = cmd.split()[2]
            return (0, "\n".join(self.sites.get(site, [])).encode())
        return (1, b"")

    def ran_find(self) -> bool:
        return any(isinstance(c, str) and c.startswith("find ") for c in self.calls)


@pytest.fixture()
def wired(monkeypatch):
    """In-memory cache + the core docker boundary patched; returns (store, writes, install)."""
    store: dict[str, dict] = {}
    writes: list = []

    def fake_get(name):
        return store.get(name)

    def fake_cache(name, benches):
        writes.append([dict(b) for b in benches])
        store[name] = {"project_name": name, "bench_instances": benches, "last_updated": "now"}

    monkeypatch.setattr(core_inspect.db_utils, "get_cached_project_data", fake_get)
    monkeypatch.setattr(core_inspect.db_utils, "cache_project_data", fake_cache)
    monkeypatch.setattr(core_inspect.config_utils, "load_config", lambda: {})

    def install(container):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [container])

    return store, writes, install


def _seed(store, available_apps=("frappe",), installed=("frappe 15.0.0 version-15",)):
    store["proj"] = {
        "project_name": "proj",
        "bench_instances": [
            {
                "path": BENCH,
                "sites": [{"name": "dev.local", "installed_apps": list(installed)}],
                "available_apps": list(available_apps),
            }
        ],
        "last_updated": "earlier",
    }


def _matching_container():
    return FakeFrappeContainer(apps=["frappe"], sites={"dev.local": ["frappe 15.0.0 version-15"]})


def _drifted_container():
    return FakeFrappeContainer(
        apps=["frappe", "newapp"],
        sites={"dev.local": ["frappe 15.0.0 version-15", "newapp 1.0.0 develop"]},
    )


# ------------------------------------------------------------------------- the tiers


class TestTier1CacheOnly:
    def test_serves_cache_verbatim_with_zero_container_calls(self, wired):
        store, writes, install = wired
        _seed(store)
        container = _drifted_container()  # disk differs; cache_only must not look
        install(container)

        result = core_inspect.inspect_raw("proj", refresh="cache_only")

        assert result.status is Status.OK
        assert result.data.served_from == "cache"
        assert result.data.degraded is False
        assert result.data.benches == store["proj"]["bench_instances"]
        assert container.calls == []
        assert writes == []

    def test_cache_only_with_no_cache_escalates_to_a_full_inspect(self, wired):
        store, writes, install = wired
        container = _matching_container()
        install(container)

        result = core_inspect.inspect_raw("proj", refresh="cache_only")

        assert result.data.served_from == "full"
        assert container.ran_find()
        assert len(writes) == 1

    def test_unknown_refresh_mode_is_a_usage_error(self, wired):
        with pytest.raises(CwcliError) as excinfo:
            core_inspect.inspect_raw("proj", refresh="fresh-please")
        assert excinfo.value.kind is ErrorKind.USAGE


class TestTier2:
    def test_no_drift_serves_cache_unchanged_and_never_writes(self, wired):
        store, writes, install = wired
        _seed(store)
        container = _matching_container()
        install(container)

        result = core_inspect.inspect_raw("proj")

        assert result.status is Status.OK
        assert result.data.served_from == "partial"
        assert result.data.benches == store["proj"]["bench_instances"]
        assert not container.ran_find()
        assert writes == []

    def test_drift_escalates_to_a_full_inspect_and_only_t3_writes(self, wired):
        store, writes, install = wired
        _seed(store)
        container = _drifted_container()
        install(container)

        result = core_inspect.inspect_raw("proj")

        assert result.data.served_from == "full"
        assert container.ran_find()
        assert len(writes) == 1  # exactly the T3 write; the T2 pass wrote nothing
        assert "newapp" in result.data.benches[0]["available_apps"]

    def test_stopped_project_serves_cache_and_starts_nothing_even_with_auto_start(self, wired):
        store, writes, install = wired
        _seed(store)
        container = FakeFrappeContainer(apps=[], sites={}, status="exited")
        install(container)

        result = core_inspect.inspect_raw("proj", auto_start=True)

        assert result.status is Status.OK
        assert result.data.served_from == "cache"
        assert result.data.benches == store["proj"]["bench_instances"]
        assert container.start_calls == 0
        assert container.calls == []  # not one exec against the stopped project
        assert writes == []

    def test_a_failing_partial_pass_degrades_to_the_cache_without_writing(self, wired):
        store, writes, install = wired
        _seed(store)

        class ExplodingContainer(FakeFrappeContainer):
            def exec_run(self, cmd, workdir=None):
                raise RuntimeError("mid-probe explosion")

        install(ExplodingContainer(apps=[], sites={}))

        result = core_inspect.inspect_raw("proj")

        assert result.status is Status.OK
        assert result.data.served_from == "cache"
        assert result.data.benches == store["proj"]["bench_instances"]
        assert writes == []

    def test_a_missing_project_is_a_hard_error_even_on_a_cache_hit(self, wired, monkeypatch):
        # Pre-migration behavior: the T2 run-state prologue exited on a project
        # whose containers are GONE (not merely stopped); the core preserves that
        # as a typed NOT_FOUND rather than serving the stale cache.
        store, _writes, _install = wired
        _seed(store)
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])

        with pytest.raises(CwcliError) as excinfo:
            core_inspect.inspect_raw("proj")
        assert excinfo.value.kind is ErrorKind.NOT_FOUND


class TestTier3Forks:
    def test_stopped_project_returns_confirm_start_at_call_time(self, wired):
        _store, writes, install = wired  # no cache -> T3 direct
        install(FakeFrappeContainer(apps=[], sites={}, status="exited"))

        result = core_inspect.inspect_raw("proj")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice is not None
        assert result.choice.kind == "confirm_start"
        assert writes == []

    def test_offer_choice_false_raises_not_running(self, wired):
        _store, writes, install = wired
        install(FakeFrappeContainer(apps=[], sites={}, status="exited"))

        with pytest.raises(CwcliError) as excinfo:
            core_inspect.inspect_raw("proj", offer_choice=False)
        assert excinfo.value.kind is ErrorKind.NOT_RUNNING
        assert writes == []

    def test_auto_start_suppresses_the_choice_and_proceeds(self, wired):
        # resolve_container_state(auto_start=True) reports start_requested; the
        # actual UI-coupled start is the CALLER's job (cf. core.update). The core
        # proceeds rather than returning a fork.
        _store, writes, install = wired
        container = FakeFrappeContainer(
            apps=["frappe"],
            sites={"dev.local": ["frappe 15.0.0 version-15"]},
            status="exited",
        )
        install(container)

        result = core_inspect.inspect_raw("proj", auto_start=True)

        assert result.status is Status.OK
        assert result.data.served_from == "full"
        assert len(writes) == 1

    def test_full_refresh_on_no_benches_fails_hard(self, wired):
        store, writes, install = wired
        _seed(store)
        # The bench lives outside every search root, so discovery finds nothing.
        container = FakeFrappeContainer(apps=[], sites={}, bench_path="/opt/benches/custom")
        install(container)

        with pytest.raises(CwcliError) as excinfo:
            core_inspect.inspect_raw("proj", refresh="full")
        assert excinfo.value.kind is ErrorKind.NOT_FOUND
        assert "No Bench Instances found" in excinfo.value.message
        assert writes == []

    def test_drift_escalation_that_rediscovers_nothing_degrades_without_persisting(self, wired):
        store, writes, install = wired
        custom_bench = "/opt/benches/custom-bench"
        store["proj"] = {
            "project_name": "proj",
            "bench_instances": [
                {
                    "path": custom_bench,
                    "sites": [
                        {"name": "dev.local", "installed_apps": ["frappe 15.0.0 version-15"]}
                    ],
                    "available_apps": ["frappe"],
                }
            ],
            "last_updated": "earlier",
        }
        # Drift trips (new app), but the bench is outside every search root.
        container = FakeFrappeContainer(
            apps=["frappe", "newapp"],
            sites={"dev.local": ["frappe 15.0.0 version-15"]},
            bench_path=custom_bench,
        )
        install(container)

        result = core_inspect.inspect_raw("proj")

        assert result.status is Status.WARNING
        assert result.data.degraded is True
        assert result.data.served_from == "cache"
        assert result.data.benches == store["proj"]["bench_instances"]
        assert writes == []
        assert any(w.code == "inspect.degraded" for w in result.warnings)


# -------------------------------------------------------------- disclosed hardenings


class TestHardenings:
    def test_a_non_utf8_probe_byte_no_longer_kills_the_inspect(self, wired):
        _store, writes, install = wired

        class MojibakeContainer(FakeFrappeContainer):
            def exec_run(self, cmd, workdir=None):
                if isinstance(cmd, str) and cmd == f"ls -1 {self.bench_path}/apps":
                    self.calls.append(cmd)
                    return (0, b"frappe\n\xff\xfe-app")  # invalid UTF-8 mid-listing
                return super().exec_run(cmd, workdir=workdir)

        install(MojibakeContainer(apps=["frappe"], sites={"dev.local": ["frappe 15"]}))

        result = core_inspect.inspect_raw("proj")  # no cache -> full inspect

        assert result.status is Status.OK  # completed, where it used to crash
        assert len(writes) == 1

    def test_a_dropped_daemon_connection_mid_fanout_is_typed_and_writes_nothing(self, wired):
        _store, writes, install = wired

        class DroppingContainer(FakeFrappeContainer):
            def exec_run(self, cmd, workdir=None):
                if isinstance(cmd, str) and cmd.startswith("find "):
                    raise APIError("connection aborted mid-fan-out")
                return super().exec_run(cmd, workdir=workdir)

        install(DroppingContainer(apps=[], sites={}))

        with pytest.raises(CwcliError) as excinfo:
            core_inspect.inspect_raw("proj")
        assert excinfo.value.kind is ErrorKind.DOCKER
        assert writes == []  # crash-without-corruption, now typed


class _DiscoveryContainer:
    """Answers only the ``find`` + ``_is_bench_directory`` probes discovery issues.

    ``bench_apps`` maps each real bench dir to its ``apps`` path; every ``find``
    returns the ones nested under the queried root, so overlapping roots surface
    the same bench more than once (which the dedup must collapse).
    """

    def __init__(self, bench_dirs):
        self.bench_dirs = list(bench_dirs)
        self.finds: list[str] = []

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None):
        if cmd.startswith("find "):
            root = cmd.split()[1].rstrip("/")
            self.finds.append(root)
            # a root may be an ancestor of a bench OR the bench dir itself (init
            # registers the bench dir as its own custom search root).
            hits = [f"{b}/apps" for b in self.bench_dirs if b == root or b.startswith(root + "/")]
            return (0, "\n".join(hits).encode())
        if "test -d" in cmd:  # _is_bench_directory
            return (0, b"")
        return (1, b"")


class TestDiscoverySearchRoots:
    def test_bare_workspace_root_is_searched(self, monkeypatch):
        monkeypatch.setattr(core_inspect.config_utils, "load_config", lambda: {})
        container = _DiscoveryContainer([])

        core_inspect.discover_benches(container)

        # bare /workspace is added; the deeper roots stay because the devcontainer
        # bench (/workspace/development/frappe-bench) has its apps at depth 3 from
        # bare /workspace, out of maxdepth-2 reach.
        assert "/workspace" in container.finds
        assert "/workspace/development" in container.finds
        assert "/home/frappe/workspace/development" in container.finds

    def test_a_hand_made_bench_under_workspace_is_found(self, monkeypatch):
        monkeypatch.setattr(core_inspect.config_utils, "load_config", lambda: {})
        container = _DiscoveryContainer(["/workspace/hand-made-bench"])

        found = core_inspect.discover_benches(container)

        assert found == ["/workspace/hand-made-bench"]

    def test_a_path_known_via_custom_root_and_the_default_yields_one_row(self, monkeypatch):
        # A bench registered as a custom path that ALSO sits under the new default
        # /workspace root is discovered by both, but must appear exactly once so the
        # path-keyed numeric identity is never reminted.
        bench = "/workspace/shared-bench"
        monkeypatch.setattr(
            core_inspect.config_utils,
            "load_config",
            lambda: {"search_paths": {"custom_bench_paths": [bench]}},
        )
        container = _DiscoveryContainer([bench])

        found = core_inspect.discover_benches(container)

        assert found == [bench]  # deduped, and sorted order is the selector contract


# ------------------------------------------------------------------ boundary hygiene


def _assert_plain(value, path="$"):
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_plain(v, f"{path}.{k}")
    elif isinstance(value, list):
        for idx, v in enumerate(value):
            _assert_plain(v, f"{path}[{idx}]")
    else:
        assert value is None or isinstance(
            value, (str, int, float, bool)
        ), f"non-plain value at {path}: {value!r}"
        assert not isinstance(value, Enum), f"enum leaked at {path}"


class TestBoundary:
    def test_the_core_prints_nothing_at_all(self, wired, capsys):
        store, _writes, install = wired
        _seed(store)
        install(_drifted_container())  # exercise T2 + escalation + T3 + the write

        core_inspect.inspect_raw("proj")
        core_inspect.inspect("proj")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_the_typed_report_is_plain_serializable_data(self, wired):
        store, _writes, install = wired
        store["proj"] = {
            "project_name": "proj",
            "bench_instances": [
                {
                    "path": BENCH,
                    "sites": [
                        {
                            "name": "dev.local",
                            "installed_apps": ["frappe 15.0.0 version-15"],
                            "site_config": {"db_name": "x"},
                        }
                    ],
                    "available_apps": ["frappe"],
                    "label": "primary",
                    "current_site": "dev.local",
                    "common_site_config": {"default_site": ""},
                }
            ],
            "last_updated": "earlier",
        }

        result = core_inspect.inspect("proj", refresh="cache_only")

        report = result.data
        assert report is not None
        _assert_plain(asdict(report))
        bench = report.benches[0]
        assert bench.index == 0
        assert bench.label == "primary"
        # default_site resolution: common_site_config.default_site is FALSY here,
        # so the currentsite.txt pointer is the fallback that resolves.
        assert bench.default_site == "dev.local"
        assert bench.sites[0].has_site_config is True
        # A cache_only read never observes the site's apps live, so the ref is
        # served REMEMBERED and the honest token says so.
        assert report.served_from == "cache"
        assert bench.sites[0].installed_apps_verified is False
        # The config CONTENT never crosses the typed boundary.
        assert "db_name" not in json.dumps(asdict(report))

    def test_verbose_trace_rides_the_event_channel_not_warnings(self, wired):
        _store, _writes, install = wired
        install(_matching_container())
        events: list = []

        result = core_inspect.inspect_raw("proj", on_event=events.append)  # cache miss -> T3

        assert result.warnings == []  # debug echoes never ride the envelope
        kinds = {type(e) for e in events}
        assert core_inspect.InspectCommand in kinds
        assert core_inspect.InspectTrace in kinds


class TestInstalledAppsVerifiedToken:
    """The fail-honest verified-or-remembered token on per-site installed_apps.

    A ``git checkout`` inside an app changes the ref carried in ``installed_apps``
    but touches neither the ``apps/`` listing nor the site set, so the cheap T1/T2
    tiers serve a stale ref while labeling the read ``cache``/``partial``. The
    token (and its warning) is how a caller tells a verified ref from a remembered
    one - the same fail-honest contract ``core.where`` carries. See the module.
    """

    def _warning_codes(self, result):
        return {w.code for w in result.warnings}

    def test_cache_only_serves_remembered_and_warns(self, wired):
        store, _writes, install = wired
        _seed(store)
        install(_drifted_container())  # cache_only must not look; token stays honest

        result = core_inspect.inspect("proj", refresh="cache_only")

        assert result.data.served_from == "cache"
        assert result.data.benches[0].sites[0].installed_apps_verified is False
        assert "inspect.apps_unverified" in self._warning_codes(result)

    def test_partial_no_drift_serves_remembered_and_warns(self, wired):
        # The exact defect shape: T2 confirms no drift and serves the cached ref,
        # which a checkout could have moved out from under it.
        store, _writes, install = wired
        _seed(store)
        install(_matching_container())

        result = core_inspect.inspect("proj")

        assert result.data.served_from == "partial"
        assert result.data.benches[0].sites[0].installed_apps_verified is False
        assert "inspect.apps_unverified" in self._warning_codes(result)

    def test_full_inspect_observes_the_ref_and_vouches(self, wired):
        store, _writes, install = wired
        _seed(store)
        install(_matching_container())

        result = core_inspect.inspect("proj", refresh="full")

        assert result.data.served_from == "full"
        assert result.data.benches[0].sites[0].installed_apps_verified is True
        assert "inspect.apps_unverified" not in self._warning_codes(result)

    def test_no_installed_apps_stays_quiet(self, wired):
        # Nothing remembered to be stale about -> no unverified nag.
        store, _writes, install = wired
        _seed(store, installed=())
        install(_matching_container())

        result = core_inspect.inspect("proj", refresh="cache_only")

        assert result.data.benches[0].sites[0].installed_apps_verified is False
        assert "inspect.apps_unverified" not in self._warning_codes(result)
