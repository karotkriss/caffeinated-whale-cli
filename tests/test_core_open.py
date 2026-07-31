"""``core.open_plan`` - the resolve-everything-launch-nothing slice under test.

Pins the contract from ``openspec/changes/migrate-open-core/design.md``: the
declarative four-string :class:`LaunchTarget` (container NAME, never an argv),
all three ``NEEDS_CHOICE`` kinds (``confirm_start``, ``select_bench``, the new
``select_editor``), the fallback populate's abort/degrade matrix (a hard
``CwcliError`` PROPAGATES; a non-``CwcliError`` exception and a
succeeds-but-still-nothing populate degrade to the default path with a
warning), the verbatim ``--app`` pass (match by PATH, in-memory refresh,
degrade-on-error), and that the core prints nothing at all.
"""

from dataclasses import asdict

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core import open as core_open
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"
BENCH_B = "/home/frappe/bench-b"
DEFAULT = resolvers.DEFAULT_BENCH_PATH


class FakeContainer:
    def __init__(self, status="running", name="proj-frappe-1"):
        self.status = status
        self.name = name
        self.labels = {"com.docker.compose.service": "frappe"}

    def reload(self):
        pass


@pytest.fixture()
def running(monkeypatch):
    """A running frappe container plus a single cached bench; the OK baseline."""
    container = FakeContainer()
    monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: container)
    monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [{"path": BENCH}])
    monkeypatch.setattr(
        core_inspect, "partial_refresh", lambda c, benches, on_event=None: (benches, False)
    )
    monkeypatch.setattr("shutil.which", lambda _name: None)
    return container


class TestPlanOk:
    def test_launch_target_is_declarative_and_serializable(self, running):
        result = core_open.open_plan("proj", editor="docker")

        assert result.status is Status.OK
        target = result.data
        assert target == core_open.LaunchTarget(
            project="proj",
            container_name="proj-frappe-1",  # a NAME, not an ID
            working_dir=BENCH,
            editor="docker",
        )
        # Serializable throughout: plain nested data, no live Docker object.
        assert asdict(target) == {
            "project": "proj",
            "container_name": "proj-frappe-1",
            "working_dir": BENCH,
            "editor": "docker",
            # The bench's real host URL, None here because this fake container
            # publishes no ports - a hint that cannot be read is omitted, not guessed.
            "web_url": None,
        }

    def test_core_prints_nothing_at_all(self, running, capsys):
        events = []
        core_open.open_plan("proj", editor="docker", on_event=events.append)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        # The diagnostics ride typed events instead.
        assert any(isinstance(e, core_open.OpenTrace) for e in events)


class TestConfirmStartFork:
    def test_stopped_container_returns_confirm_start(self, running):
        running.status = "exited"

        result = core_open.open_plan("proj", editor="docker")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice is not None
        assert result.choice.kind == "confirm_start"
        assert result.choice.param == "auto_start"

    def test_auto_start_requested_proceeds_to_a_plan(self, running):
        # The frontend performs the actual start; the plan proceeds as run_plan does.
        running.status = "exited"

        result = core_open.open_plan("proj", editor="docker", auto_start=True)

        assert result.status is Status.OK


class TestSelectBenchFork:
    def test_multi_bench_no_selector_returns_select_bench(self, running, monkeypatch):
        monkeypatch.setattr(
            resolvers, "cached_benches", lambda _p: [{"path": BENCH}, {"path": BENCH_B}]
        )

        result = core_open.open_plan("proj", editor="docker")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice is not None
        assert result.choice.kind == "select_bench"
        assert [o["label"] for o in result.choice.options] == [BENCH, BENCH_B]

    def test_bench_and_path_together_raise_usage(self, running):
        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", bench="0", bench_path=BENCH, editor="docker")
        assert excinfo.value.kind is ErrorKind.USAGE


