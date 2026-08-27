"""Characterization net for the ``inspect`` migration (openspec ``migrate-inspect-core``).

Pinned GREEN against the UNMIGRATED command first (batch 4's discipline), then the
tier machine moves underneath. Everything here patches only seams that SURVIVE the
migration, so this file never has to change when the subject moves:

- the docker boundary: ``core.docker.get_project_containers`` (plus the command
  module's own binding, ``raising=False`` so it keeps working once that binding is
  gone) and the ``@handle_docker_errors`` preflight;
- the shared module objects ``db_utils`` / ``config_utils`` (setattr on the module
  mutates the one object every layer imports);
- the CLI start seam ``commands.utils._start_containers_for_command`` (the frontend
  keeps performing the UI-coupled start on both sides of the migration).

What it pins, byte-for-byte where the spec demands bytes:

- the human ``--json`` output for a representative MULTI-bench fixture on the
  T3 (cache-miss full), T1 (``--no-refresh``), and T2 (cache-hit no-drift) paths -
  including the key-order difference between a gathered bench dict (current_site
  before label) and a cache-read bench dict (label before current_site);
- the exact cache dict shape a full inspect persists (task 1.3's anchor);
- T2 passivity: a stopped project on a cache hit serves the cache and starts
  NOTHING, even under ``--yes`` (zero container calls);
- T3 auto-start under ``--yes`` and the non-TTY-without-``--yes`` refusal (exit 1);
- the removed ``--show-apps`` flag: rejected as an unknown option (exit 2);
- the tree renderer's "(default)" resolution order (common_site_config first,
  current_site fallback).
"""

import json
import shlex

import click
import pytest
import typer

from caffeinated_whale_cli.commands import inspect as inspect_mod
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.utils import config_utils, db_utils

ROOT = "/workspace/development"
BENCH_A = f"{ROOT}/bench-a"
BENCH_B = f"{ROOT}/bench-b"
MARKER_B = f"{BENCH_B}/.cwcli/.bench-label"


class _StubDockerClient:
    def ping(self):
        return True


class MultiBenchContainer:
    """Answers every probe a full/partial inspect issues, for two benches.

    Bench A: two apps, one site with a site_config, a common_site_config carrying
    ``default_site``, and a ``currentsite.txt``. Bench B: one app, one site with
    no configs at all, and a user-label marker file - together they exercise every
    optional key in the cache dict shape.
    """

    def __init__(self, status="running"):
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.name = "proj-frappe-1"
        self.calls: list = []
        self.start_calls = 0
        self.benches = {
            BENCH_A: {
                "apps": ["erpnext", "frappe"],
                "sites": {"a.localhost": ["frappe 15.0.0 version-15", "erpnext 15.0.0 version-15"]},
                "site_configs": {"a.localhost": {"db_name": "adb"}},
                "common_site_config": {"default_site": "a.localhost"},
                "current_site": "a.localhost",
            },
            BENCH_B: {
                "apps": ["frappe"],
                "sites": {"b.localhost": ["frappe 15.0.0 version-15"]},
                "site_configs": {},
                "common_site_config": None,
                "current_site": None,
            },
        }
        self.markers = {MARKER_B: "staging"}

    def reload(self):
        pass

    def start(self):
        self.start_calls += 1
        self.status = "running"

    def exec_run(self, cmd, workdir=None, environment=None):
        self.calls.append(cmd)

        # Marker reads are list-form: ["cat", "<bench>/.cwcli/.bench-label"].
        if isinstance(cmd, (list, tuple)):
            if cmd[:1] == ["cat"] and cmd[1] in self.markers:
                payload = json.dumps({"schema": 1, "label": self.markers[cmd[1]]})
                return (0, payload.encode())
            return (1, b"")

        # Fail-safe site-classification probe (bench_sites.list_sites).
        if cmd.startswith("sh -c '") and "echo SITE" in cmd and "site_config.json" in cmd:
            entry_dir = shlex.split(cmd)[-1]
            bench, entry = entry_dir.rsplit("/sites/", 1)
            sites = self.benches.get(bench, {}).get("sites", {})
            return (0, b"SITE\n") if entry in sites else (0, b"NOTASITE\n")

        if "test -d" in cmd:  # _is_bench_directory
            return (0, b"")

        if cmd.startswith("find "):
            root = cmd.split()[1]
            if root == ROOT:
                return (0, "\n".join(f"{b}/apps" for b in self.benches).encode())
            return (1, b"")

        for bench, spec in self.benches.items():
            if cmd == f"ls -1 {bench}/apps":
                return (0, "\n".join(spec["apps"]).encode())
            if cmd == f"ls -1 {bench}/sites":
                listing = ["apps.txt", "common_site_config.json"]
                if spec["current_site"]:
                    listing.append("currentsite.txt")
                listing.extend(spec["sites"].keys())
                return (0, "\n".join(listing).encode())
            if cmd == f"cat {bench}/sites/common_site_config.json":
                if spec["common_site_config"] is None:
                    return (1, b"")
                return (0, json.dumps(spec["common_site_config"]).encode())
            if cmd == f"cat {bench}/sites/currentsite.txt":
                if spec["current_site"] is None:
                    return (1, b"")
                return (0, spec["current_site"].encode())
            for site in spec["sites"]:
                if cmd == f"cat {bench}/sites/{site}/site_config.json":
                    if site in spec["site_configs"]:
                        return (0, json.dumps(spec["site_configs"][site]).encode())
                    return (1, b"")
                if cmd == f"bench --site {site} list-apps" and workdir == bench:
                    return (0, "\n".join(spec["sites"][site]).encode())
        return (1, b"")


