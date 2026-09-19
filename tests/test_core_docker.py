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
    """Answers the ``id -u/-g frappe``, app-source discovery, and ``stat`` probes,
    captures the remap exec.

    ``workspace_owner`` is what a ``stat -c '%u:%g' <path>`` returns for a bench path
    or app source with no explicit owner (defaults to the container frappe user's own
    uid/gid, the migrated workspace's real owner); ``owners`` overrides it per path
    (so a half-fixed instance can report the bench as already-correct while an app
    repo is not); ``app_sources`` is what the ``apps/*`` symlink-resolution probe
    returns (the real out-of-bench app source dirs a devcontainer bench points at);
    ``workspace_present=False`` makes stat report the path absent (a fresh init whose
    bench dir does not exist yet)."""

    def __init__(
        self,
        *,
        frappe_uid,
        frappe_gid,
        remap_code=0,
        remap_out=b"",
        workspace_owner=None,
        workspace_present=True,
        app_sources=None,
        owners=None,
    ):
        self.frappe_uid = frappe_uid
        self.frappe_gid = frappe_gid
        self.remap_code = remap_code
        self.remap_out = remap_out
        self.workspace_owner = workspace_owner or f"{frappe_uid}:{frappe_gid}"
        self.workspace_present = workspace_present
        self.app_sources = list(app_sources or [])
        self.owners = dict(owners or {})
        self.remap_scripts: list[str] = []
        self.remap_user: str | None = None

    def exec_run(self, cmd, **kwargs):
        if cmd and cmd[0] == "id":
            val = self.frappe_uid if "-u" in cmd else self.frappe_gid
            return 0, (b"" if val is None else f"{val}\n".encode())
        if cmd and cmd[0] == "stat":
            if not self.workspace_present:
                return 1, b"stat: cannot statx: No such file or directory\n"
            owner = self.owners.get(cmd[3], self.workspace_owner)
            return 0, f"{owner}\n".encode()
        # the app-source discovery probe (`for d in <bench>/apps/*/ ... readlink -f`)
        if cmd and cmd[0] == "bash" and "for d in" in cmd[2] and "readlink -f" in cmd[2]:
            return 0, ("\n".join(self.app_sources) + "\n").encode()
        # the `bash -c "<remap>"` root exec
        self.remap_user = kwargs.get("user")
        self.remap_scripts.append(cmd[2])
        return self.remap_code, self.remap_out


@pytest.fixture
def host_1001(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)


def test_matching_ids_still_repair_home_when_requested(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    assert core_docker.align_container_user_to_host(c, chown_home=True) == (False, None)
    assert c.remap_scripts
    assert "chown 1000:1000 /home/frappe" in c.remap_scripts[0]


def test_matching_ids_without_home_repair_are_a_noop(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    assert core_docker.align_container_user_to_host(c) == (False, None)
    assert c.remap_scripts == []


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
    steps = script.split(" && ")
    recursive_steps = [step for step in steps if "chown -R" in step]
    shallow_steps = [
        step
        for step in steps
        if "chown -R" not in step
        and any(path in step for path in core_docker._CHOWN_HOME_SHALLOW_DIRS)
    ]

    for path in core_docker._CHOWN_HOME_RECURSIVE_DIRS:
        assert any(path in step for step in recursive_steps)
    for path in core_docker._CHOWN_HOME_SHALLOW_DIRS:
        assert any(path in step for step in shallow_steps)

    # Mutable manager state must be recursive: login shells rewrite pyenv's
    # existing shims, while pyenv, nvm, and npm all write beneath private cache
    # or alias directories. Leaving their baked files at the old uid makes the
    # first post-remap login shell or v14 tool install fail with EACCES.
    for path in (
        "/home/frappe/.npm",
        "/home/frappe/.pyenv/cache",
        "/home/frappe/.pyenv/shims",
        "/home/frappe/.nvm/.cache",
        "/home/frappe/.nvm/alias",
    ):
        assert any(path in step for step in recursive_steps)

    # Installed interpreters remain shallow. Recursing into either tree is the
    # expensive baked-toolchain copy-up this change exists to avoid.
    for path in ("/home/frappe/.pyenv/versions", "/home/frappe/.nvm/versions/node"):
        assert any(path in step for step in shallow_steps)
        assert not any(path in step for step in recursive_steps)


def test_home_chown_skips_missing_paths_without_hiding_failures(host_1001):
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c, chown_home=True)
    script = c.remap_scripts[0]

    assert "|| true" not in script
    paths = (*core_docker._CHOWN_HOME_RECURSIVE_DIRS, *core_docker._CHOWN_HOME_SHALLOW_DIRS)
    for path in paths:
        assert f"[ ! -e {path} ] || chown" in script


def test_uid_change_repairs_runtime_home_state_without_copying_the_toolchain(host_1001):
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c)  # chown_home defaults False
    script = c.remap_scripts[0]

    # A recreated container needs this even outside first provisioning: its login
    # shell runs `pyenv rehash`, which fails before the requested command when the
    # existing shims still belong to the image uid.
    assert "chown -R 1001:1001 /home/frappe/.pyenv/shims" in script
    assert "chown 1001:1001 /home/frappe/.pyenv/versions" in script
    assert "chown -R 1001:1001 /home/frappe/.pyenv/versions" not in script
    assert "chown -R 1001:1001 /home/frappe/.nvm/versions/node" not in script


