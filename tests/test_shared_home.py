"""Unit coverage for the opt-in shared-home gate and mode policy.

The load-bearing guarantee is that the DEFAULT (per-user) behavior is unchanged:
every ``*_mode(per_user)`` helper returns the per-user mode verbatim off, and the
``secure_*_shared_only`` helpers are no-ops off. Shared mode is reached only via
the machine marker (relocatable to a temp file with ``CWCLI_SHARED_MARKER`` so
these tests need no root).
"""

import os
import stat

import pytest

from caffeinated_whale_cli.utils import config_utils, shared_home


@pytest.fixture
def marker(tmp_path, monkeypatch):
    """Write a shared-mode marker pointing at a temp state dir and select it."""
    state = tmp_path / "state"
    state.mkdir()
    marker_file = tmp_path / "shared.toml"
    marker_file.write_text(
        f'enabled = true\nstate_dir = "{state}"\ngroup = "cwcli"\nservice_user = "cwcli"\n'
    )
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker_file))
    monkeypatch.delenv("CWCLI_HOME", raising=False)
    return state, marker_file


class TestDefaultIsUnchanged:
    def test_no_marker_is_per_user(self, monkeypatch):
        monkeypatch.delenv("CWCLI_SHARED_MARKER", raising=False)
        monkeypatch.setenv("CWCLI_SHARED_MARKER", "/nonexistent/shared.toml")
        assert shared_home.shared_mode() is False
        assert shared_home.state_dir() is None
        assert shared_home.marker_config() is None

    def test_mode_helpers_return_per_user_verbatim_when_off(self, monkeypatch):
        monkeypatch.setenv("CWCLI_SHARED_MARKER", "/nonexistent/shared.toml")
        assert shared_home.dir_mode(0o700) == 0o700
        assert shared_home.dir_mode(0o755) == 0o755
        assert shared_home.file_mode(0o600) == 0o600
        assert shared_home.file_mode(0o644) == 0o644
        assert shared_home.socket_mode() == 0o666

    def test_secure_helpers_are_noops_when_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CWCLI_SHARED_MARKER", "/nonexistent/shared.toml")
        d = tmp_path / "d"
        d.mkdir(mode=0o755)
        before = stat.S_IMODE(d.stat().st_mode)
        shared_home.secure_dir_shared_only(d)
        shared_home.apply_group(d)
        assert stat.S_IMODE(d.stat().st_mode) == before

    def test_disabled_marker_is_per_user(self, tmp_path, monkeypatch):
        marker_file = tmp_path / "shared.toml"
        marker_file.write_text('enabled = false\nstate_dir = "/var/lib/cwcli"\n')
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker_file))
        assert shared_home.shared_mode() is False

    def test_malformed_marker_degrades_to_per_user(self, tmp_path, monkeypatch):
        marker_file = tmp_path / "shared.toml"
        marker_file.write_text("this is not = valid = toml [[[")
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker_file))
        assert shared_home.shared_mode() is False


class TestSharedModeGate:
    def test_marker_turns_shared_mode_on(self, marker):
        state, _ = marker
        assert shared_home.shared_mode() is True
        assert shared_home.state_dir() == state
        assert shared_home.group_name() == "cwcli"

    def test_mode_helpers_shift_in_shared_mode(self, marker):
        assert shared_home.dir_mode(0o700) == 0o2770
        assert shared_home.dir_mode(0o755) == 0o2770
        assert shared_home.file_mode(0o600) == 0o660
        assert shared_home.socket_mode() == 0o660

    def test_secure_dir_applies_setgid_in_shared_mode(self, marker, tmp_path, monkeypatch):
        # chgrp to our own gid so os.chown(-1, gid) succeeds without root.
        monkeypatch.setattr(shared_home, "gid", lambda: os.getgid())
        d = tmp_path / "state" / "cache"
        d.mkdir()
        shared_home.secure_dir_shared_only(d)
        assert stat.S_IMODE(d.stat().st_mode) == 0o2770

    def test_umask_relaxed_only_in_shared_mode(self, marker):
        old = os.umask(0o022)
        try:
            shared_home.apply_process_umask()
            assert os.umask(0o022) == 0o007  # was relaxed to 0007
        finally:
            os.umask(old)


class TestHomeResolutionPrecedence:
    def test_cwcli_home_env_wins_over_shared(self, marker, tmp_path, monkeypatch):
        private = tmp_path / "private"
        monkeypatch.setenv("CWCLI_HOME", str(private))
        assert config_utils.cwcli_home() == private

    def test_shared_marker_resolves_home_when_no_env(self, marker):
        state, _ = marker
        assert config_utils.cwcli_home() == state

    def test_per_user_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("CWCLI_HOME", raising=False)
        monkeypatch.setenv("CWCLI_SHARED_MARKER", "/nonexistent/shared.toml")
        assert config_utils.cwcli_home() == config_utils.Path.home() / config_utils.APP_NAME


class TestWindowsStaysPerUser:
    def test_non_posix_is_never_shared(self, marker, monkeypatch):
        # Even with a valid marker present, a non-POSIX platform is per-user.
        monkeypatch.setattr(shared_home, "is_posix", lambda: False)
        assert shared_home.shared_mode() is False
        assert shared_home.state_dir() is None
        assert shared_home.gid() is None
        assert shared_home.service_uid() is None