# The bench dicts a full inspect GATHERS (and persists), in _gather_bench_data's
# exact insertion order: path, sites, available_apps, current_site, label,
# common_site_config (the optional keys only when present).
GATHERED_A = {
    "path": BENCH_A,
    "sites": [
        {
            "name": "a.localhost",
            "installed_apps": ["frappe 15.0.0 version-15", "erpnext 15.0.0 version-15"],
            "site_config": {"db_name": "adb"},
        }
    ],
    "available_apps": ["erpnext", "frappe"],
    "current_site": "a.localhost",
    "common_site_config": {"default_site": "a.localhost"},
}
GATHERED_B = {
    "path": BENCH_B,
    "sites": [{"name": "b.localhost", "installed_apps": ["frappe 15.0.0 version-15"]}],
    "available_apps": ["frappe"],
    "label": "staging",
}

# The same benches as the CACHE READER returns them (db_utils.get_cached_project_data
# insertion order): path, sites, available_apps, label, current_site,
# common_site_config. Note label and current_site SWAP relative to the gathered
# shape - the --json bytes differ between a T3 run and a cache-served run, and both
# orders are pinned here.
CACHED_A = {
    "path": BENCH_A,
    "sites": GATHERED_A["sites"],
    "available_apps": ["erpnext", "frappe"],
    "current_site": "a.localhost",
    "common_site_config": {"default_site": "a.localhost"},
}
CACHED_B = {
    "path": BENCH_B,
    "sites": GATHERED_B["sites"],
    "available_apps": ["frappe"],
    "label": "staging",
}


def _expected_json(benches: list[dict]) -> str:
    return (
        json.dumps(
            {
                "project_name": "proj",
                "bench_instances": [{"index": i, **b} for i, b in enumerate(benches)],
            },
            indent=2,
        )
        + "\n"
    )


@pytest.fixture()
def wired(monkeypatch):
    """An in-memory cache plus the surviving-seam patches (see module docstring)."""
    store: dict[str, dict] = {}
    writes: list = []

    def fake_get(name):
        return store.get(name)

    def fake_cache(name, benches):
        writes.append([dict(b) for b in benches])
        store[name] = {"project_name": name, "bench_instances": benches, "last_updated": "now"}

    monkeypatch.setattr(db_utils, "get_cached_project_data", fake_get)
    monkeypatch.setattr(db_utils, "cache_project_data", fake_cache)
    monkeypatch.setattr(config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(config_utils, "load_config", lambda: {})
    # Neutralize the @handle_docker_errors preflight (no real Docker needed).
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("docker.from_env", lambda: _StubDockerClient())

    def install(container):
        # The docker boundary, patched at the layer that survives the migration
        # (core.docker) AND at the command module's own binding while it exists.
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [container])
        monkeypatch.setattr(
            inspect_mod, "get_project_containers", lambda name: [container], raising=False
        )

    return store, writes, install


