"""``core.inspect.resolve_bench_with_fallback`` - the no-cache auto-inspect
fallback shared by ``core.backup`` and ``core.restore`` (``restore_plan``/
``receive_plan``).

Root cause this pins: on a fresh/cleared cache, ``resolvers.resolve_bench``
returns ``None`` (nothing cached), and the pre-fix callers used that as their
cue to silently GUESS ``resolvers.DEFAULT_BENCH_PATH`` - never running
``inspect`` to discover the real bench. On a multi-bench project, or any
instance whose bench does not happen to sit at the hardcoded default path,
that guess is wrong and the operation dead-ends downstream (e.g. backup's
site lookup then fails ``site.no_default`` because the cache it reads from
was never populated). The fix: a cold cache runs ``inspect`` once to populate
it, then re-resolves - mirroring ``core.open``'s ``_fallback_populate``.
"""

import json

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/development/frappe-bench"


class FakeFrappeContainer:
    """Answers the real probes ``core.inspect`` issues for a single-bench project."""

    def __init__(self, *, bench_path=BENCH, status="running"):
        self.bench_path = bench_path
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.name = "proj-frappe-1"

    def reload(self):
        pass

    def start(self):
        self.status = "running"

    def exec_run(self, cmd, workdir=None):
        b = self.bench_path
        if isinstance(cmd, (list, tuple)):  # marker reads are list-form
            return (1, b"")
        if cmd.startswith("sh -c '") and "echo SITE" in cmd and "site_config.json" in cmd:
            return (1, b"")
        if "test -d" in cmd:
            return (0, b"")
        if cmd.startswith("find "):
            root = cmd.split()[1].rstrip("/")
            if self.bench_path.startswith(root + "/"):
                return (0, f"{b}/apps".encode())
            return (0, b"")
        if cmd == f"ls -1 {b}/apps":
            return (0, b"frappe")
        if cmd == f"ls -1 {b}/sites":
            return (0, b"apps.txt\ncommon_site_config.json\ndev.local")
        if cmd == f"cat {b}/sites/common_site_config.json":
            return (0, json.dumps({"default_site": "dev.local"}).encode())
        if cmd.startswith(f"cat {b}/sites/") and cmd.endswith("/site_config.json"):
            return (0, json.dumps({"db_name": "testdb"}).encode())
        if cmd.startswith("bench --site ") and "list-apps" in cmd:
            return (0, b"frappe 15.0.0 version-15")
        return (1, b"")


@pytest.fixture()
def wired(monkeypatch):
    """A cold (empty) in-memory cache + the core docker boundary patched."""
    store: dict[str, dict] = {}

    monkeypatch.setattr(core_inspect.db_utils, "get_cached_project_data", store.get)
    monkeypatch.setattr(
        core_inspect.db_utils,
        "cache_project_data",
        lambda name, benches: store.__setitem__(
            name, {"project_name": name, "bench_instances": benches, "last_updated": "now"}
        ),
    )
    monkeypatch.setattr(core_inspect.config_utils, "load_config", lambda: {})

    def install(container):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [container])

    return store, install


class TestColdCachePopulatesAndResolves:
    def test_populate_discovers_the_real_bench_not_the_guessed_default(self, monkeypatch, wired):
        """A bench NOT at the hardcoded default path is still found via inspect,
        rather than the caller silently guessing wrong."""
        store, install = wired
        container = FakeFrappeContainer(bench_path="/workspace/development/my-custom-bench")
        install(container)

        result = core_inspect.resolve_bench_with_fallback("proj", None, None)

        assert result.status is Status.OK
        assert result.data == "/workspace/development/my-custom-bench"
        assert result.data != core_inspect.resolvers.DEFAULT_BENCH_PATH
        # The cache was actually populated, not just read.
        assert store.get("proj") is not None

    def test_populate_finds_nothing_degrades_to_default_with_warning(self, monkeypatch, wired):
        """inspect runs successfully but discovers no bench at all (e.g. an empty
        workspace) - degrade to the historical default, never raise."""
        _store, install = wired
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])

        # A missing project during the populate is itself a hard CwcliError
        # (see the propagation test below); to exercise the "populate ran but
        # still nothing resolved" branch specifically, patch inspect itself to
        # a no-op success and leave the cache empty.
        monkeypatch.setattr(core_inspect, "inspect", lambda *a, **k: None)

        result = core_inspect.resolve_bench_with_fallback("proj", None, None)

        assert result.status is Status.OK
        assert result.data == core_inspect.resolvers.DEFAULT_BENCH_PATH
        assert any(w.code == "bench.default_used" for w in result.warnings)

    def test_hard_error_from_populate_propagates(self, monkeypatch, wired):
        """A real failure (no container, daemon unreachable, ...) must ABORT, never
        be papered over by the guessed default path."""
        _store, install = wired
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])

        with pytest.raises(CwcliError) as exc:
            core_inspect.resolve_bench_with_fallback("proj", None, None)
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_non_cwcli_error_from_populate_degrades_to_default(self, monkeypatch, wired):
        """A non-CwcliError exception (the disclosed degrade residue, same as
        ``core.open``) still degrades rather than propagating a raw traceback."""
        _store, install = wired

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(core_inspect, "inspect", boom)

        result = core_inspect.resolve_bench_with_fallback("proj", None, None)
        assert result.status is Status.OK
        assert result.data == core_inspect.resolvers.DEFAULT_BENCH_PATH
        assert any(w.code == "bench.default_used" for w in result.warnings)

    def test_populate_discovering_multiple_benches_yields_select_bench(self, monkeypatch, wired):
        """A multi-bench project discovered ONLY by the populate must still surface
        the multi-bench choice, never silently pick one."""
        store, install = wired
        # Pre-seed as if inspect had discovered two benches; skip a full multi-bench
        # container fake by patching `inspect` to perform the (real) cache write.
        install(FakeFrappeContainer())

        def fake_inspect(project_name, **kwargs):
            store[project_name] = {
                "project_name": project_name,
                "bench_instances": [{"path": BENCH}, {"path": "/workspace/second-bench"}],
                "last_updated": "now",
            }

        monkeypatch.setattr(core_inspect, "inspect", fake_inspect)

        result = core_inspect.resolve_bench_with_fallback("proj", None, None)
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"


class TestWarmCacheNeverPopulates:
    def test_cached_bench_skips_inspect_entirely(self, monkeypatch, wired):
        store, _install = wired
        store["proj"] = {
            "project_name": "proj",
            "bench_instances": [{"path": BENCH}],
            "last_updated": "now",
        }
        called = []
        monkeypatch.setattr(core_inspect, "inspect", lambda *a, **k: called.append(True))

        result = core_inspect.resolve_bench_with_fallback("proj", None, None)

        assert result.status is Status.OK
        assert result.data == BENCH
        assert called == []  # inspect never ran; the cache already answered

    def test_explicit_path_override_skips_inspect_entirely(self, monkeypatch, wired):
        called = []
        monkeypatch.setattr(core_inspect, "inspect", lambda *a, **k: called.append(True))

        result = core_inspect.resolve_bench_with_fallback("proj", None, "/explicit/path")

        assert result.status is Status.OK
        assert result.data == "/explicit/path"
        assert called == []
