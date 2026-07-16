"""Regression tests for label recovery from marker files.

Contract: a user label lives BOTH in the SQLite cache and in the per-bench marker
``<bench-root>/.cwcli/.bench-label``. If the cache is lost/empty, a full inspect
(``--update`` / cache-miss) must rebuild each label from its marker while
re-deriving the rest of the bench config live.

These drive the real ``inspect`` command against a fake multi-bench container that
answers the full-inspect probes AND the marker ``cat``.
"""

import json

import pytest

from caffeinated_whale_cli.commands import inspect as inspect_mod
from caffeinated_whale_cli.core import docker as core_docker

BENCH_A = "/workspace/frappe-bench"
BENCH_B = "/workspace/frappe-bench-2"
MARKER_A = f"{BENCH_A}/.cwcli/.bench-label"
MARKER_B = f"{BENCH_B}/.cwcli/.bench-label"


class _StubDockerClient:
    """Stand-in for ``docker.from_env()`` so the ``@handle_docker_errors``
    daemon ``ping()`` succeeds without a real Docker daemon."""

    def ping(self):
        return True


class TwoBenchContainer:
    """Answers full-inspect probes for two benches, plus marker ``cat`` reads.

    ``markers`` maps a marker path -> label string (its JSON is synthesized).
    """

    def __init__(self, markers=None):
        self.benches = [BENCH_A, BENCH_B]
        self.markers = dict(markers or {})
        self.calls: list = []
        self.labels = {"com.docker.compose.service": "frappe"}
        self.status = "running"
        self.name = "fake-frappe"

    def reload(self):
        # The core's resolve_container_state refreshes the handle before reading
        # .status; a fake's state is already current.
        pass

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append(cmd)

        # Marker reads are list-form: ["cat", "<bench>/.cwcli/.bench-label"].
        if isinstance(cmd, (list, tuple)):
            if cmd[:1] == ["cat"]:
                path = cmd[1]
                if path in self.markers:
                    payload = json.dumps({"schema": 1, "label": self.markers[path]})
                    return (0, payload.encode())
                return (1, b"")
            return (1, b"")

        # String-form probes from the full inspect.
        if "test -d" in cmd:  # _is_bench_directory
            return (0, b"")
        if cmd.startswith("find "):  # discovery: return both bench apps dirs
            return (0, f"{BENCH_A}/apps\n{BENCH_B}/apps".encode())
        for b in self.benches:
            if cmd == f"ls -1 {b}/apps":
                return (0, b"frappe")
            if cmd == f"ls -1 {b}/sites":
                return (0, b"apps.txt\ncommon_site_config.json")
            if cmd == f"cat {b}/sites/common_site_config.json":
                return (0, json.dumps({"default_site": ""}).encode())
        if cmd.startswith("bench --site ") and "list-apps" in cmd:
            return (0, b"frappe 15.0.0 version-15")
        return (1, b"")


@pytest.fixture()
def wired(monkeypatch):
    """In-memory cache + a fake container, wired into inspect's collaborators."""
    store: dict[str, dict] = {}
    writes: list = []

    def fake_get(name):
        return store.get(name)

    def fake_cache(name, benches):
        writes.append([dict(b) for b in benches])
        store[name] = {"project_name": name, "bench_instances": benches, "last_updated": "now"}

    monkeypatch.setattr(inspect_mod.db_utils, "get_cached_project_data", fake_get)
    monkeypatch.setattr(inspect_mod.db_utils, "cache_project_data", fake_cache)
    monkeypatch.setattr(inspect_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(inspect_mod.config_utils, "get_show_tips", lambda: False)
    # `inspect` is wrapped by ``@handle_docker_errors``, which probes the real
    # environment (``shutil.which("docker")`` + ``docker.from_env().ping()``)
    # before the body runs. Neutralize that probe so these unit tests don't
    # depend on Docker being installed/running (it is absent on CI runners).
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("docker.from_env", lambda: _StubDockerClient())

    def install(container):
        # The tier machine resolves the container through core.docker now; the
        # command module binding is patched too while it exists (raising=False).
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [container])
        monkeypatch.setattr(
            inspect_mod, "get_project_containers", lambda name: [container], raising=False
        )

    return store, writes, install


def _run_full_inspect():
    inspect_mod.inspect(
        project_name="proj",
        verbose=False,
        json_output=True,
        update=True,  # force a full inspect (the DB-loss / cache-miss path)
        no_refresh=False,
        show_apps=False,
        interactive=False,
        yes=False,
        prompt_to_start=True,
    )


def test_full_inspect_recovers_label_from_marker(wired):
    store, writes, install = wired
    # Cache is empty (DB lost); only the marker holds the label.
    install(TwoBenchContainer(markers={MARKER_B: "staging"}))

    _run_full_inspect()

    benches = store["proj"]["bench_instances"]
    by_path = {b["path"]: b for b in benches}
    assert by_path[BENCH_A].get("label") is None  # no marker -> numeric index only
    assert by_path[BENCH_B]["label"] == "staging"  # recovered from marker
    # It was persisted, not just displayed.
    assert writes and any(b.get("label") == "staging" for b in writes[-1])


def test_full_inspect_no_markers_leaves_labels_unset(wired):
    store, writes, install = wired
    install(TwoBenchContainer(markers={}))

    _run_full_inspect()

    for b in store["proj"]["bench_instances"]:
        assert "label" not in b or b["label"] is None


def test_recovered_labels_are_addressable_by_selector(wired):
    """After recovery, the label resolves as a --bench selector against the cache."""
    from caffeinated_whale_cli.utils import bench_labels

    store, _writes, install = wired
    install(TwoBenchContainer(markers={MARKER_A: "primary", MARKER_B: "staging"}))

    _run_full_inspect()

    benches = store["proj"]["bench_instances"]
    assert bench_labels.resolve_bench(benches, "staging")["path"] == BENCH_B
    assert bench_labels.resolve_bench(benches, "primary")["path"] == BENCH_A
    # Numeric index still works and is stable (sorted discovery order).
    assert bench_labels.resolve_bench(benches, "0")["path"] == BENCH_A
    assert bench_labels.resolve_bench(benches, "1")["path"] == BENCH_B