def _run_inspect(**overrides):
    kwargs = dict(
        project_name="proj",
        bench=None,
        verbose=False,
        json_output=True,
        update=False,
        no_refresh=False,
        interactive=False,
        yes=False,
        prompt_to_start=True,
    )
    kwargs.update(overrides)
    inspect_mod.inspect(**kwargs)


def _seed_cache(store):
    store["proj"] = {
        "project_name": "proj",
        "bench_instances": [json.loads(json.dumps(CACHED_A)), json.loads(json.dumps(CACHED_B))],
        "last_updated": "earlier",
    }


class TestJsonBytesAreByteIdentical:
    """Task 1.3's anchors: the exact --json bytes per tier, and the cache shape."""

    def test_t3_cache_miss_full_inspect_json_bytes_and_cache_shape(self, wired, capsys):
        store, writes, install = wired
        install(MultiBenchContainer())

        _run_inspect()

        assert capsys.readouterr().out == _expected_json([GATHERED_A, GATHERED_B])
        # The persisted cache dicts: exact content AND key order.
        assert writes == [[GATHERED_A, GATHERED_B]]
        persisted = store["proj"]["bench_instances"]
        assert [list(b.keys()) for b in persisted] == [
            ["path", "sites", "available_apps", "current_site", "common_site_config"],
            ["path", "sites", "available_apps", "label"],
        ]
        assert [list(s.keys()) for b in persisted for s in b["sites"]] == [
            ["name", "installed_apps", "site_config"],
            ["name", "installed_apps"],
        ]

    def test_t1_no_refresh_serves_cache_verbatim(self, wired, capsys):
        store, writes, install = wired
        _seed_cache(store)
        container = MultiBenchContainer()
        install(container)

        _run_inspect(no_refresh=True)

        assert capsys.readouterr().out == _expected_json([CACHED_A, CACHED_B])
        assert container.calls == []  # zero container work on the fast path
        assert writes == []

    def test_t2_no_drift_serves_cache_unchanged(self, wired, capsys):
        store, writes, install = wired
        _seed_cache(store)
        container = MultiBenchContainer()
        install(container)

        _run_inspect()

        assert capsys.readouterr().out == _expected_json([CACHED_A, CACHED_B])
        assert writes == []  # T2 never writes
        # The cheap pass genuinely ran (ls over apps/), but never the expensive
        # discovery or the deep per-site listing.
        assert f"ls -1 {BENCH_A}/apps" in container.calls
        assert not any(str(c).startswith("find ") for c in container.calls)
        assert not any("list-apps" in str(c) for c in container.calls)

    def test_forced_update_json_bytes_match_the_gathered_shape(self, wired, capsys):
        store, writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())

        _run_inspect(update=True)

        assert capsys.readouterr().out == _expected_json([GATHERED_A, GATHERED_B])
        assert writes == [[GATHERED_A, GATHERED_B]]


class TestBenchSelector:
    """``--bench <index|label>`` narrows the human ``--json`` output to one bench,
    on every tier, without disturbing the no-flag (every-bench) path.
    """

    def test_index_narrows_t1_no_refresh_output(self, wired, capsys):
        store, _writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())

        _run_inspect(no_refresh=True, bench="1")

        assert capsys.readouterr().out == _expected_json([CACHED_B])

    def test_label_narrows_t3_full_inspect_output_but_still_caches_every_bench(self, wired, capsys):
        store, writes, install = wired
        install(MultiBenchContainer())

        _run_inspect(update=True, bench="staging")

        # Bench B's TRUE durable index is 1 (its position among all discovered
        # benches), which narrowing must preserve rather than reporting 0 (its
        # position within the now-narrowed one-item list).
        assert (
            capsys.readouterr().out
            == json.dumps(
                {"project_name": "proj", "bench_instances": [{"index": 1, **GATHERED_B}]}, indent=2
            )
            + "\n"
        )
        # The bonus, not the requirement: every bench is still discovered and
        # persisted regardless of the selector - only the emitted report narrows.
        assert writes == [[GATHERED_A, GATHERED_B]]

    def test_unknown_bench_exits_non_zero(self, wired, capsys):
        store, _writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())

        with pytest.raises(typer.Exit) as excinfo:
            _run_inspect(no_refresh=True, bench="nope")

        assert excinfo.value.exit_code != 0
        assert "nope" in capsys.readouterr().err

    def test_no_selector_reports_every_bench_unchanged(self, wired, capsys):
        store, _writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())

        _run_inspect(no_refresh=True)

        assert capsys.readouterr().out == _expected_json([CACHED_A, CACHED_B])