def test_gid_only_change_does_not_repair_home_without_request(monkeypatch):
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    core_docker.align_container_user_to_host(c)
    assert "chown" not in c.remap_scripts[0]


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


# --------------------------- shared-mode alignment (stable service uid/gid) ------


def test_shared_mode_aligns_to_service_uid_not_host(monkeypatch):
    """Shared mode targets the STABLE cwcli service uid/gid, never os.getuid().

    Aligning to whoever ran cwcli is what ping-pongs the frappe uid between users
    on a shared container (report 2.4/6.2). Here the host uid (1001) must be
    IGNORED in favour of the service account (9000)."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 9000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 9000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    remapped, err = core_docker.align_container_user_to_host(
        c, bench_paths=["/workspace/frappe-bench"]
    )
    assert (remapped, err) == (True, None)
    script = c.remap_scripts[0]
    assert "groupmod -o -g 9000 frappe" in script
    assert "chown 9000:9000 /home/frappe" in script
    assert "1001" not in script  # the host uid is deliberately not used in shared mode
    # The remap moves `frappe` off the pre-shared uid that owns a migrated
    # instance's bind-mounted workspace, so align must ALSO re-own each resolved
    # bench dir to the new id - otherwise the remapped `frappe` user can no longer
    # write `<bench>/logs/bench.log` and `bench start` crash-loops the instance.
    assert (
        "[ ! -e /workspace/frappe-bench ] || chown -R 9000:9000 /workspace/frappe-bench" in script
    )


def test_shared_mode_falls_back_to_host_when_service_account_absent(monkeypatch):
    """A half-provisioned shared box (no service account yet) degrades to the
    per-user host target rather than crashing."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: None)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: None)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    remapped, err = core_docker.align_container_user_to_host(c)
    assert (remapped, err) == (True, None)
    assert "chown 1001:1001 /home/frappe" in c.remap_scripts[0]


def test_normal_box_remap_never_chowns_the_workspace(monkeypatch):
    """The workspace re-own is a SHARED-mode-only reconciliation. On a normal box
    align targets os.getuid(), which already owns the workspace it built, so even a
    real id change (a CI runner is uid 1001, the image frappe is 1000) must leave
    the bench dirs untouched when the caller passes them."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
    remapped, err = core_docker.align_container_user_to_host(
        c, chown_home=True, bench_paths=["/workspace/frappe-bench"]
    )
    assert (remapped, err) == (True, None)
    assert "/workspace/frappe-bench" not in c.remap_scripts[0]


def test_shared_mode_matching_owner_skips_the_workspace_reown(monkeypatch):
    """The true no-op: the frappe ids match the target AND the workspace is already
    owned by that identity, so align must not chown it even in shared mode with
    chown_home forcing the /home/frappe repair."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 1000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 1000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000, workspace_owner="1000:1000")
    remapped, err = core_docker.align_container_user_to_host(
        c, chown_home=True, bench_paths=["/workspace/frappe-bench"]
    )
    assert (remapped, err) == (False, None)
    assert "/workspace/frappe-bench" not in c.remap_scripts[0]


