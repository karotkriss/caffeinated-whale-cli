"""``core.docker.align_container_user_to_host`` - the host-uid alignment.

Bench commands run as the image's ``frappe`` user (uid 1000), so files they write
to the bind-mounted workspace are owned by 1000 on the host; a host on a different
uid (CI = 1001) then cannot ``cwcli rm`` them. This pins the remap that makes the
workspace host-owned on any host uid, and its no-op-when-matched fast path.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import docker as core_docker


class FakeContainer:
    """Answers the ``id -u/-g frappe`` probe and captures the root remap exec."""

    def __init__(self, *, frappe_uid, frappe_gid, remap_code=0, remap_out=b""):
        self.frappe_uid = frappe_uid
        self.frappe_gid = frappe_gid
        self.remap_code = remap_code
        self.remap_out = remap_out
        self.remap_scripts: list[str] = []
        self.remap_user: str | None = None

    def exec_run(self, cmd, **kwargs):
        if cmd and cmd[0] == "id":
            val = self.frappe_uid if "-u" in cmd else self.frappe_gid
            return 0, (b"" if val is None else f"{val}\n".encode())
        # the `bash -c "<remap>"` root exec
        self.remap_user = kwargs.get("user")
        self.remap_scripts.append(cmd[2])
        return self.remap_code, self.remap_out


@pytest.fixture
def host_1001(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)


def test_noop_when_ids_already_match(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    assert core_docker.align_container_user_to_host(c, chown_home=True) == (False, None)
    assert c.remap_scripts == []  # nothing ran - the dev-box common case


def test_remaps_uid_and_gid_as_root_with_home_chown(host_1001):
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    remapped, err = core_docker.align_container_user_to_host(c, chown_home=True)
    assert (remapped, err) == (True, None)
    assert c.remap_user == "root"
    script = c.remap_scripts[0]
    assert "groupmod -o -g 1001 frappe" in script
    assert "usermod -o -u 1001 frappe" in script
    assert "chown -R 1001:1001 /home/frappe" in script


def test_start_path_skips_the_slow_home_chown(host_1001):
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c)  # chown_home defaults False
    assert "usermod -o -u 1001 frappe" in c.remap_scripts[0]
    assert "chown -R" not in c.remap_scripts[0]


def test_failed_remap_is_a_soft_warning_not_a_raise(host_1001):
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000, remap_code=1, remap_out=b"boom")
    remapped, err = core_docker.align_container_user_to_host(c)
    assert remapped is False
    assert err is not None and "boom" in err


def test_unreadable_ids_are_a_soft_warning(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    c = FakeContainer(frappe_uid=None, frappe_gid=None)
    remapped, err = core_docker.align_container_user_to_host(c)
    assert remapped is False
    assert err is not None
    assert c.remap_scripts == []  # never attempted the remap on unknown ids