class TestShowAppsRemoved:
    """--show-apps was declared-and-dead and is REMOVED (captain decision
    2026-07-16): Typer rejects it as an unknown option, and the tree output is
    what the flag always produced anyway (apps were always shown)."""

    def test_show_apps_is_rejected_as_unknown_option(self):
        from typer.testing import CliRunner

        from caffeinated_whale_cli.main import app

        result = CliRunner().invoke(app, ["inspect", "proj", "--show-apps"])
        assert result.exit_code == 2
        # Typer force-enables rich's terminal styling under GITHUB_ACTIONS, which
        # splits "--show-apps" across several ANSI-styled spans; unstyle first so
        # the substring check is stable both locally and in CI.
        assert "--show-apps" in click.unstyle(result.output)

    def test_short_a_is_rejected_as_unknown_option(self):
        from typer.testing import CliRunner

        from caffeinated_whale_cli.main import app

        result = CliRunner().invoke(app, ["inspect", "proj", "-a"])
        assert result.exit_code == 2


class TestTreeDefaultMarker:
    """ "(default)" resolves common_site_config.default_site first, current_site
    fallback; a bench with neither shows no default marker."""

    def test_default_marker_next_to_resolved_site_only(self, wired, capsys):
        store, _writes, install = wired
        _seed_cache(store)
        # Bench A's default comes from common_site_config; strip it so the
        # current_site FALLBACK is what resolves.
        del store["proj"]["bench_instances"][0]["common_site_config"]
        install(MultiBenchContainer())

        _run_inspect(no_refresh=True, json_output=False)

        out = capsys.readouterr().out
        assert "a.localhost (default)" in out
        assert "b.localhost (default)" not in out


class TestStoppedProjectContract:
    """The run-state contract through surfaces that survive the migration."""

    def test_cache_hit_with_stopped_project_serves_cache_and_starts_nothing_even_with_yes(
        self, wired, capsys
    ):
        store, writes, install = wired
        _seed_cache(store)
        container = MultiBenchContainer(status="exited")
        install(container)

        _run_inspect(yes=True)  # --yes must NOT wake a stopped project on a cache hit

        assert capsys.readouterr().out == _expected_json([CACHED_A, CACHED_B])
        assert container.start_calls == 0
        assert container.calls == []  # not a single exec against the stopped project
        assert writes == []

    def test_t3_stopped_with_yes_auto_starts_and_inspects(self, wired, monkeypatch, capsys):
        from caffeinated_whale_cli.commands import utils as cmd_utils

        store, writes, install = wired
        container = MultiBenchContainer(status="exited")
        install(container)
        starts: list[str] = []

        def fake_start(project_name, verbose=False):
            starts.append(project_name)
            container.start()

        monkeypatch.setattr(cmd_utils, "_start_containers_for_command", fake_start)

        _run_inspect(yes=True)  # cache miss -> T3 -> --yes auto-starts

        assert starts == ["proj"]
        assert capsys.readouterr().out == _expected_json([GATHERED_A, GATHERED_B])
        assert writes == [[GATHERED_A, GATHERED_B]]

    def test_t3_stopped_non_tty_without_yes_refuses_exit_1(self, wired, monkeypatch, capsys):
        store, writes, install = wired
        container = MultiBenchContainer(status="exited")
        install(container)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(typer.Exit) as excinfo:
            _run_inspect()  # cache miss -> T3 against a stopped project, no TTY, no --yes

        assert excinfo.value.exit_code == 1
        assert container.start_calls == 0
        assert writes == []
        err = capsys.readouterr().err
        assert "not running" in err


