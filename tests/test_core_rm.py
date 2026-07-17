"""Core-contract tests for ``core.remove`` (batch 12: migrate-rm-core).

The deep behavioral coverage (the C1 backup gate, the multi-bench fan-out, the
streamed copy, the H5 refusals, the not-running backstop) lives in
``tests/test_rm_safety.py``, ``tests/test_rm_truth.py``, and
``tests/test_rm_stopped.py``, re-pointed at ``core.rm`` with their subject. This
file adds the CORE-CONTRACT assertions the migration is about: the typed error
taxonomy, the ``Result`` status mapping, plain-data DTOs, core silence, and the
deliberately-absent ``axi rm`` verb.
"""

import dataclasses
from unittest.mock import MagicMock

import pytest

from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"


def _make_container(status="running", name="proj-frappe-1"):
    c = MagicMock()
    c.name = name
    c.status = status
    c.labels = {"com.docker.compose.service": "frappe"}
    # Every archive/backup probe fails benignly (no sites) so removal proceeds
    # on the --no-backup path without needing a full backup harness.
    c.exec_run.return_value = (1, b"")
    return c


def _make_volume(name):
    v = MagicMock()
    v.name = name
    return v


@pytest.fixture()
def cwcli_home(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(core_rm, "PROJECTS_DIR", projects_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_project_dir(projects_dir, name):
    project_dir = projects_dir / name
    (project_dir / "conf").mkdir(parents=True)
    (project_dir / "conf" / "docker-compose.yml").write_text("# fake\n")
    return project_dir


def _wire(monkeypatch, containers, volumes):
    monkeypatch.setattr(core_rm, "get_project_containers", lambda name: list(containers))
    monkeypatch.setattr(core_rm, "get_project_volumes", lambda name: list(volumes))
    monkeypatch.setattr(core_rm.db_utils, "clear_cache_for_project", lambda name: None)
    monkeypatch.setattr(core_rm.db_utils, "get_cached_project_data", lambda name: None)


class TestTypedErrors:
    """Hard failures raise a typed ``CwcliError`` rather than returning a dict."""

    def test_invalid_name_raises_usage(self, cwcli_home, monkeypatch):
        called = MagicMock()
        monkeypatch.setattr(core_rm, "get_project_containers", called)
        with pytest.raises(CwcliError) as exc:
            core_rm.remove("..", remove_volumes=True, no_backup=True)
        assert exc.value.kind is ErrorKind.USAGE
        called.assert_not_called()  # refused before touching Docker

    def test_docker_unreachable_raises_docker(self, cwcli_home, monkeypatch):
        monkeypatch.setattr(core_rm, "get_project_containers", lambda name: None)
        with pytest.raises(CwcliError) as exc:
            core_rm.remove("proj", remove_volumes=True, no_backup=True)
        assert exc.value.kind is ErrorKind.DOCKER


class TestStatusMapping:
    """The envelope status: OK on a clean removal, WARNING otherwise."""

    def test_clean_removal_is_ok(self, cwcli_home, monkeypatch):
        _make_project_dir(cwcli_home / "projects", "proj")
        container = _make_container()
        _wire(monkeypatch, [container], [_make_volume("proj_sites")])

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        assert result.status is Status.OK
        assert result.data.found is True
        assert result.data.failures == []
        assert result.data.containers_removed == 1
        assert result.data.volumes_removed == 1
        assert result.data.dir_removed is True

    def test_not_found_is_warning_found_false(self, cwcli_home, monkeypatch):
        _wire(monkeypatch, [], [])  # no containers, no volumes, no dir
        result = core_rm.remove("ghost", remove_volumes=True, no_backup=True)
        assert result.status is Status.WARNING
        assert result.data.found is False
        assert result.data.failures == []

    def test_partial_failure_is_warning_with_failures(self, cwcli_home, monkeypatch):
        _make_project_dir(cwcli_home / "projects", "proj")
        container = _make_container()
        container.remove = MagicMock(side_effect=RuntimeError("daemon error"))
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [container], volumes)

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        assert result.status is Status.WARNING
        assert result.data.failures  # non-empty -> the frontend exits 1
        # The LATE gate spared the data because a container removal failed.
        volumes[0].remove.assert_not_called()


class TestPlainDataDTO:
    """No live Docker object crosses the return boundary; ``asdict`` is plain."""

    def test_outcome_asdict_is_plain_data(self, cwcli_home, monkeypatch):
        _make_project_dir(cwcli_home / "projects", "proj")
        _wire(monkeypatch, [_make_container()], [_make_volume("proj_sites")])

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)
        d = dataclasses.asdict(result.data)

        allowed = (str, bool, int, list)
        for key, value in d.items():
            assert isinstance(value, allowed), f"{key} is {type(value)!r}, not plain data"
        assert all(isinstance(f, str) for f in d["failures"])


class TestCoreSilence:
    """``core.remove`` prints NOTHING; all narration rides ``on_event``."""

    def test_no_event_callback_is_silent(self, cwcli_home, monkeypatch, capsys):
        _make_project_dir(cwcli_home / "projects", "proj")
        _wire(monkeypatch, [_make_container()], [_make_volume("proj_sites")])

        core_rm.remove("proj", remove_volumes=True, no_backup=True)  # no on_event

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_events_are_delivered_when_a_callback_is_given(self, cwcli_home, monkeypatch):
        _make_project_dir(cwcli_home / "projects", "proj")
        _wire(monkeypatch, [_make_container()], [_make_volume("proj_sites")])

        events = []
        core_rm.remove("proj", remove_volumes=True, no_backup=True, on_event=events.append)

        # At least a spinner step and the removal notices flowed through the callback.
        assert any(isinstance(e, core_rm.RmStep) for e in events)
        assert any(isinstance(e, core_rm.RmNotice) for e in events)


class TestNoAxiRmVerb:
    """There is deliberately NO ``axi rm`` verb this batch, and it is DEFERRED
    (design Decision 5 of ``migrate-rm-core``): whether an agent may delete an
    instance's data (named volumes, the whole bench) is a product decision the
    captain owns on its own evidence - the ``axi apps install``/``uninstall`` and
    ``axi init`` precedent. The fail-closed backup gate protects against ACCIDENT,
    not against an agent that deliberately means to delete. This test keeps the
    deferral legible so "deferred" can never read as "forgotten"; the
    single-function core shape makes the verb thin whenever it is decided (it
    would wire the destructive consent separately, the ``apps uninstall``
    precedent)."""

    def test_axi_registry_has_no_rm_command(self):
        from caffeinated_whale_cli.commands import axi as axi_mod

        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "rm" not in registered
        for group in axi_mod.app.registered_groups:
            sub = {c.name for c in group.typer_instance.registered_commands}
            assert "rm" not in sub


class TestCorePurity:
    """The import ban in ``tests/test_core_envelope.py`` covers ``core/rm.py``;
    this is a direct smoke check that it holds no UI imports."""

    def test_core_rm_imports_no_ui(self):
        import inspect

        source = inspect.getsource(core_rm)
        assert "import rich" not in source
        assert "import questionary" not in source
        assert "import typer" not in source