def test_shared_mode_matching_ids_still_recover_a_v3_1_0_armed_workspace(monkeypatch):
    """A v3.1.0-armed instance already has `frappe` remapped to the service uid, so
    the ids match now (no id change), yet its bind-mounted workspace is still owned
    by the pre-shared uid and `bench start` crash-loops writing its log. Gating the
    re-own on the real owner MISMATCH recovers it in place: align must chown the
    bench dir to the service identity even though no id changed this call."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 9000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 9000)
    # frappe is ALREADY at the service uid (armed by v3.1.0), but the workspace is
    # still owned by the pre-shared uid 500.
    c = FakeContainer(frappe_uid=9000, frappe_gid=9000, workspace_owner="500:500")
    remapped, err = core_docker.align_container_user_to_host(
        c, bench_paths=["/workspace/frappe-bench"]
    )
    assert err is None
    assert remapped is False  # no id change this call
    script = c.remap_scripts[0]
    assert (
        "[ ! -e /workspace/frappe-bench ] || chown -R 9000:9000 /workspace/frappe-bench" in script
    )


def test_shared_mode_absent_bench_dir_is_a_noop(monkeypatch):
    """A fresh init whose bench dir does not exist yet stats as absent, so there is
    no workspace to reconcile and no reown step is emitted."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 1000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 1000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000, workspace_present=False)
    remapped, err = core_docker.align_container_user_to_host(
        c, bench_paths=["/workspace/frappe-bench"]
    )
    assert (remapped, err) == (False, None)
    assert c.remap_scripts == []


# ----------------- shared-mode: app repos symlinked outside the bench (.hdsrc) ----


def test_shared_mode_reowns_app_source_symlinked_out_of_bench(monkeypatch):
    """A devcontainer bench points `apps/<app>` at a shared source tree outside the
    bench (e.g. /workspace/.hdsrc/erpnext), which a `chown -R <bench>` never follows.
    Even when the bench dir is ALREADY correctly owned (the v3.1.1 re-own fixed it),
    an app source still owned by the pre-shared uid is exactly git's dubious-ownership
    refusal on the next `apps update` - so align must re-own that app source too, and
    the bench dir being fine must not hide it."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 9000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 9000)
    # frappe already at the service uid (bench re-owned by v3.1.1), but the external
    # app source is still owned by the pre-shared uid 500.
    c = FakeContainer(
        frappe_uid=9000,
        frappe_gid=9000,
        app_sources=["/workspace/.hdsrc/erpnext"],
        owners={"/workspace/frappe-bench": "9000:9000", "/workspace/.hdsrc/erpnext": "500:500"},
    )
    remapped, err = core_docker.align_container_user_to_host(
        c, bench_paths=["/workspace/frappe-bench"]
    )
    assert err is None
    assert remapped is False  # no id change this call
    script = c.remap_scripts[0]
    assert (
        "[ ! -e /workspace/.hdsrc/erpnext ] || chown -R 9000:9000 /workspace/.hdsrc/erpnext"
        in script
    )
    # the already-correct bench dir must NOT be re-owned
    assert "chown -R 9000:9000 /workspace/frappe-bench" not in script


def test_shared_mode_native_app_dirs_add_no_extra_reown(monkeypatch):
    """A cwcli-native bench holds its apps as real dirs under <bench>/apps, so the
    discovery probe returns no out-of-bench sources and only the bench dir is
    re-owned - the native path is unchanged."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 9000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 9000)
    c = FakeContainer(frappe_uid=1000, frappe_gid=1000, app_sources=[])
    remapped, err = core_docker.align_container_user_to_host(
        c, bench_paths=["/workspace/frappe-bench"]
    )
    assert (remapped, err) == (True, None)
    script = c.remap_scripts[0]
    assert (
        "[ ! -e /workspace/frappe-bench ] || chown -R 9000:9000 /workspace/frappe-bench" in script
    )
    assert ".hdsrc" not in script