def _stub_questionary(monkeypatch, answers):
    """Feed the ``-i`` prompt loop a queued list of answers (None = Ctrl+C)."""
    queue = list(answers)

    class _Q:
        def ask(self):
            return queue.pop(0) if queue else ""

    monkeypatch.setattr(inspect_mod.questionary, "text", lambda *a, **k: _Q())


class TestInteractiveLabeling:
    """`inspect -i` now PROMPTS (frontend) then routes the collected answers through
    ``core.label.set_labels`` - the validate/uniqueness/marker/cache rule has one
    owner. These pin the routing and the preserved per-bench outcome rendering."""

    def test_accepted_label_persists_and_blank_keeps_existing(self, wired, monkeypatch, capsys):
        store, writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())
        # Bench 0 gets "web"; bench 1 answered blank keeps its "staging" label.
        _stub_questionary(monkeypatch, ["web", ""])
        monkeypatch.setattr(
            inspect_mod.core_label.bench_labels, "write_label_marker", lambda *a: True
        )

        _run_inspect(interactive=True, no_refresh=True)

        out = json.loads(capsys.readouterr().out)
        labels = {b["index"]: b.get("label") for b in out["bench_instances"]}
        assert labels == {0: "web", 1: "staging"}
        assert writes and writes[-1][0]["label"] == "web"

    def test_duplicate_answer_is_rejected_with_todays_wording(self, wired, monkeypatch, capsys):
        store, writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())
        # Bench 0 tries to reuse bench 1's existing "staging" label -> rejected.
        _stub_questionary(monkeypatch, ["staging", ""])

        _run_inspect(interactive=True, no_refresh=True)

        captured = capsys.readouterr()
        normalized = " ".join(captured.err.split())  # rich soft-wraps the line
        assert "already used" in normalized
        assert "Keeping the previous label" in normalized
        out = json.loads(captured.out)
        assert out["bench_instances"][0].get("label") is None

    def test_stopped_project_warns_marker_skipped_but_saves_to_cache(
        self, wired, monkeypatch, capsys
    ):
        store, writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer(status="exited"))
        _stub_questionary(monkeypatch, ["web", ""])

        _run_inspect(interactive=True, no_refresh=True)

        captured = capsys.readouterr()
        assert "cache only" in captured.err
        out = json.loads(captured.out)
        assert out["bench_instances"][0]["label"] == "web"

    def test_ctrl_c_on_first_prompt_labels_nothing(self, wired, monkeypatch, capsys):
        store, writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer())
        _stub_questionary(monkeypatch, [None])  # Ctrl+C immediately

        _run_inspect(interactive=True, no_refresh=True)

        out = json.loads(capsys.readouterr().out)
        assert out["bench_instances"][0].get("label") is None
        assert out["bench_instances"][1].get("label") == "staging"

    def test_marker_skipped_warning_shown_before_prompt_loop_not_after(
        self, wired, monkeypatch, capsys
    ):
        """The cache-only warning must land BEFORE the user answers any prompt (the
        pre-migration ordering), not only after ``set_labels`` returns - a user
        against a stopped project should learn markers won't be written before
        typing anything, not after answering every prompt."""
        store, writes, install = wired
        _seed_cache(store)
        install(MultiBenchContainer(status="exited"))
        _stub_questionary(monkeypatch, ["web", ""])

        order: list[str] = []
        real_text = inspect_mod.questionary.text

        def spying_text(*a, **k):
            order.append("prompt")
            return real_text(*a, **k)

        monkeypatch.setattr(inspect_mod.questionary, "text", spying_text)

        real_print = inspect_mod.console_err.print

        def spying_print(msg, *a, **k):
            if "cache only" in str(msg):
                order.append("warning")
            return real_print(msg, *a, **k)

        monkeypatch.setattr(inspect_mod.console_err, "print", spying_print)

        _run_inspect(interactive=True, no_refresh=True)

        assert order[0] == "warning"
        assert order.count("warning") == 1  # not echoed again after set_labels
        assert order.count("prompt") == 2