class TestFallbackPopulate:
    """The abort/degrade matrix of design Decision 4."""

    def _no_cache(self, monkeypatch):
        container = FakeContainer()
        monkeypatch.setattr(core_docker, "get_frappe_container", lambda _p: container)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        store: dict = {"benches": []}
        monkeypatch.setattr(resolvers, "cached_benches", lambda _p: list(store["benches"]))
        return store

    def test_populate_call_shape_and_success(self, monkeypatch):
        store = self._no_cache(monkeypatch)
        calls = []

        def fake_inspect(project_name, **kwargs):
            calls.append((project_name, kwargs))
            store["benches"] = [{"path": BENCH_B}]

        monkeypatch.setattr(core_inspect, "inspect", fake_inspect)
        events = []

        result = core_open.open_plan(
            "proj", editor="docker", auto_start=True, on_event=events.append
        )

        # The populate announced itself unconditionally, then re-resolved.
        assert any(
            isinstance(e, core_open.OpenNotice)
            and e.text == "No cached bench path found. Running inspect..."
            for e in events
        )
        assert calls == [("proj", {"refresh": "auto", "auto_start": True, "offer_choice": False})]
        assert result.status is Status.OK
        assert result.data.working_dir == BENCH_B
        # The freshly-populated resolve is not the default-path degrade.
        assert not any(w.code == "bench.default_used" for w in result.warnings)

    def test_freshly_populated_multi_bench_still_surfaces_select_bench(self, monkeypatch):
        store = self._no_cache(monkeypatch)

        def fake_inspect(project_name, **kwargs):
            store["benches"] = [{"path": BENCH}, {"path": BENCH_B}]

        monkeypatch.setattr(core_inspect, "inspect", fake_inspect)

        result = core_open.open_plan("proj", editor="docker")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"

    def test_hard_cwcli_error_propagates_never_the_default_path(self, monkeypatch):
        self._no_cache(monkeypatch)

        def raise_not_found(project_name, **kwargs):
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "bench.none_found",
                f"No Bench Instances found for project '{project_name}'.",
            )

        monkeypatch.setattr(core_inspect, "inspect", raise_not_found)

        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", editor="docker")
        assert excinfo.value.code == "bench.none_found"

    def test_non_cwcli_exception_degrades_to_default_with_warning(self, monkeypatch):
        self._no_cache(monkeypatch)

        def raise_runtime(project_name, **kwargs):
            raise RuntimeError("transient")

        monkeypatch.setattr(core_inspect, "inspect", raise_runtime)
        events = []

        result = core_open.open_plan("proj", editor="docker", on_event=events.append)

        assert result.status is Status.OK
        assert result.data.working_dir == DEFAULT
        assert [w.text for w in result.warnings if w.code == "bench.default_used"] == [
            f"Inspect failed. Using default bench path: {DEFAULT}"
        ]
        assert any(isinstance(e, core_open.OpenTrace) and "Inspect error" in e.text for e in events)

    def test_populate_succeeds_but_still_nothing_degrades_to_default(self, monkeypatch):
        self._no_cache(monkeypatch)
        monkeypatch.setattr(core_inspect, "inspect", lambda project_name, **kwargs: None)

        result = core_open.open_plan("proj", editor="docker")

        assert result.status is Status.OK
        assert result.data.working_dir == DEFAULT
        assert [w.text for w in result.warnings if w.code == "bench.default_used"] == [
            f"Could not detect bench path. Using default: {DEFAULT}"
        ]


class TestAppPass:
    """Design Decision 5: the ``--app`` pass, verbatim."""

    def test_no_cached_benches_is_not_found_naming_inspect(self, running, monkeypatch):
        monkeypatch.setattr(resolvers, "cached_benches", lambda _p: [])

        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", bench_path=BENCH, app="frappe", editor="docker")

        assert excinfo.value.kind is ErrorKind.NOT_FOUND
        assert "cwcli inspect proj" in excinfo.value.message

    def test_bench_matched_by_path_never_index_zero(self, running, monkeypatch):
        # appB lives only in bench-b; opening bench-b must validate against IT.
        monkeypatch.setattr(
            resolvers,
            "cached_benches",
            lambda _p: [
                {"path": BENCH, "available_apps": ["frappe", "appA"]},
                {"path": BENCH_B, "available_apps": ["frappe", "appB"]},
            ],
        )

        result = core_open.open_plan("proj", bench_path=BENCH_B, app="appB", editor="docker")
        assert result.data.working_dir == f"{BENCH_B}/apps/appB"

        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", bench_path=BENCH_B, app="appA", editor="docker")
        assert excinfo.value.code == "app.not_found"
        assert "frappe, appB" in (excinfo.value.hint or "")

    def test_unknown_bench_path_is_not_cached_error(self, running):
        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", bench_path="/nowhere", app="frappe", editor="docker")
        assert excinfo.value.code == "bench.not_cached"

    def test_in_memory_refresh_surfaces_fresh_app(self, running, monkeypatch):
        monkeypatch.setattr(
            resolvers, "cached_benches", lambda _p: [{"path": BENCH, "available_apps": ["frappe"]}]
        )
        monkeypatch.setattr(
            core_inspect,
            "partial_refresh",
            lambda c, benches, on_event=None: (
                [{"path": BENCH, "available_apps": ["frappe", "newapp"]}],
                True,
            ),
        )

        result = core_open.open_plan("proj", app="newapp", editor="docker")

        assert result.status is Status.OK
        assert result.data.working_dir == f"{BENCH}/apps/newapp"

    def test_refresh_failure_degrades_to_cached_list(self, running, monkeypatch):
        monkeypatch.setattr(
            resolvers, "cached_benches", lambda _p: [{"path": BENCH, "available_apps": ["frappe"]}]
        )

        def boom(c, benches, on_event=None):
            raise RuntimeError("exec failed")

        monkeypatch.setattr(core_inspect, "partial_refresh", boom)
        events = []

        result = core_open.open_plan("proj", app="frappe", editor="docker", on_event=events.append)

        assert result.status is Status.OK
        assert result.data.working_dir == f"{BENCH}/apps/frappe"
        assert any(
            isinstance(e, core_open.OpenTrace) and "App-list refresh skipped" in e.text
            for e in events
        )

    def test_empty_available_apps_is_not_found(self, running, monkeypatch):
        monkeypatch.setattr(
            resolvers, "cached_benches", lambda _p: [{"path": BENCH, "available_apps": []}]
        )

        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", app="frappe", editor="docker")
        assert excinfo.value.code == "apps.none_cached"