def test_shared_mode_matching_app_source_owner_skips_the_reown(monkeypatch):
    """An external app source already owned by the target identity is not re-owned:
    a healthy shared instance whose app repos already match pays no chown."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 1000)
    monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 1000)
    c = FakeContainer(
        frappe_uid=1000,
        frappe_gid=1000,
        app_sources=["/workspace/.hdsrc/erpnext"],
        owners={"/workspace/frappe-bench": "1000:1000", "/workspace/.hdsrc/erpnext": "1000:1000"},
    )
    remapped, err = core_docker.align_container_user_to_host(
        c, bench_paths=["/workspace/frappe-bench"]
    )
    assert (remapped, err) == (False, None)
    assert c.remap_scripts == []


# ------------------- reown_app_repo_to_frappe (apps update --force) ---------------


class ReownFake:
    """Answers `id`, the `readlink -f`+`stat` probe, and captures the chown exec.

    ``real_path`` is what `readlink -f <app_path>` resolves to (the real out-of-bench
    repo); ``owner`` is that repo's current `uid:gid`; ``probe_code`` non-zero makes
    the resolve fail (unreadable)."""

    def __init__(self, *, uid=9000, gid=9000, real_path="/workspace/.hdsrc/erpnext",
                 owner="500:500", probe_code=0, chown_code=0, chown_out=b""):
        self.uid = uid
        self.gid = gid
        self.real_path = real_path
        self.owner = owner
        self.probe_code = probe_code
        self.chown_code = chown_code
        self.chown_out = chown_out
        self.chown_cmd: str | None = None
        self.chown_user: str | None = None

    def exec_run(self, cmd, **kwargs):
        if cmd and cmd[0] == "id":
            val = self.uid if "-u" in cmd else self.gid
            return 0, f"{val}\n".encode()
        if cmd and cmd[0] == "bash" and "readlink -f" in cmd[2] and "stat -c" in cmd[2]:
            if self.probe_code != 0:
                return self.probe_code, b""
            return 0, f"{self.real_path}\n{self.owner}\n".encode()
        if cmd and cmd[0] == "bash" and cmd[2].startswith("chown -R"):
            self.chown_cmd = cmd[2]
            self.chown_user = kwargs.get("user")
            return self.chown_code, self.chown_out
        raise AssertionError(f"unexpected exec: {cmd}")


def test_reown_app_repo_chowns_a_mismatched_symlinked_repo():
    c = ReownFake(uid=9000, gid=9000, real_path="/workspace/.hdsrc/erpnext", owner="500:500")
    reowned, err = core_docker.reown_app_repo_to_frappe(c, "/workspace/frappe-bench/apps/erpnext")
    assert (reowned, err) == (True, None)
    # chowned the RESOLVED real path (not the symlink), recursively, as root
    assert c.chown_cmd == "chown -R 9000:9000 /workspace/.hdsrc/erpnext"
    assert c.chown_user == "root"


def test_reown_app_repo_is_a_noop_when_already_owned():
    c = ReownFake(uid=9000, gid=9000, owner="9000:9000")
    reowned, err = core_docker.reown_app_repo_to_frappe(c, "/workspace/frappe-bench/apps/erpnext")
    assert (reowned, err) == (False, None)
    assert c.chown_cmd is None  # nothing re-owned when the repo already matches


def test_reown_app_repo_degrades_when_unresolvable():
    """An unresolvable repo is not a failure to raise on: --force falls through to the
    pull's own error rather than the reown blowing up the run."""
    c = ReownFake(probe_code=1)
    reowned, err = core_docker.reown_app_repo_to_frappe(c, "/workspace/frappe-bench/apps/erpnext")
    assert (reowned, err) == (False, None)
    assert c.chown_cmd is None


