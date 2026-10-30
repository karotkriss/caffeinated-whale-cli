"""``core.remote_access``: the grant a remote's owner gives a hub (one restricted
``authorized_keys`` line), the pairing string, and a revocation that reaches the
bridge processes still running."""

from __future__ import annotations

import getpass
import os
import shutil
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
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM other hub"


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

    @pytest.mark.parametrize(
        "earlier",
        [
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm me@laptop",
            'from="10.0.0.0/8" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm',
        ],
    )
    def test_a_grant_another_line_with_the_same_key_would_shadow_is_refused(self, keys, host_key_file, earlier):
        """sshd applies the first line that matches a key: with the hub's key already in
        the file unrestricted (an ssh-copy-id while setting up SSH), that line would give
        it a shell and the forced bridge and the cap would never apply."""
        keys.parent.mkdir(parents=True)
        keys.write_text(f"# set up by hand\n{earlier}\n")
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file)
        assert e.value.code == "remote.key_in_use" and "Line 2 of" in e.value.message
        assert keys.read_text() == f"# set up by hand\n{earlier}\n"

    def test_the_same_key_granted_to_another_hub_is_refused_too(self, keys, host_key_file):
        _allow(keys, host_key_file, hub="hub-home")
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file)
        assert e.value.code == "remote.key_in_use"
        assert [g.hub for g in remote_access.list_grants(keys)] == ["hub-home"]

    def test_a_missing_host_key_is_a_loud_warning_and_an_empty_pin(self, keys, tmp_path):
        grant = _allow(keys, tmp_path / "nope.pub", address="10.0.0.1")
        assert hosts.parse_pairing(grant.pairing)["host_key"] is None
        assert any("host key could not be read" in w for w in grant.warnings)


