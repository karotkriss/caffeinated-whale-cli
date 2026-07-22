"""Core-contract tests for ``core.remove`` (migrate-rm-core).

The deep behavioral coverage (the C1 backup gate, the multi-bench fan-out, the
streamed copy, the H5 refusals, the not-running backstop) lives in
``tests/test_rm_safety.py``, ``tests/test_rm_truth.py``, and
``tests/test_rm_stopped.py``, re-pointed at ``core.rm`` with their subject. This
file adds the CORE-CONTRACT assertions the migration is about: the typed error
taxonomy, the ``Result`` status mapping, plain-data DTOs, core silence, and the
shipped ``axi rm`` verb's decided properties (consent-only ``--yes``, no
auto-start, no ``--no-backup``).
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


def _make_network(name):
    n = MagicMock()
    n.name = name
    return n


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


def _wire(monkeypatch, containers, volumes, networks=()):
    monkeypatch.setattr(core_rm, "get_project_containers", lambda name: list(containers))
    monkeypatch.setattr(core_rm, "get_project_volumes", lambda name: list(volumes))
    monkeypatch.setattr(core_rm, "get_project_networks", lambda name: list(networks))
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


class TestAxiRmVerbShipped:
    """``axi rm`` SHIPPED (captain-approved 2026-07-21), reversing the deferral
    this class used to assert.

    The deferral (``migrate-rm-core``'s design) held that whether an
    agent may delete an instance's data is a product decision the captain owns on
    its own evidence, because the fail-closed backup gate protects against ACCIDENT
    and not against an agent that deliberately means to delete. That reasoning was
    about WHETHER the capability should exist, and only that: the captain answered
    it, and NO safety property was waived with it. The verb is a thin frontend over
    the UNCHANGED ``core.remove``, so the gate, the verified copy-out and the
    failures-driven exit code are the same code the human verb runs.

    Two properties below are the ones this surface DECIDED rather than inherited,
    because the human verb owns them in its frontend, and they are asserted here so
    a later change cannot quietly widen them:

    - ``--yes`` grants consent ONLY. The human verb's ``--yes`` also auto-starts a
      stopped project for its backup; fusing those is the interface convenience
      ``core.remove`` was migrated to keep OUT of the core, and re-creating it on
      the agent surface would mean an agent that asked to delete an instance had
      thereby started one.
    - There is NO ``--no-backup``. It disables the C1 gate, and on this surface a
      bypass flag has no named beneficiary (the ``axi apps install`` ``--force`` and
      ``axi migrate`` ``--skip-maintenance`` rulings). The human verb is the hatch.
    """

    def test_axi_registry_has_the_rm_command(self):
        from caffeinated_whale_cli.commands import axi as axi_mod

        assert "rm" in {c.name for c in axi_mod.app.registered_commands}

    def test_consent_is_required_and_never_implies_auto_start(self):
        """``--yes`` exists and means consent; nothing on the verb starts a container."""
        import inspect as _inspect

        from caffeinated_whale_cli.commands import axi as axi_mod

        params = _inspect.signature(axi_mod.axi_rm).parameters
        assert "yes" in params
        # The consent flag defaults to False, so omitting it can never be read as
        # consent by a caller that simply did not pass it.
        assert params["yes"].default.default is False
        assert "auto_start" not in params

    def test_there_is_no_no_backup_bypass_flag(self):
        """The C1 gate has no agent-surface off switch. Widening this must edit this test."""
        import inspect as _inspect

        from caffeinated_whale_cli.commands import axi as axi_mod

        params = _inspect.signature(axi_mod.axi_rm).parameters
        assert "no_backup" not in params
        source = _inspect.getsource(axi_mod.axi_rm)
        assert "no_backup=False" in source  # the core is called with the gate ON


class TestNetworkRemoval:
    """``core.remove`` cleans up the project's OWN compose network too - the
    leak this class exists to close (a network outlived containers/volumes/dir
    on every prior removal, silently consuming Docker's finite address pool)."""

    def test_network_is_removed_and_reported(self, cwcli_home, monkeypatch):
        _make_project_dir(cwcli_home / "projects", "proj")
        network = _make_network("proj_default")
        _wire(
            monkeypatch,
            [_make_container()],
            [_make_volume("proj_sites")],
            networks=[network],
        )

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        network.remove.assert_called_once_with()
        assert result.data.network_removed is True
        assert result.data.failures == []

    def test_network_only_orphan_is_removed(self, cwcli_home, monkeypatch):
        network = _make_network("proj_default")
        cache_clear = MagicMock()
        events = []
        _wire(monkeypatch, [], [], networks=[network])
        monkeypatch.setattr(core_rm.db_utils, "clear_cache_for_project", cache_clear)

        result = core_rm.remove("proj", on_event=events.append)

        network.remove.assert_called_once_with()
        cache_clear.assert_called_once_with("proj")
        assert result.data.found is True
        assert result.data.orphan is True
        assert result.data.containers_removed == 0
        assert result.data.volumes_removed == 0
        assert result.data.dir_removed is False
        assert result.data.network_removed is True
        assert result.data.failures == []
        assert any(
            isinstance(event, core_rm.RmWarning)
            and "no named volumes were found" in event.text.lower()
            and "no database backup was needed" in event.text.lower()
            for event in events
        )

    def test_orphan_with_named_volumes_still_requires_backup(self, cwcli_home, monkeypatch):
        volume = _make_volume("proj_sites")
        network = _make_network("proj_default")
        _wire(monkeypatch, [], [volume], networks=[network])

        result = core_rm.remove("proj")

        assert result.data.found is True
        assert result.data.orphan is True
        assert result.data.backup_ok is False
        assert result.data.network_removed is False
        assert result.data.failures
        volume.remove.assert_not_called()
        network.remove.assert_not_called()

    def test_orphan_with_unknown_volumes_still_requires_backup(self, cwcli_home, monkeypatch):
        network = _make_network("proj_default")
        _wire(monkeypatch, [], [], networks=[network])
        monkeypatch.setattr(core_rm, "get_project_volumes", lambda name: None)

        result = core_rm.remove("proj")

        assert result.data.found is True
        assert result.data.orphan is True
        assert result.data.backup_ok is False
        assert result.data.network_removed is False
        assert result.data.failures
        network.remove.assert_not_called()

    def test_network_removed_regardless_of_no_volumes(self, cwcli_home, monkeypatch):
        """The network holds no user data, so --no-volumes must not spare it."""
        _make_project_dir(cwcli_home / "projects", "proj")
        network = _make_network("proj_default")
        _wire(
            monkeypatch,
            [_make_container()],
            [_make_volume("proj_sites")],
            networks=[network],
        )

        result = core_rm.remove("proj", remove_volumes=False, no_backup=True)

        network.remove.assert_called_once_with()
        assert result.data.network_removed is True

    def test_network_removal_failure_is_reported_never_forced(self, cwcli_home, monkeypatch):
        """A network with an endpoint attached from outside this project cannot
        be removed; the failure is reported (non-zero exit) rather than the
        network being disconnected/forced or the failure silently swallowed."""
        _make_project_dir(cwcli_home / "projects", "proj")
        network = _make_network("proj_default")
        network.remove.side_effect = RuntimeError("network has active endpoints")
        volumes = [_make_volume("proj_sites")]
        _wire(monkeypatch, [_make_container()], volumes, networks=[network])

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        network.remove.assert_called_once_with()
        assert result.data.network_removed is False
        assert any("network" in f for f in result.data.failures)
        assert result.status is Status.WARNING
        # Never forced: no disconnect call was ever made against the network.
        network.disconnect.assert_not_called()
        # A failed network removal does not block the volume/dir cleanup already
        # in flight - it is orthogonal to the C1 data-safety gate.
        volumes[0].remove.assert_called_once_with(force=True)
        assert result.data.dir_removed is True

    def test_network_enumeration_failure_is_reported(self, cwcli_home, monkeypatch):
        """None from get_project_networks is a Docker error, distinct from 'no
        network' - it must be reported, not silently treated as clean."""
        _make_project_dir(cwcli_home / "projects", "proj")
        _wire(monkeypatch, [_make_container()], [_make_volume("proj_sites")])
        monkeypatch.setattr(core_rm, "get_project_networks", lambda name: None)

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        assert result.data.network_removed is False
        assert any("enumerate" in f and "network" in f for f in result.data.failures)

    def test_network_enumeration_failure_is_not_reported_as_absent(
        self, cwcli_home, monkeypatch
    ):
        _wire(monkeypatch, [], [])
        monkeypatch.setattr(core_rm, "get_project_networks", lambda name: None)

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        assert result.data.found is True
        assert result.data.orphan is True
        assert result.data.network_removed is False
        assert any("enumerate" in f and "network" in f for f in result.data.failures)

    def test_volume_enumeration_failure_is_not_reported_as_absent(
        self, cwcli_home, monkeypatch
    ):
        _wire(monkeypatch, [], [], networks=[])
        monkeypatch.setattr(core_rm, "get_project_volumes", lambda name: None)

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        assert result.data.found is True
        assert result.data.orphan is True
        assert any("enumerate" in f and "volume" in f for f in result.data.failures)

    def test_no_network_found_is_not_a_failure(self, cwcli_home, monkeypatch):
        _make_project_dir(cwcli_home / "projects", "proj")
        _wire(monkeypatch, [_make_container()], [_make_volume("proj_sites")], networks=[])

        result = core_rm.remove("proj", remove_volumes=True, no_backup=True)

        assert result.data.network_removed is False
        assert result.data.failures == []
        assert result.status is Status.OK


class TestCorePurity:
    """The import ban in ``tests/test_core_envelope.py`` covers ``core/rm.py``;
    this is a direct smoke check that it holds no UI imports."""

    def test_core_rm_imports_no_ui(self):
        import inspect

        source = inspect.getsource(core_rm)
        assert "import rich" not in source
        assert "import questionary" not in source
        assert "import typer" not in source