def test_reown_app_repo_reports_a_failed_chown():
    c = ReownFake(owner="500:500", chown_code=1, chown_out=b"chown: cannot access\n")
    reowned, err = core_docker.reown_app_repo_to_frappe(c, "/workspace/frappe-bench/apps/erpnext")
    assert reowned is False
    assert err is not None and "could not re-own" in err


# ------------------- root-host uid alignment (issue #229) ------------------------
# On a host running as uid 0, remapping the container `frappe` user to 0 collides
# with the container's own root row in /etc/passwd, so `docker exec -u frappe`
# resolves HOME=/root, drops bench's PATH, and `bench init` fails with exit 127.
# resolve_frappe_alignment_ids is the shared target decision; align must never
# emit a remap to uid 0; align_bind_mount_source_owner keeps the host data dir
# writable by the fallback/override uid.


class TestResolveFrappeAlignmentIds:
    """The uid/gid decision table shared by align and init's data-dir chown."""

    def test_non_root_host_no_override_uses_the_host_ids(self, host_1001):
        assert core_docker.resolve_frappe_alignment_ids() == (1001, 1001, None)

    def test_root_host_no_override_falls_back_to_the_image_default_with_a_note(self, monkeypatch):
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        uid, gid, note = core_docker.resolve_frappe_alignment_ids()
        assert (uid, gid) == (
            core_docker.ROOT_HOST_FALLBACK_UID,
            core_docker.ROOT_HOST_FALLBACK_GID,
        )
        assert note is not None and "root" in note and "--uid" in note

    def test_explicit_override_wins_and_carries_no_note(self, monkeypatch):
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        # Even on a root host, an explicit --uid is honoured verbatim (no fallback).
        assert core_docker.resolve_frappe_alignment_ids(1005) == (1005, 1005, None)

    def test_shared_service_uid_wins_over_a_root_host(self, monkeypatch):
        """A properly provisioned shared install on a root host resolves to its
        non-zero service uid FIRST, so the root fallback is never reached - the
        service uid wins over root."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
        monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: 9000)
        monkeypatch.setattr(core_docker.shared_home, "gid", lambda: 9000)
        assert core_docker.resolve_frappe_alignment_ids() == (9000, 9000, None)

    def test_shared_mode_unprovisioned_on_root_host_falls_back(self, monkeypatch):
        """A half-set-up shared box degrades to os.getuid(); on a root host that is
        0, so the root fallback still applies rather than aligning frappe to 0."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
        monkeypatch.setattr(core_docker.shared_home, "service_uid", lambda: None)
        monkeypatch.setattr(core_docker.shared_home, "gid", lambda: None)
        uid, gid, note = core_docker.resolve_frappe_alignment_ids()
        assert (uid, gid) == (
            core_docker.ROOT_HOST_FALLBACK_UID,
            core_docker.ROOT_HOST_FALLBACK_GID,
        )
        assert note is not None

    def test_non_posix_host_has_nothing_to_align_to(self, monkeypatch):
        monkeypatch.delattr(core_docker.os, "getuid", raising=False)
        assert core_docker.resolve_frappe_alignment_ids() == (None, None, None)