class TestGrantsAndRevocation:
    def test_the_file_each_grant_went_to_is_recorded_for_hubs_and_revoke(self, keys, host_key_file, tmp_path, monkeypatch):
        """`allow --authorized-keys <file>` writes the line where the bridge will read it;
        the record of that grant is what `hubs`, `revoke` and the schedule check go by."""
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "cw"))
        monkeypatch.setenv("HOME", str(tmp_path / "someone-else"))
        _allow(keys, host_key_file, home=None, cap="admin")
        entry = remote_access.recorded_grants()[HUB]
        assert (entry["keys"], entry["home"], entry["cap"]) == (str(keys), str(tmp_path / "cw"), "admin")
        assert entry["key_fp"] == hosts.fingerprint(*KEY.split()[:2])
        assert (entry["user"], entry["uid"]) == (getpass.getuser(), os.getuid()) and entry["at"] > 0
        assert [(g.hub, g.cap) for g in remote_access.list_grants()] == [(HUB, "admin")]
        assert remote_access.grant_cap(HUB) == "admin" and remote_access.grant_cap("hub-home") is None
        done = remote_access.revoke(hub=HUB, proc_root=keys.parent)
        assert done.removed == 1 and done.files[str(keys)] == 1
        assert remote_access.recorded_grants() == {} and remote_access.list_grants() == []

    def test_a_recorded_file_that_is_gone_is_dropped_never_an_error_and_pruned_on_allow(self, keys, host_key_file, tmp_path, monkeypatch):
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "cw"))
        monkeypatch.setenv("HOME", str(tmp_path / "someone-else"))
        gone = tmp_path / "gone" / "authorized_keys"
        _allow(gone, host_key_file, home=None, cap="admin")
        gone.unlink()
        assert remote_access.list_grants() == [] and remote_access.grant_cap(HUB) is None
        _allow(keys, host_key_file, home=None, hub="hub-home", key=OTHER_KEY)
        assert {hub: e["keys"] for hub, e in remote_access.recorded_grants().items()} == {"hub-home": str(keys)}

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
    def test_a_recorded_file_this_account_cannot_read_is_listed_from_the_record_and_named(self, keys, host_key_file, tmp_path, monkeypatch):
        """Another account's grant recorded in a shared home sits behind that account's 0700
        .ssh, which this account cannot even look into: `hubs` lists it from the record, the
        schedule check and `revoke` name the account that wrote it, and the record keeps it
        while `revoke` still cleans this account's own file and ends the hub's bridges."""
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "cw"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        scanned = []
        monkeypatch.setattr(remote_access, "bridge_pids", lambda hub, **kw: scanned.append(hub) or [])
        monkeypatch.setattr(remote_access.getpass, "getuser", lambda: "alice")
        theirs = tmp_path / "alice" / ".ssh" / "authorized_keys"
        _allow(theirs, host_key_file, home=None, cap="admin")
        _allow(keys, host_key_file, home=tmp_path / "elsewhere")  # the same hub, recorded elsewhere
        os.chmod(theirs.parent, 0)
        try:
            listed = {g.keys_path: g for g in remote_access.list_grants()}
            with pytest.raises(CwcliError) as checked:
                remote_access.grant_cap(HUB)
            _allow(keys, host_key_file, home=None, hub="hub-home", key=OTHER_KEY)
            with pytest.raises(CwcliError) as revoked:
                remote_access.revoke(hub=HUB, proc_root=keys.parent)
        finally:
            os.chmod(theirs.parent, 0o700)
        assert (listed[str(keys)].cap, listed[str(keys)].unreadable) == ("operator", "")
        recorded = listed[str(theirs)]
        assert (recorded.hub, recorded.cap, recorded.recorded_by) == (HUB, "admin", f"alice (uid {os.getuid()})")
        assert recorded.unreadable and recorded.home == str(tmp_path / "cw")
        for e in (checked, revoked):
            assert str(theirs) in e.value.message and "alice" in e.value.message
        assert "alice" in revoked.value.hint
        assert [g.hub for g in remote_access.list_grants(keys)] == ["hub-home"]
        assert scanned and set(scanned) == {HUB}
        assert remote_access.recorded_grants()[HUB]["keys"] == str(theirs) and HUB in theirs.read_text()

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
    def test_on_a_shared_install_a_members_check_never_reads_another_hubs_file(self, host_key_file, tmp_path, monkeypatch):
        """Alice grants hub-x and Bob grants hub-y, each in their own 0700 .ssh, both recorded
        in the one shared home. Alice's check of hub-x reads only her file, so Bob's file,
        which she cannot read, never blocks it, and `hubs` lists Bob's grant from the record."""
        marker = tmp_path / "shared.toml"
        marker.write_text(f'enabled = true\nstate_dir = "{tmp_path / "state"}"\n')
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
        monkeypatch.delenv("CWCLI_HOME", raising=False)
        for user, hub, key in (("alice", "hub-x", KEY), ("bob", "hub-y", OTHER_KEY)):
            monkeypatch.setenv("HOME", str(tmp_path / user))
            monkeypatch.setattr(remote_access.getpass, "getuser", lambda user=user: user)
            remote_access.allow(
                hub=hub, key=key, cap="admin", python="/opt/py/bin/python3", state_path=str(tmp_path / "ui.json"),
                host_key_file=str(host_key_file), proc_root=tmp_path,
            )  # fmt: skip
        bobs = tmp_path / "bob" / ".ssh"
        monkeypatch.setenv("HOME", str(tmp_path / "alice"))
        os.chmod(bobs, 0)
        try:
            cap = remote_access.grant_cap("hub-x")
            listed = {g.hub: g for g in remote_access.list_grants()}
        finally:
            os.chmod(bobs, 0o700)
        assert cap == "admin" and listed["hub-x"].unreadable == ""
        bob = listed["hub-y"]
        assert (bob.cap, bob.keys_path, bob.recorded_by) == ("admin", str(bobs / "authorized_keys"), f"bob (uid {os.getuid()})")
        assert bob.unreadable and bob.home == str(tmp_path / "state")

    def test_a_rewrite_keeps_the_modes_and_every_other_key_byte_for_byte(self, keys, host_key_file, tmp_path, monkeypatch):
        """sshd reads the file as its owner and treats a key's comment as opaque bytes: a
        rewrite of this account's own file keeps its mode and its directory's, and a Latin-1
        comment on another key neither stops a scan nor changes when a grant is written and
        revoked."""
        monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "cw"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        theirs = b"ssh-rsa AAAAB3NzaC1yc2E= Jos\xe9's laptop\r\n"
        keys.parent.mkdir(parents=True)
        keys.parent.chmod(0o755)
        keys.write_bytes(theirs)
        keys.chmod(0o640)
        _allow(keys, host_key_file, home=None, cap="admin")
        assert (stat.S_IMODE(keys.stat().st_mode), stat.S_IMODE(keys.parent.stat().st_mode)) == (0o640, 0o755)
        assert [(g.hub, g.cap) for g in remote_access.list_grants()] == [(HUB, "admin")]
        assert remote_access.grant_cap(HUB) == "admin"
        assert remote_access.revoke(hub=HUB, proc_root=keys.parent).removed == 1
        assert keys.read_bytes() == theirs
        assert (stat.S_IMODE(keys.stat().st_mode), stat.S_IMODE(keys.parent.stat().st_mode)) == (0o640, 0o755)

    def test_a_file_another_account_owns_is_refused_unless_root_which_keeps_its_owner(self, keys, host_key_file, monkeypatch):
        """sshd opens authorized_keys as its owner, so a rewrite from another account (a new
        owner, a new mode) would lock that owner out: allow and revoke refuse it alike,
        naming the owner to run them as, with nothing written. Root rewrites it and hands it
        back to its owner and group, with its mode."""
        _allow(keys, host_key_file)
        before = keys.read_bytes()
        real_stat = os.stat

        class _Stat:
            st_uid, st_gid, st_mode = 2_147_483_641, 2_147_483_642, 0o100640

        monkeypatch.setattr(type(keys), "stat", lambda self, *a, **k: _Stat() if self == keys else real_stat(self, *a, **k))
        monkeypatch.setattr(os, "getuid", lambda: 4242)
        with pytest.raises(CwcliError) as refused_allow:
            _allow(keys, host_key_file, cap="admin")
        with pytest.raises(CwcliError) as refused_revoke:
            remote_access.revoke(hub=HUB, keys_path=keys, proc_root=keys.parent)
        assert refused_allow.value.code == refused_revoke.value.code == "remote.keys_not_owned"
        assert (refused_allow.value.message, refused_allow.value.hint) == (refused_revoke.value.message, refused_revoke.value.hint)
        assert refused_allow.value.hint == "Run this as uid 2147483641."
        assert keys.read_bytes() == before and os.listdir(keys.parent) == ["authorized_keys"]
        chowned = []
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)))
        _allow(keys, host_key_file, cap="admin")
        assert chowned == [(2_147_483_641, 2_147_483_642)] and stat.S_IMODE(real_stat(keys).st_mode) == 0o640
        assert "--max-role admin" in keys.read_text()

    def test_the_rewrite_never_follows_a_planted_link_and_leaves_nothing_behind(self, keys, host_key_file, tmp_path, monkeypatch):
        """The new content goes through a uniquely named file of its own: a link planted at a
        predictable name is never followed, and a write that fails leaves the file as it was
        and no temporary file behind."""
        _allow(keys, host_key_file)
        victim = tmp_path / "victim"
        victim.write_text("untouched\n")
        (keys.parent / "authorized_keys.cwcli-tmp").symlink_to(victim)
        _allow(keys, host_key_file, cap="viewer")
        assert victim.read_text() == "untouched\n" and "--max-role viewer" in keys.read_text()
        before = keys.read_bytes()

        def no_space(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "replace", no_space)
        with pytest.raises(CwcliError) as e:
            _allow(keys, host_key_file, cap="admin")
        assert e.value.code == "remote.keys_unwritable" and str(keys) in e.value.message
        assert keys.read_bytes() == before
        assert sorted(os.listdir(keys.parent)) == ["authorized_keys", "authorized_keys.cwcli-tmp"]

    def test_hubs_are_listed_with_their_caps(self, keys, host_key_file):
        _allow(keys, host_key_file)
        _allow(keys, host_key_file, hub="hub-home", cap="admin", key=OTHER_KEY)
        grants = remote_access.list_grants(keys)
        assert [(g.hub, g.cap, g.key_type) for g in grants] == [(HUB, "operator", "ssh-ed25519"), ("hub-home", "admin", "ssh-ed25519")]
        assert all(g.fingerprint.startswith("SHA256:") for g in grants)

    def test_revoke_removes_only_that_hubs_line(self, keys, host_key_file):
        _allow(keys, host_key_file)
        _allow(keys, host_key_file, hub="hub-home", key=OTHER_KEY)
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
        assert remote_access.bridge_pids(HUB, uids={os.getuid()}, proc_root=proc) == [13]

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
        assert remote_access.bridge_pids(HUB, uids={uid}, proc_root=tmp_path / "no-proc") == [13]

    def test_another_users_process_is_never_touched(self, tmp_path, monkeypatch):
        proc = tmp_path / "proc"
        self._proc(proc, 21, ["python3", "-m", "caffeinated_whale_cli.bridge", "--hub", HUB])
        real_stat = os.stat

        class _Stat:
            st_uid = 4243

        monkeypatch.setattr(type(proc / "21"), "stat", lambda self, *a, **k: _Stat() if self.name == "21" else real_stat(self))
        assert remote_access.bridge_pids(HUB, uids={4242}, proc_root=proc) == []

    def test_a_revoke_ends_only_the_bridges_of_the_accounts_whose_grant_it_rewrote(self, keys, host_key_file, tmp_path, monkeypatch):
        """Two bridges of one hub run under two accounts. Root's revoke that rewrote only
        alice's file ends only alice's bridge; a non-root revoke ends only its own account's,
        whatever it removed."""
        alice, bob = os.getuid(), 4243  # alice owns the grant file this test writes
        _allow(keys, host_key_file)
        proc = tmp_path / "proc"
        bridge = ["python3", "-m", "caffeinated_whale_cli.bridge", "--hub", HUB]
        owners = {31: alice, 32: bob}
        for pid in owners:
            self._proc(proc, pid, bridge)
        real_stat = os.stat

        class _Stat:
            def __init__(self, uid):
                self.st_uid = uid

        monkeypatch.setattr(type(proc), "stat", lambda self, *a, **k: _Stat(owners[int(self.name)]) if self.parent == proc else real_stat(self, *a, **k))
        sent: list[int] = []

        def fake_kill(pid, sig):
            sent.append(pid)
            shutil.rmtree(proc / str(pid))

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(os, "getuid", lambda: 0)
        done = remote_access.revoke(hub=HUB, keys_path=keys, proc_root=proc)
        assert done.removed == 1 and done.ended_pids == [31] and sent == [31] and (proc / "32").exists()
        self._proc(proc, 31, bridge)
        monkeypatch.setattr(os, "getuid", lambda: bob)
        done = remote_access.revoke(hub=HUB, keys_path=tmp_path / "bobs-keys", proc_root=proc)
        assert done.removed == 0 and done.ended_pids == [32] and sent == [31, 32] and (proc / "31").exists()
