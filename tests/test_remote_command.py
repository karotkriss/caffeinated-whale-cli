"""``cwcli remote``: the renderers over ``core.remote_access`` (allow, revoke, hubs on
a remote) and ``core.hosts`` (key, add, ls, remove on the hub), and the deliberate
absence of an ``axi`` twin."""

from __future__ import annotations

import json
import os
import shutil

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core import hosts, remote_access
from caffeinated_whale_cli.main import app
from caffeinated_whale_cli.utils import shared_home

runner = CliRunner()
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm hub"
HOST_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFmBvLshzLAzHjvkIyGPGlSs9uXUe8kK/zwyIT14B3QM root@box"
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("CWCLI_NO_UPDATE_CHECK", "1")


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestRemoteSide:
    def test_allow_hubs_and_revoke(self, tmp_path, monkeypatch):
        scanned = []
        monkeypatch.setattr(
            remote_access, "bridge_pids", lambda hub, **kw: scanned.append(hub) or []
        )
        keys = tmp_path / "ak"
        host_key = tmp_path / "hk.pub"
        host_key.write_text(HOST_KEY + "\n")
        grant = _json(
            runner.invoke(
                app,
                [
                    "remote",
                    "allow",
                    "--hub",
                    "hub-x",
                    "--key",
                    KEY,
                    "--host-key-file",
                    str(host_key),
                    "--authorized-keys",
                    str(keys),
                    "--address",
                    "10.0.0.2",
                    "--json",
                ],
            )
        )
        assert grant["cap"] == "operator" and grant["pairing"].startswith("cwcli-remote:v1:")
        assert hosts.parse_pairing(grant["pairing"])["address"] == "10.0.0.2"
        (line,) = keys.read_text().splitlines()
        assert line.startswith("restrict,command=") and line.endswith("cwcli-hub:hub-x")
        hubs = _json(
            runner.invoke(app, ["remote", "hubs", "--authorized-keys", str(keys), "--json"])
        )
        assert [(h["hub"], h["cap"]) for h in hubs] == [("hub-x", "operator")]
        human = runner.invoke(
            app,
            [
                "remote",
                "allow",
                "--hub",
                "hub-x",
                "--key",
                KEY,
                "--host-key-file",
                str(host_key),
                "--authorized-keys",
                str(keys),
                "--max-role",
                "viewer",
            ],
        )
        assert (
            human.exit_code == 0
            and "Replaced hub" in human.stdout
            and "cwcli-remote:v1:" in human.stdout
        )
        revoked = _json(
            runner.invoke(
                app,
                ["remote", "revoke", "--hub", "hub-x", "--authorized-keys", str(keys), "--json"],
            )
        )
        assert revoked["removed"] == 1 and keys.read_text() == ""
        assert scanned and set(scanned) == {"hub-x"}

    def test_hubs_and_revoke_find_a_grant_line_this_home_never_recorded(
        self, tmp_path, monkeypatch
    ):
        """The bridge honours a grant line whether or not this cwcli home recorded it (a reset
        home, another CWCLI_HOME): `hubs` lists it and `revoke` takes it out of its file."""
        remote_access.allow(
            hub="hub-x", key=KEY, cap="operator", home=tmp_path / "another-home", state_path=str(tmp_path / "ui.json"),
            host_key_file=str(tmp_path / "none"), proc_root=tmp_path,
        )  # fmt: skip
        default = tmp_path / "userhome" / ".ssh" / "authorized_keys"
        assert remote_access.recorded_grants() == {} and "cwcli-hub:hub-x" in default.read_text()
        hubs = _json(runner.invoke(app, ["remote", "hubs", "--json"]))
        assert [(h["hub"], h["cap"], h["keys_path"]) for h in hubs] == [
            ("hub-x", "operator", str(default))
        ]
        monkeypatch.setattr(remote_access, "bridge_pids", lambda hub, **kw: [])
        revoked = _json(runner.invoke(app, ["remote", "revoke", "--hub", "hub-x", "--json"]))
        assert revoked["removed"] == 1 and revoked["files"] == {str(default): 1}
        assert "cwcli-hub:hub-x" not in default.read_text()
        assert _json(runner.invoke(app, ["remote", "hubs", "--json"])) == []

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
    def test_a_grant_this_account_cannot_read_is_listed_from_the_record_and_revoke_names_its_account(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(remote_access, "bridge_pids", lambda hub, **kw: [])
        monkeypatch.setattr(remote_access.getpass, "getuser", lambda: "alice")
        theirs = tmp_path / "alice" / ".ssh" / "authorized_keys"
        host_key = tmp_path / "hk.pub"
        host_key.write_text(HOST_KEY + "\n")
        _json(
            runner.invoke(
                app,
                [
                    "remote",
                    "allow",
                    "--hub",
                    "hub-x",
                    "--key",
                    KEY,
                    "--max-role",
                    "admin",
                    "--host-key-file",
                    str(host_key),
                    "--authorized-keys",
                    str(theirs),
                    "--address",
                    "10.0.0.2",
                    "--json",
                ],
            )
        )
        os.chmod(theirs.parent, 0)
        try:
            listed = runner.invoke(app, ["remote", "hubs"])
            rows = _json(runner.invoke(app, ["remote", "hubs", "--json"]))
            revoked = runner.invoke(app, ["remote", "revoke", "--hub", "hub-x"])
        finally:
            os.chmod(theirs.parent, 0o700)
        alice = f"alice (uid {os.getuid()})"
        assert (
            listed.exit_code == 0
            and f"hub-x  recorded by {alice}, not readable from this account  cap admin"
            in listed.stdout
        )
        assert [(r["hub"], r["cap"], r["keys_path"], r["recorded_by"]) for r in rows] == [
            ("hub-x", "admin", str(theirs), alice)
        ]
        assert (
            revoked.exit_code == 1
            and str(theirs) in revoked.stderr
            and "Run this as alice." in revoked.stderr
        )
        assert "cwcli-hub:hub-x" in theirs.read_text()

    def test_on_a_shared_install_a_member_grants_under_their_own_login(self, tmp_path, monkeypatch):
        """The service account cannot be logged into over SSH, so a group member's grant is
        their own: the line goes into their authorized_keys and names the shared home, and the
        note says so and that only this account's revoke removes it."""
        shared = tmp_path / "state"
        marker = tmp_path / "shared.toml"
        marker.write_text(f'enabled = true\nstate_dir = "{shared}"\n')
        monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
        monkeypatch.delenv("CWCLI_HOME", raising=False)
        monkeypatch.setattr(shared_home, "service_uid", lambda: os.getuid() + 1)
        host_key = tmp_path / "hk.pub"
        host_key.write_text(HOST_KEY + "\n")
        grant = _json(
            runner.invoke(
                app,
                [
                    "remote",
                    "allow",
                    "--hub",
                    "hub-x",
                    "--key",
                    KEY,
                    "--host-key-file",
                    str(host_key),
                    "--address",
                    "10.0.0.2",
                    "--json",
                ],
            )
        )
        mine = tmp_path / "userhome" / ".ssh" / "authorized_keys"
        (line,) = mine.read_text().splitlines()
        assert f" --home {shared} --state {shared / 'run' / f'ui-{os.getuid()}.json'} " in line
        entry = remote_access.recorded_grants()["hub-x"]
        assert grant["keys_path"] == entry["keys"] == str(mine) and entry["home"] == str(shared)
        assert str(shared) in grant["note"] and "revoke` as this same account" in grant["note"]

    def test_allow_exits_1_naming_an_earlier_line_it_could_not_remove(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote_access, "bridge_pids", lambda hub, **kw: [])
        host_key = tmp_path / "hk.pub"
        host_key.write_text(HOST_KEY + "\n")
        earlier = tmp_path / "srv-keys"
        args = [
            "remote",
            "allow",
            "--hub",
            "hub-x",
            "--key",
            KEY,
            "--host-key-file",
            str(host_key),
            "--address",
            "10.0.0.2",
            "--json",
        ]
        _json(runner.invoke(app, [*args, "--authorized-keys", str(earlier)]))
        real_replace, stuck = os.replace, os.path.realpath(earlier)

        def replace(src, dst, *a, **k):
            if os.fspath(dst) == stuck:
                raise OSError(5, "Input/output error")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", replace)
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        out = json.loads(result.stdout)
        assert out["earlier_left"] == str(earlier) and out["pairing"].startswith("cwcli-remote:v1:")
        assert "cwcli-hub:hub-x" in (tmp_path / "userhome" / ".ssh" / "authorized_keys").read_text()

    def test_a_bad_key_exits_1_and_writes_nothing(self, tmp_path):
        keys = tmp_path / "ak"
        result = runner.invoke(
            app,
            [
                "remote",
                "allow",
                "--hub",
                "hub-x",
                "--key",
                "not a key",
                "--authorized-keys",
                str(keys),
            ],
        )
        assert result.exit_code == 1 and not keys.exists()


class TestHubSide:
    def test_add_ls_and_remove_without_a_network(self, tmp_path):
        pairing = hosts.make_pairing(
            {
                "v": 1,
                "hub": "hub-x",
                "cap": "operator",
                "user": "frappe",
                "address": "192.0.2.7",
                "port": 22,
                "name": "Build box",
                "host_key": HOST_KEY.rsplit(" ", 1)[0],
            }
        )
        added = _json(runner.invoke(app, ["remote", "add", pairing, "--no-check", "--json"]))
        assert (
            added["id"] == "build-box"
            and added["fingerprint"].startswith("SHA256:")
            and added["transport_on"] is False
        )
        rows = _json(runner.invoke(app, ["remote", "ls", "--json"]))
        assert [r["id"] for r in rows] == ["build-box"] and rows[0]["link"] is None
        removed = runner.invoke(app, ["remote", "remove", "build-box"])
        assert removed.exit_code == 0 and hosts.list_hosts() == []
        assert runner.invoke(app, ["remote", "remove", "build-box"]).exit_code == 1

    def test_add_without_a_host_key_is_refused(self):
        pairing = hosts.make_pairing(
            {
                "v": 1,
                "hub": "hub-x",
                "cap": "operator",
                "user": "frappe",
                "address": "192.0.2.7",
                "port": 22,
                "name": "Build box",
                "host_key": None,
            }
        )
        result = runner.invoke(app, ["remote", "add", pairing, "--no-check"])
        assert result.exit_code == 1 and "host key is required" in result.output

    def test_key_prints_the_public_half_and_the_allow_command(self):
        if shutil.which("ssh-keygen") is None:
            pytest.skip("no ssh-keygen")
        data = _json(runner.invoke(app, ["remote", "key", "--json"]))
        assert data["public_key"].startswith("ssh-ed25519 ") and data["allow_command"].startswith(
            f"cwcli remote allow --hub {data['hub']} --key "
        )
        assert "PRIVATE" not in json.dumps(data)


class TestNoAxiRemote:
    """Granting another machine's key a foothold here, or pinning a machine this hub
    will dial and act on, is a person's decision: no ``cwcli axi remote``."""

    def test_axi_registry_has_no_remote_command(self):
        names = {c.name for c in axi_mod.app.registered_commands}
        names |= {g.name for g in axi_mod.app.registered_groups}
        for group in axi_mod.app.registered_groups:
            names |= {c.name for c in group.typer_instance.registered_commands}
        assert not names & {"remote", "hosts", "allow", "revoke"}
        assert "remote" in {g.name for g in app.registered_groups}