class TestAlignNeverRemapsFrappeToRoot:
    def test_root_host_leaves_a_default_frappe_untouched(self, monkeypatch):
        """Frappe already at the image default (1000): a root host resolves the same
        1000 target, so there is no id change and NO remap to 0 is ever emitted."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
        remapped, err = core_docker.align_container_user_to_host(c, chown_home=True)
        assert err is None
        # No step anywhere sets frappe's uid to 0.
        joined = " ".join(c.remap_scripts)
        assert ":0:" not in joined and "chown 0:0" not in joined and "-g 0 " not in joined

    def test_root_host_remaps_a_stray_uid_to_the_default_not_zero(self, monkeypatch):
        """If frappe is at some other uid, a root host remaps it to the image default
        1000 (making it host-removable by root), never to 0."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        c = FakeContainer(frappe_uid=1234, frappe_gid=1234)
        remapped, err = core_docker.align_container_user_to_host(c, chown_home=True)
        assert (remapped, err) == (True, None)
        script = c.remap_scripts[0]
        assert "groupmod -o -g 1000 frappe" in script
        assert "frappe:\\1:1000:" in script  # sed sets the passwd uid to 1000
        assert ":0:" not in script

    def test_explicit_override_on_a_root_host_remaps_to_that_uid(self, monkeypatch):
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        c = FakeContainer(frappe_uid=1000, frappe_gid=1000)
        remapped, err = core_docker.align_container_user_to_host(c, uid_override=1005)
        assert (remapped, err) == (True, None)
        script = c.remap_scripts[0]
        assert "groupmod -o -g 1005 frappe" in script
        assert "frappe:\\1:1005:" in script


class TestAlignBindMountSourceOwner:
    """Keep the host-side bind-mount source dir writable by the aligned frappe user."""

    def _record_chown(self, monkeypatch):
        chowns: list[tuple] = []
        monkeypatch.setattr(core_docker.os, "chown", lambda p, u, g: chowns.append((str(p), u, g)))
        return chowns

    def test_root_host_chowns_the_data_dir_to_the_fallback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        chowns = self._record_chown(monkeypatch)
        data = tmp_path / "data"
        data.mkdir()
        aligned = core_docker.align_bind_mount_source_owner(data)
        assert aligned == core_docker.ROOT_HOST_FALLBACK_UID
        assert chowns == [(str(data), 1000, 1000)]

    def test_root_host_retry_reowns_a_root_owned_dir(self, monkeypatch, tmp_path):
        """A prior failed init left the dir root-owned; the chown is idempotent, so a
        retry re-applies it (the dir already existing must not skip the fix)."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        chowns = self._record_chown(monkeypatch)
        data = tmp_path / "data"
        data.mkdir()  # already exists from the failed run
        assert core_docker.align_bind_mount_source_owner(data) == 1000
        assert chowns == [(str(data), 1000, 1000)]

    def test_non_root_host_no_override_is_a_noop(self, monkeypatch, tmp_path):
        """The common case: frappe aligns to this process's own uid, which already
        owns the dir it created - byte-identical to before this existed (no chown)."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 1001)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 1001)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        chowns = self._record_chown(monkeypatch)
        data = tmp_path / "data"
        data.mkdir()
        assert core_docker.align_bind_mount_source_owner(data) is None
        assert chowns == []

    def test_non_root_host_override_follows_the_override(self, monkeypatch, tmp_path):
        """--uid different from the host uid must not leave an unwritable bench: the
        data dir ownership follows the override."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 1000)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 1000)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        chowns = self._record_chown(monkeypatch)
        data = tmp_path / "data"
        data.mkdir()
        assert core_docker.align_bind_mount_source_owner(data, uid_override=1001) == 1001
        assert chowns == [(str(data), 1001, 1001)]

    def test_shared_mode_is_skipped(self, monkeypatch, tmp_path):
        """Shared mode's group-writable model owns bind-mount permissions; the
        per-uid chown must not fight it."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: True)
        chowns = self._record_chown(monkeypatch)
        data = tmp_path / "data"
        data.mkdir()
        assert core_docker.align_bind_mount_source_owner(data) is None
        assert chowns == []

    def test_absent_dir_is_a_noop(self, monkeypatch, tmp_path):
        """An old '..:/workspace' instance has no data/ dir - a clean no-op."""
        monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
        monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
        monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)
        chowns = self._record_chown(monkeypatch)
        assert core_docker.align_bind_mount_source_owner(tmp_path / "nope") is None
        assert chowns == []
