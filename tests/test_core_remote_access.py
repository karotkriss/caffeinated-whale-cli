"""``core.remote_access``: the grant a remote's owner gives a hub (one restricted
``authorized_keys`` line), the pairing string, and a revocation that reaches the
bridge processes still running."""

from __future__ import annotations

import os
import signal
import stat
import sys
import types

import pytest

from caffeinated_whale_cli.core import hosts, remote_access
from caffeinated_whale_cli.core.errors import CwcliError

HUB = "hub-office"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm hub comment"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM root@box"


@pytest.fixture
def keys(tmp_path):
    return tmp_path / "home" / ".ssh" / "authorized_keys"


@pytest.fixture
def host_key_file(tmp_path):
    path = tmp_path / "ssh_host_ed25519_key.pub"
    path.write_text(HOST_KEY + "\n")
    return path


def _allow(keys, host_key_file, **kw):
    return remote_access.allow(
        hub=kw.pop("hub", HUB),
        key=kw.pop("key", KEY),
        keys_path=keys,
        python=kw.pop("python", "/opt/py/bin/python3"),
        home=kw.pop("home", keys.parent.parent / ".cwcli"),
        state_path=kw.pop("state_path", "/home/u/.cwcli/run/ui.json"),
        host_key_file=str(host_key_file),
        proc_root=kw.pop("proc_root", keys.parent),
        **kw,
    )


class TestAllow:
    def test_the_line_is_restricted_to_the_bridge_and_carries_only_the_key(self, keys, host_key_file):
        grant = _allow(keys, host_key_file)
        (line,) = keys.read_text().splitlines()
        assert line.startswith('restrict,command="/opt/py/bin/python3 -m caffeinated_whale_cli.bridge --hub hub-office --max-role operator --home ')
        assert line.endswith(" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm cwcli-hub:hub-office")
        assert "hub comment" not in line  # the hub's own comment is dropped
        assert f"--keys {keys}" in line and "--state /home/u/.cwcli/run/ui.json" in line
        # The command names the key it serves, so the bridge can require this exact line.
        assert f"--key-fp {hosts.fingerprint(*KEY.split()[:2])}\"" in line
        assert grant.cap == "operator" and grant.replaced is False and grant.ended_pids == []

    def test_the_files_get_the_modes_sshd_insists_on(self, keys, host_key_file):
        _allow(keys, host_key_file)
        assert stat.S_IMODE(keys.stat().st_mode) == 0o600
        assert stat.S_IMODE(keys.parent.stat().st_mode) == 0o700

    def test_a_key_with_options_or_a_second_line_cannot_widen_the_grant(self, keys, host_key_file):
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file, key='command="/bin/sh" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm')
        assert e.value.code == "host.key"
        two = KEY + "\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM second"
        _allow(keys, host_key_file, key=two)
        lines = keys.read_text().splitlines()
        assert len(lines) == 1 and "IFmBvLsh" not in lines[0]

    def test_a_damaged_or_foreign_key_is_refused(self, keys, host_key_file):
        for bad in ("ssh-ed25519 not-base64!", "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAAAgQC", "ecdsa-sha2-nistp256 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm", "AAAA"):
            with pytest.raises(CwcliError) as e:
                _allow(keys, host_key_file, key=bad)
            assert e.value.code == "host.key", bad
        assert not keys.exists()

    def test_allowing_the_same_hub_again_replaces_its_one_line(self, keys, host_key_file):
        keys.parent.mkdir(parents=True)
        keys.write_text("# a comment the owner wrote\nssh-rsa AAAAB3NzaC1yc2E= their-laptop\n")
        _allow(keys, host_key_file)
        grant = _allow(keys, host_key_file, cap="viewer")
        lines = keys.read_text().splitlines()
        assert grant.replaced is True
        assert lines[:2] == ["# a comment the owner wrote", "ssh-rsa AAAAB3NzaC1yc2E= their-laptop"]
        assert len(lines) == 3 and "--max-role viewer" in lines[2]

    def test_a_path_that_could_break_out_of_the_command_option_is_refused(self, keys, host_key_file):
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file, python='/tmp/x"; sh -c evil; "/bin/python3')
        assert e.value.code == "remote.unsafe_path"
        assert not keys.exists()

    def test_a_path_with_spaces_is_shell_quoted(self, keys, host_key_file):
        _allow(keys, host_key_file, python="/Users/A Person/py/bin/python3")
        (line,) = keys.read_text().splitlines()
        assert "command=\"'/Users/A Person/py/bin/python3' -m caffeinated_whale_cli.bridge" in line

    def test_the_cap_and_hub_name_are_validated(self, keys, host_key_file):
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file, cap="root")
        assert e.value.code == "host.role"
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file, hub='hub"; evil')
        assert e.value.code == "host.hub_name"

    def test_the_pairing_string_carries_the_host_key_and_how_to_reach_this_machine(self, keys, host_key_file):
        grant = _allow(keys, host_key_file, address="10.1.2.3", port=2222, name="Build box")
        fields = hosts.parse_pairing(grant.pairing)
        assert fields["address"] == "10.1.2.3" and fields["port"] == 2222 and fields["name"] == "Build box"
        assert fields["hub"] == HUB and fields["cap"] == "operator"
        assert fields["host_key"] == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM"
        assert fields["user"] == grant.fields["user"]
        assert grant.fields["host_key_fingerprint"].startswith("SHA256:")
        assert grant.warnings == []

    def test_a_label_or_user_the_hub_would_refuse_is_refused_before_anything_is_written(self, keys, host_key_file, monkeypatch):
        """The hub validates every pairing field before applying any override, so a label
        it refuses would make the printed pairing useless after the line was written."""
        for label in ("Chris's Mac", "build (EU)"):
            with pytest.raises(CwcliError) as e:
                _allow(keys, host_key_file, name=label)
            assert e.value.code == "host.name"
        monkeypatch.setattr(remote_access.getpass, "getuser", lambda: "DOMAIN\\someone")
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file, name="Build box")
        assert e.value.code == "host.user"
        assert not keys.exists()

    def test_a_missing_host_key_is_a_loud_warning_and_an_empty_pin(self, keys, tmp_path):
        grant = _allow(keys, tmp_path / "nope.pub", address="10.0.0.1")
        assert hosts.parse_pairing(grant.pairing)["host_key"] is None
        assert any("host key could not be read" in w for w in grant.warnings)


