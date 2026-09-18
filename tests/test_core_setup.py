"""Unit coverage for ``core.setup`` - the privileged shared-mode provisioning.

Runs as fake-root (``os.geteuid`` monkeypatched to 0) against a temp state tree
and a relocatable marker, with group/user creation stubbed - so no real
``groupadd``/``useradd`` runs and no root is needed. ``chown`` to root is
best-effort-suppressed in :func:`core.setup._secure`, so the mode assertions
(chmod succeeds as the owner) hold on a non-root box while the E2E proves real
ownership.
"""

import os
import stat

import pytest

from caffeinated_whale_cli.core import setup as core_setup
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.utils import shared_home


@pytest.fixture
def fake_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)


@pytest.fixture
def stub_identity(monkeypatch):
    """Pretend the group + service user already exist; resolve gid to our own."""
    import grp

    monkeypatch.setattr(core_setup, "_group_exists", lambda name: True)
    monkeypatch.setattr(core_setup, "_user_exists", lambda name: True)

    class _FakeGroup:
        gr_gid = os.getgid()
        gr_mem: list = []

    monkeypatch.setattr(grp, "getgrnam", lambda name: _FakeGroup())


@pytest.fixture
def marker_at(tmp_path, monkeypatch):
    marker = tmp_path / "etc" / "shared.toml"
    marker.parent.mkdir()
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
    monkeypatch.delenv("CWCLI_HOME", raising=False)
    return marker


class TestProvision:
    def test_creates_setgid_tree_and_marker(self, fake_root, stub_identity, marker_at, tmp_path):
        state = tmp_path / "state"
        result = core_setup.provision(group="cwcli", state_dir=state)
        assert result.status is Status.OK
        assert result.data.state_dir == str(state)
        for sub in ("projects", "cache", "archive", "run"):
            d = state / sub
            assert d.is_dir()
            assert stat.S_IMODE(d.stat().st_mode) == 0o2770
        assert marker_at.exists()
        # The marker now turns shared mode on for subsequent resolution.
        assert shared_home.shared_mode() is True
        assert shared_home.state_dir() == state

    def test_is_idempotent(self, fake_root, stub_identity, marker_at, tmp_path):
        state = tmp_path / "state"
        first = core_setup.provision(state_dir=state)
        assert any(a.startswith("tree.created") for a in first.data.actions)
        second = core_setup.provision(state_dir=state)
        assert second.status is Status.OK
        # A re-run neither re-creates the tree nor crashes.
        assert not any(a.startswith("tree.created") for a in second.data.actions)

    def test_refuses_on_windows(self, monkeypatch):
        monkeypatch.setattr(shared_home, "is_posix", lambda: False)
        with pytest.raises(CwcliError) as exc:
            core_setup.provision()
        assert exc.value.code == "setup.windows"

    def test_requires_root(self, monkeypatch, marker_at, tmp_path):
        monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
        with pytest.raises(CwcliError) as exc:
            core_setup.provision(state_dir=tmp_path / "state")
        assert exc.value.code == "setup.needs_root"

    def test_unknown_user_is_refused(self, fake_root, stub_identity, marker_at, tmp_path, monkeypatch):
        monkeypatch.setattr(core_setup, "_user_exists", lambda name: name != "ghost")
        with pytest.raises(CwcliError) as exc:
            core_setup.provision(state_dir=tmp_path / "state", users=["ghost"])
        assert exc.value.code == "setup.unknown_user"


class TestConsolidate:
    def _provision(self, tmp_path):
        state = tmp_path / "state"
        return core_setup.provision(state_dir=state), state

    def test_merges_projects_from_a_source_home(
        self, fake_root, stub_identity, marker_at, tmp_path
    ):
        _, state = self._provision(tmp_path)
        src = tmp_path / "homeA"
        (src / "projects" / "foo").mkdir(parents=True)
        (src / "projects" / "foo" / "docker-compose.yml").write_text("services: {}\n")
        result = core_setup.consolidate(sources=[src])
        assert result.status is Status.OK
        assert result.data.merged_projects == ["foo"]
        merged = state / "projects" / "foo" / "docker-compose.yml"
        assert merged.is_file()
        # Source is never deleted.
        assert (src / "projects" / "foo").is_dir()

    def test_refuses_to_clobber_an_existing_shared_project(
        self, fake_root, stub_identity, marker_at, tmp_path
    ):
        _, state = self._provision(tmp_path)
        existing = state / "projects" / "foo"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("original")
        src = tmp_path / "homeA"
        (src / "projects" / "foo").mkdir(parents=True)
        (src / "projects" / "foo" / "keep.txt").write_text("intruder")

        result = core_setup.consolidate(sources=[src])
        assert result.status is Status.WARNING
        assert result.data.conflicts == [f"{src}:foo"]
        assert result.data.merged_projects == []
        # The existing shared project is left byte-for-byte intact.
        assert (existing / "keep.txt").read_text() == "original"

    def test_is_idempotent_on_rerun(self, fake_root, stub_identity, marker_at, tmp_path):
        _, state = self._provision(tmp_path)
        src = tmp_path / "homeA"
        (src / "projects" / "bar").mkdir(parents=True)
        first = core_setup.consolidate(sources=[src])
        assert first.data.merged_projects == ["bar"]
        # Second run: bar now exists in the shared tree -> conflict, not clobbered,
        # not a crash.
        second = core_setup.consolidate(sources=[src])
        assert second.data.merged_projects == []
        assert second.data.conflicts == [f"{src}:bar"]

    def test_needs_sources(self, fake_root, stub_identity, marker_at, tmp_path):
        self._provision(tmp_path)
        with pytest.raises(CwcliError) as exc:
            core_setup.consolidate()
        assert exc.value.code == "setup.no_sources"

    def test_needs_provisioning_first(self, fake_root, monkeypatch, tmp_path):
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(tmp_path / "absent.toml"))
        with pytest.raises(CwcliError) as exc:
            core_setup.consolidate(sources=[tmp_path / "homeA"])
        assert exc.value.code == "setup.not_provisioned"