class TestEditorResolution:
    """Design Decision 3: detection in the core, the decision in the envelope."""

    def test_docker_is_always_valid(self, running):
        result = core_open.open_plan("proj", editor="docker")
        assert result.data.editor == "docker"

    def test_requested_editor_not_installed_raises_with_install_url(self, running):
        for editor, url in [
            ("code", "https://code.visualstudio.com/"),
            ("code-insiders", "https://code.visualstudio.com/insiders/"),
            ("cursor", "https://cursor.sh/"),
        ]:
            with pytest.raises(CwcliError) as excinfo:
                core_open.open_plan("proj", editor=editor)
            assert excinfo.value.kind is ErrorKind.NOT_FOUND
            assert excinfo.value.code == "editor.not_installed"
            assert url in (excinfo.value.hint or "")

    def test_unknown_editor_is_usage(self, running):
        with pytest.raises(CwcliError) as excinfo:
            core_open.open_plan("proj", editor="emacs")
        assert excinfo.value.kind is ErrorKind.USAGE

    def test_installed_editor_is_accepted(self, running, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        result = core_open.open_plan("proj", editor="cursor")
        assert result.data.editor == "cursor"

    def test_none_and_nothing_installed_picks_docker_silently(self, running):
        result = core_open.open_plan("proj")
        assert result.status is Status.OK
        assert result.data.editor == "docker"

    def test_none_with_editors_installed_returns_select_editor(self, running, monkeypatch):
        monkeypatch.setattr(
            "shutil.which",
            lambda name: f"/usr/bin/{name}" if name in ("code", "cursor") else None,
        )

        result = core_open.open_plan("proj")

        assert result.status is Status.NEEDS_CHOICE
        choice = result.choice
        assert choice.kind == "select_editor"
        assert choice.param == "editor"
        assert choice.prompt == "How would you like to open this instance?"
        # Installed editors plus Docker, with today's exact labels.
        assert [(o["value"], o["label"]) for o in choice.options] == [
            ("code", "VS Code - Open in development container"),
            ("cursor", "Cursor - Open in development container"),
            ("docker", "Docker - Execute interactive shell in container"),
        ]

    def test_editor_choice_arrives_after_bench_resolution(self, running, monkeypatch):
        # Error ordering: a multi-bench project with no selector surfaces
        # select_bench, not select_editor, even with editors installed.
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            resolvers, "cached_benches", lambda _p: [{"path": BENCH}, {"path": BENCH_B}]
        )

        result = core_open.open_plan("proj")

        assert result.choice.kind == "select_bench"


class TestNoAxiOpenVerb:
    """There is deliberately NO ``axi open`` verb (design Decision 6): not
    because ``LaunchTarget`` will not serialize - it is four strings and would -
    but because the interactive Docker branch consumes the process that owes
    ``axi`` its one-TOON-document contract, and the editor branches are
    meaningless to an agent with no desktop. This test keeps the absence a
    decision, not an oversight (the ``axi apps install``/``uninstall`` non-verb
    precedent)."""

    def test_axi_registry_has_no_open_command(self):
        from caffeinated_whale_cli.commands import axi as axi_mod

        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "open" not in registered
        # Nor under any axi subapp (e.g. `axi apps ...`).
        for group in axi_mod.app.registered_groups:
            sub = {c.name for c in group.typer_instance.registered_commands}
            assert "open" not in sub
