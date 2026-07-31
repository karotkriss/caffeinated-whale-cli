"""Regression tests for host-uid alignment and its matching-id/platform no-op paths."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


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
    assert "chown 1001:1001 /home/frappe" in script
    # NEVER a full recursive chown of the home tree - that is what forced the
    # baked pyenv/nvm toolchain's overlayfs copy-up (79s measured; see
    # core/docker.py's _CHOWN_HOME_* constants for the narrowed replacement).
    assert "chown -R 1001:1001 /home/frappe " not in script
    assert not script.rstrip().endswith("chown -R 1001:1001 /home/frappe")


def test_uid_change_never_uses_usermod(host_1001):
    """`usermod -u` unconditionally chowns the target's entire $HOME as a
    documented side effect (measured ~90s / a full 1.28 GB overlayfs copy-up on
    a real image, reproduced twice) - the actual cost this function used to
    blame entirely on the separate `chown -R`. The uid must be changed by
    editing /etc/passwd directly, which does not walk the filesystem at all;
    `usermod` reappearing here would silently reintroduce that walk."""
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c, chown_home=True)
    assert "usermod" not in c.remap_scripts[0]


def test_sed_uid_edit_matches_real_usermod_output():
    """The sed replacement must produce the exact /etc/passwd row `usermod -u`
    would have (verified against a real `usermod` run): only the uid field
    (3rd column) changes, gid/home/shell and every other user's row untouched."""
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.MonkeyPatch.context() as m:
            m.setattr(core_docker.os, "getuid", lambda: 1001)
            m.setattr(core_docker.os, "getgid", lambda: 1000)  # gid unchanged this run
            core_docker.align_container_user_to_host(c)
        script = c.remap_scripts[0]
        sed_cmd = next(s for s in script.split(" && ") if s.startswith("sed"))

        passwd = Path(tmp) / "passwd"
        passwd.write_text(
            "root:x:0:0:root:/root:/bin/bash\n"
            "frappe:x:1000:1000::/home/frappe:/bin/sh\n"
            "other:x:1000:1000::/home/other:/bin/sh\n"
        )
        subprocess.run(sed_cmd.replace("/etc/passwd", str(passwd)), shell=True, check=True)

        lines = passwd.read_text().splitlines()
        assert lines[0] == "root:x:0:0:root:/root:/bin/bash"
        assert lines[1] == "frappe:x:1001:1000::/home/frappe:/bin/sh"
        # A different user sharing the OLD uid must be left alone - the edit
        # is anchored to the "frappe:" row, not a blind uid substitution.
        assert lines[2] == "other:x:1000:1000::/home/other:/bin/sh"


def test_home_chown_is_narrowed_to_provisioning_write_paths(host_1001):
    """The chown must re-own only what `_install_pyenv_python`/`_install_nvm_node`
    (core/init.py) actually write, never the baked toolchain beneath them - a
    drifted list here silently re-introduces the full-tree chown's slowness."""
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c, chown_home=True)
    script = c.remap_scripts[0]
    recursive_step = next(s for s in script.split(" && ") if "chown -R" in s)
    shallow_step = next(s for s in script.split(" && ") if "chown -R" not in s and ".pyenv" in s)

    for path in core_docker._CHOWN_HOME_RECURSIVE_DIRS:
        assert path in recursive_step
    for path in core_docker._CHOWN_HOME_SHALLOW_DIRS:
        assert path in shallow_step

    # The recursive re-own is scoped to caches/config, never the toolchain dirs
    # a first provision writes new versions INTO (pyenv/nvm need only their
    # parent directory re-owned, not the tens of thousands of files inside).
    assert "/home/frappe/.pyenv" not in recursive_step
    assert "/home/frappe/.nvm" not in recursive_step


def test_start_path_skips_the_slow_home_chown(host_1001):
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c)  # chown_home defaults False
    assert "1001" in c.remap_scripts[0]  # the uid still gets remapped
    assert "chown -R" not in c.remap_scripts[0]
    assert "chown 1001" not in c.remap_scripts[0]  # ...but home is never touched


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


def test_no_op_on_a_platform_without_getuid(monkeypatch):
    """Windows Python has no os.getuid/getgid; must no-op, never AttributeError."""
    monkeypatch.delattr(core_docker.os, "getuid", raising=False)
    monkeypatch.delattr(core_docker.os, "getgid", raising=False)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    assert core_docker.align_container_user_to_host(c, chown_home=True) == (False, None)
    assert c.remap_scripts == []


class TestEnsureComposeAvailable:
    """The ``docker compose version`` preflight init/scale both run up front, so
    a bare Docker Engine host missing the v2 plugin fails clean before either
    command creates any state, instead of mid-run as a raw subprocess error."""

    def test_present_is_a_silent_no_op(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(
                returncode=0, stdout=b"Docker Compose version v2.29.1", stderr=b""
            )

        monkeypatch.setattr(core_docker.subprocess, "run", fake_run)
        core_docker.ensure_compose_available()  # must not raise
        assert calls == [["docker", "compose", "version"]]

    def test_nonzero_exit_is_a_typed_precondition_with_an_install_hint(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"docker: 'compose' is not a docker command"
            )

        monkeypatch.setattr(core_docker.subprocess, "run", fake_run)
        with pytest.raises(CwcliError) as exc:
            core_docker.ensure_compose_available()
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "compose.unavailable"
        assert "docker-compose-plugin" in (exc.value.hint or "")

    def test_missing_docker_binary_is_also_a_typed_precondition(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("docker not found")

        monkeypatch.setattr(core_docker.subprocess, "run", fake_run)
        with pytest.raises(CwcliError) as exc:
            core_docker.ensure_compose_available()
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "compose.unavailable"

    def test_hanging_probe_is_bounded_and_typed(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr(core_docker.subprocess, "run", fake_run)
        with pytest.raises(CwcliError) as exc:
            core_docker.ensure_compose_available()
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "compose.unavailable"