class TestGrantsAndRevocation:
    def test_hubs_are_listed_with_their_caps(self, keys, host_key_file):
        _allow(keys, host_key_file)
        _allow(keys, host_key_file, hub="hub-home", cap="admin")
        grants = remote_access.list_grants(keys)
        assert [(g.hub, g.cap, g.key_type) for g in grants] == [(HUB, "operator", "ssh-ed25519"), ("hub-home", "admin", "ssh-ed25519")]
        assert all(g.fingerprint.startswith("SHA256:") for g in grants)

    def test_revoke_removes_only_that_hubs_line(self, keys, host_key_file):
        _allow(keys, host_key_file)
        _allow(keys, host_key_file, hub="hub-home")
        done = remote_access.revoke(hub=HUB, keys_path=keys, proc_root=keys.parent)
        assert done.removed == 1
        assert [g.hub for g in remote_access.list_grants(keys)] == ["hub-home"]

    def test_revoking_a_hub_that_was_never_granted_is_a_no_op_that_says_so(self, keys, tmp_path):
        done = remote_access.revoke(hub=HUB, keys_path=keys, proc_root=tmp_path)
        assert done.removed == 0 and done.ended_pids == []

    def _proc(self, root, pid, argv, uid=None):
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        return d

    def test_revoke_ends_the_hubs_running_bridges_and_nothing_else(self, keys, host_key_file, tmp_path, monkeypatch):
        """OpenSSH checks authorized_keys only when a connection authenticates, so the
        line alone leaves an open multiplexed connection working; the bridges must go too."""
        proc = tmp_path / "proc"
        this = [sys.executable, "-m", "caffeinated_whale_cli.bridge", "--hub", HUB, "--max-role", "operator"]
        other = [sys.executable, "-m", "caffeinated_whale_cli.bridge", "--hub", "hub-home", "--max-role", "admin"]
        lookalike = ["/bin/sleep", "caffeinated_whale_cli.bridge", "--hub", HUB]
        self._proc(proc, 4001, this)
        self._proc(proc, 4002, other)
        self._proc(proc, 4003, lookalike)
        (proc / "self").mkdir()
        alive = {4001}
        sent: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            sent.append((pid, sig))
            if sig == signal.SIGTERM:
                alive.discard(pid)
                import shutil

                shutil.rmtree(proc / str(pid))

        monkeypatch.setattr(os, "kill", fake_kill)
        _allow(keys, host_key_file)
        done = remote_access.revoke(hub=HUB, keys_path=keys, proc_root=proc)
        assert done.ended_pids == [4001]
        assert sent == [(4001, signal.SIGTERM)]
        assert (proc / "4002").exists() and (proc / "4003").exists()

    def test_replacing_a_hubs_line_ends_the_bridges_it_still_runs_under_the_old_one(self, keys, host_key_file, tmp_path, monkeypatch):
        """A new cap or key reaches new channels through the bridge's own check; the
        stream a hub already holds is a bridge started under the old line, so it ends."""
        proc = tmp_path / "proc"
        self._proc(proc, 4001, [sys.executable, "-m", "caffeinated_whale_cli.bridge", "--hub", HUB, "--max-role", "admin"])
        self._proc(proc, 4002, [sys.executable, "-m", "caffeinated_whale_cli.bridge", "--hub", "hub-home", "--max-role", "admin"])
        sent: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            sent.append((pid, sig))
            import shutil

            shutil.rmtree(proc / str(pid))

        monkeypatch.setattr(os, "kill", fake_kill)
        assert _allow(keys, host_key_file, cap="admin", proc_root=proc).ended_pids == []  # nothing replaced
        grant = _allow(keys, host_key_file, cap="viewer", proc_root=proc)
        assert grant.replaced is True and grant.ended_pids == [4001]
        assert sent == [(4001, signal.SIGTERM)] and (proc / "4002").exists()

    def test_bridge_pids_match_exact_argv_tokens_not_substrings(self, tmp_path):
        proc = tmp_path / "proc"
        self._proc(proc, 11, ["python3", "-m", "caffeinated_whale_cli.bridge", "--hub", HUB + "-2"])
        self._proc(proc, 12, ["python3", "-m", "caffeinated_whale_cli.bridgework", "--hub", HUB])
        self._proc(proc, 13, ["python3", "-m", "caffeinated_whale_cli.bridge", "--hub", HUB])
        assert remote_access.bridge_pids(HUB, proc_root=proc) == [13]

    def test_the_ps_fallback_skips_rows_shlex_would_choke_on(self, tmp_path, monkeypatch):
        # No /proc (macOS): ``ps`` output is argv joined by spaces, never shell-quoted, so a
        # stray quote in some other process's argv must not abort the scan (it once did).
        uid = os.getuid()
        listing = (
            f"  7 {uid} bash -c echo 'unterminated\n"
            f" 13 {uid} python3 -m caffeinated_whale_cli.bridge --hub {HUB}\n"
            f" 14 {uid + 1} python3 -m caffeinated_whale_cli.bridge --hub {HUB}\n"
        )
        monkeypatch.setattr(
            remote_access.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout=listing),
        )
        assert remote_access.bridge_pids(HUB, proc_root=tmp_path / "no-proc") == [13]

    def test_another_users_process_is_never_touched(self, tmp_path, monkeypatch):
        proc = tmp_path / "proc"
        self._proc(proc, 21, ["python3", "-m", "caffeinated_whale_cli.bridge", "--hub", HUB])
        real_stat = os.stat

        class _Stat:
            st_uid = os.getuid() + 1

        monkeypatch.setattr(type(proc / "21"), "stat", lambda self, *a, **k: _Stat() if self.name == "21" else real_stat(self))
        assert remote_access.bridge_pids(HUB, proc_root=proc) == []
