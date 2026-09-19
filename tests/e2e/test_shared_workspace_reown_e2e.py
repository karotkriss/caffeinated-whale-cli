"""Committed real-Docker E2E for the shared-mode workspace re-own fix (v3.1.1).

Reproduces the exact bricking mechanism that took migrated Frappe instances down
after shared mode was enabled (diagnosis
`firstmate/data/cwcli-shared-supervise-perms-diagnosis/report.md`): in shared mode
`align_container_user_to_host` remaps the container's `frappe` user to the stable
service uid, but on a MIGRATED instance the bind-mounted bench workspace is still
owned by the pre-shared uid. The remapped `frappe` user can then no longer write
`<bench>/logs/bench.log`, so `bench start` exits 1 and Docker crash-loops the
instance. The fix re-owns the resolved bench dir when align remaps in shared mode.

This proves the fix with REAL uids and REAL chown semantics - a mock cannot: it
only ever asserts a generated shell string. A lightweight `python:3.12-slim`
container (no multi-GB Frappe image, no docker-in-docker) stands in for the frappe
container - what matters is a `frappe` user whose uid is remapped and a `logs/`
dir owned by a DIFFERENT ("pre-shared") uid, which is exactly the state a migrated
instance is in. The real `align_container_user_to_host` runs against it, then the
remapped `frappe` user writes its log the way `bench start` would.

Shared mode targets `os.getuid()` here (the designed fallback when no `cwcli`
service account exists on the host, so the test needs no host root and no system
account); the service-uid TARGET is unit-covered in
`tests/test_core_docker.py::test_shared_mode_aligns_to_service_uid_not_host`. What
this E2E adds is that the re-own actually makes `logs/` writable on real Docker.

Marked `standalone`: it never touches the shared session instance.
"""

from __future__ import annotations

import os

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.utils import shared_home

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

# A synthetic "pre-shared" uid/gid the migrated workspace is owned by. Any value
# distinct from the host uid works; it just has to force a real remap.
_OLD_UID = 54321
_OLD_GID = 54321
_BENCH = "/workspace/frappe-bench"
_LOG = f"{_BENCH}/logs/bench.log"

# Build the "migrated instance" state inside the container: a `frappe` user at the
# pre-shared uid owning both its home and the bench workspace (dirs 0755 / files
# 0644, ordinary bench perms), exactly what a workspace built under the old uid
# looks like before shared mode was switched on.
_SETUP = f"""
set -e
groupadd -g {_OLD_GID} frappe
useradd -u {_OLD_UID} -g {_OLD_GID} -m -d /home/frappe frappe
mkdir -p {_BENCH}/logs
chown -R {_OLD_UID}:{_OLD_GID} /home/frappe {_BENCH}
echo SETUP_OK
"""


def _armed_setup(frappe_uid: int, frappe_gid: int) -> str:
    """A v3.1.0-armed instance: `frappe` ALREADY remapped to the target id (so
    align sees no id change), but the workspace is still stranded at the old uid."""
    return f"""
set -e
groupadd -o -g {frappe_gid} frappe
useradd -o -u {frappe_uid} -g {frappe_gid} -m -d /home/frappe frappe
mkdir -p {_BENCH}/logs
chown -R {frappe_uid}:{frappe_gid} /home/frappe
chown -R {_OLD_UID}:{_OLD_GID} {_BENCH}
echo SETUP_OK
"""


def _exec(container, cmd, **kwargs) -> tuple[int, str]:
    code, out = container.exec_run(cmd, **kwargs)
    return code, out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)


# The shared app-source tree a devcontainer/externally-provisioned bench symlinks
# its apps at, OUTSIDE the bench dir. A `chown -R <bench>` never follows the
# `apps/<app>` symlink into here, which is why the pre-shared uid strands the real
# app repo and git refuses it with "dubious ownership" on the next `apps update`.
_HDSRC = "/workspace/.hdsrc"
_APP = "erpnext"
_APP_SRC = f"{_HDSRC}/{_APP}"
_APP_LINK = f"{_BENCH}/apps/{_APP}"


def _app_source_setup(frappe_uid: int, frappe_gid: int) -> str:
    """A bench whose dir is ALREADY correctly owned (v3.1.1 re-owned it) but whose
    app is symlinked to a REAL git repo under /workspace/.hdsrc still stranded at the
    pre-shared uid - the exact state that makes `apps update` hit dubious ownership."""
    return f"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends git >/dev/null
groupadd -o -g {frappe_gid} frappe
useradd -o -u {frappe_uid} -g {frappe_gid} -m -d /home/frappe frappe
mkdir -p {_BENCH}/apps/logs {_HDSRC}
git init -q {_APP_SRC}
git -C {_APP_SRC} -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
ln -s {_APP_SRC} {_APP_LINK}
chown -R {frappe_uid}:{frappe_gid} /home/frappe {_BENCH}
chown -R {_OLD_UID}:{_OLD_GID} {_HDSRC}
echo SETUP_OK
"""


def _git_status_dubious(container, host_uid: int) -> tuple[int, str]:
    """Run `git status` in the app checkout AS the frappe user (with a real HOME so no
    stray safe.directory rescues it). Returns (exit_code, combined output)."""
    return _exec(
        container,
        ["git", "-C", _APP_LINK, "status", "--porcelain"],
        user="frappe",
        environment={"HOME": "/home/frappe"},
    )


def test_shared_mode_reowns_an_app_repo_symlinked_out_of_the_bench(tmp_path, monkeypatch):
    """FIX 2: a `cwcli start`/`restart` (align) must re-own the app SOURCE repos too,
    not just the bench dir. A migrated instance whose bench was already re-owned still
    strands its `.hdsrc` app repos at the pre-shared uid, so a plain `apps update`
    git pull hits git's "dubious ownership" refusal. Align now discovers the app
    source through its symlink and re-owns it, clearing the refusal at its root."""
    import docker

    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
    assert shared_home.shared_mode(), "expected shared mode on with the marker present"

    host_uid = os.getuid()
    host_gid = os.getgid()
    assert host_uid != _OLD_UID, "host uid must differ from the pre-shared uid"

    client = docker.from_env()
    container = client.containers.run(
        "python:3.12-slim", ["sleep", "600"], detach=True, auto_remove=False
    )
    try:
        code, out = _exec(container, ["bash", "-c", _app_source_setup(host_uid, host_gid)])
        assert code == 0 and "SETUP_OK" in out, f"container setup failed:\n{out}"

        # Precondition: the bench is already correct, but the app source is stranded
        # at the pre-shared uid, so git as frappe REFUSES it (dubious ownership).
        code, owner = _exec(container, ["stat", "-c", "%u", _APP_SRC])
        assert code == 0 and owner.strip() == str(_OLD_UID), owner
        code, out = _git_status_dubious(container, host_uid)
        assert code != 0 and "dubious ownership" in out, (
            "precondition not reproduced: git should refuse the stranded app repo, "
            f"got exit {code}:\n{out}"
        )

        # Run the REAL align in shared mode, passing only the bench dir as a caller does.
        remapped, err = core_docker.align_container_user_to_host(container, bench_paths=[_BENCH])
        assert err is None, f"align reported a failure: {err}"
        assert remapped is False, "no id change was needed; the fix keys on the owner mismatch"

        # The app source (outside the bench, reached only through the symlink) was
        # re-owned to the frappe user...
        code, owner = _exec(container, ["stat", "-c", "%u", _APP_SRC])
        assert code == 0 and owner.strip() == str(host_uid), (
            f"{_APP_SRC} still owned by {owner.strip()}, not {host_uid} - the app-repo "
            "re-own did not run (a `chown -R <bench>` cannot follow the symlink)"
        )
        # ...so the very `git` an `apps update` runs now trusts the repo.
        code, out = _git_status_dubious(container, host_uid)
        assert code == 0, f"git still refuses the app repo after align (exit {code}):\n{out}"
    finally:
        container.remove(force=True)


def test_force_reown_clears_dubious_ownership_on_a_git_repo(tmp_path, monkeypatch):
    """FIX 1: `apps update --force` re-owns an app repo the container user does not
    own BEFORE pulling, so `--force` overcomes git's dubious-ownership refusal without
    a prior restart. This drives the exact primitive the update flow calls
    (`reown_app_repo_to_frappe`) against a REAL git repo and REAL chown - a mock
    cannot exercise git's ownership check."""
    import docker

    # No shared marker needed: reown targets the container frappe user's OWN uid,
    # whoever git will run as, in any mode.
    host_uid = os.getuid()
    host_gid = os.getgid()
    assert host_uid != _OLD_UID, "host uid must differ from the pre-shared uid"

    client = docker.from_env()
    container = client.containers.run(
        "python:3.12-slim", ["sleep", "600"], detach=True, auto_remove=False
    )
    try:
        code, out = _exec(container, ["bash", "-c", _app_source_setup(host_uid, host_gid)])
        assert code == 0 and "SETUP_OK" in out, f"container setup failed:\n{out}"

        # Precondition: git as frappe refuses the stranded app repo.
        code, out = _git_status_dubious(container, host_uid)
        assert code != 0 and "dubious ownership" in out, (
            f"precondition not reproduced (exit {code}):\n{out}"
        )

        # The exact call `apps update --force` makes, on the symlinked app path.
        reowned, err = core_docker.reown_app_repo_to_frappe(container, _APP_LINK)
        assert (reowned, err) == (True, None), f"reown failed: reowned={reowned} err={err}"

        # It re-owned the RESOLVED real repo (not the link), so git now trusts it.
        code, owner = _exec(container, ["stat", "-c", "%u", _APP_SRC])
        assert code == 0 and owner.strip() == str(host_uid), owner
        code, out = _git_status_dubious(container, host_uid)
        assert code == 0, f"git still refuses the app repo after reown (exit {code}):\n{out}"

        # Idempotent: a second call finds it already owned and re-owns nothing.
        reowned, err = core_docker.reown_app_repo_to_frappe(container, _APP_LINK)
        assert (reowned, err) == (False, None)
    finally:
        container.remove(force=True)


def test_shared_mode_remap_keeps_a_migrated_workspace_writable(tmp_path, monkeypatch):
    import docker

    # Turn shared mode ON via a relocatable marker (no root, no /etc/cwcli). The
    # service account is absent, so align falls back to os.getuid() as designed -
    # still a genuine remap off the pre-shared uid, which is what the fix keys on.
    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
    assert shared_home.shared_mode(), "expected shared mode on with the marker present"

    host_uid = os.getuid()
    assert host_uid != _OLD_UID, "host uid must differ from the pre-shared uid to force a remap"

    client = docker.from_env()
    container = client.containers.run(
        "python:3.12-slim",
        ["sleep", "300"],
        detach=True,
        auto_remove=False,
    )
    try:
        code, out = _exec(container, ["bash", "-c", _SETUP])
        assert code == 0 and "SETUP_OK" in out, f"container setup failed:\n{out}"

        # Precondition: the workspace is owned by the pre-shared uid, and `frappe`
        # is that uid - the migrated state align is about to remap out from under.
        code, owner = _exec(container, ["stat", "-c", "%u", f"{_BENCH}/logs"])
        assert code == 0 and owner.strip() == str(_OLD_UID), owner

        # Run the REAL align in shared mode, passing the bench dir as a caller does.
        remapped, err = core_docker.align_container_user_to_host(container, bench_paths=[_BENCH])
        assert err is None, f"align reported a failure: {err}"
        assert remapped is True, "expected a real uid/gid remap"

        # `frappe` was remapped off the pre-shared uid...
        code, uid_now = _exec(container, ["id", "-u", "frappe"])
        assert code == 0 and uid_now.strip() == str(host_uid), uid_now

        # ...and the fix re-owned the bench dir to match, so `logs/` is no longer
        # stranded at the old uid.
        code, owner = _exec(container, ["stat", "-c", "%u", f"{_BENCH}/logs"])
        assert code == 0 and owner.strip() == str(host_uid), (
            f"logs/ still owned by {owner.strip()}, not the remapped uid {host_uid} - "
            "the shared-mode workspace re-own did not run (the v3.1.0 bricking bug)"
        )

        # The crux: the remapped `frappe` user can write its log, so `bench start`
        # would come up instead of crash-looping on EACCES.
        code, out = _exec(
            container,
            ["bash", "-c", f"echo bench-start-log > {_LOG} && cat {_LOG}"],
            user="frappe",
        )
        assert (
            code == 0 and "bench-start-log" in out
        ), f"remapped frappe user could not write {_LOG} (exit {code}):\n{out}"
    finally:
        container.remove(force=True)


def test_shared_mode_recovers_a_v3_1_0_armed_instance_in_place(tmp_path, monkeypatch):
    """The v3.1.0-armed population: `frappe` is ALREADY at the service uid (so align
    sees no id change), yet the workspace is still owned by the pre-shared uid and
    the instance stays bricked. Gating the re-own on the real owner mismatch - not
    an id change - recovers it on the next `cwcli start`/`restart`."""
    import docker

    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
    assert shared_home.shared_mode(), "expected shared mode on with the marker present"

    host_uid = os.getuid()
    host_gid = os.getgid()
    assert host_uid != _OLD_UID, "host uid must differ from the pre-shared uid"

    client = docker.from_env()
    container = client.containers.run(
        "python:3.12-slim",
        ["sleep", "300"],
        detach=True,
        auto_remove=False,
    )
    try:
        code, out = _exec(container, ["bash", "-c", _armed_setup(host_uid, host_gid)])
        assert code == 0 and "SETUP_OK" in out, f"container setup failed:\n{out}"

        # Precondition: `frappe` already at the target uid (no id change to come),
        # but the workspace stranded at the pre-shared uid.
        code, uid_now = _exec(container, ["id", "-u", "frappe"])
        assert code == 0 and uid_now.strip() == str(host_uid), uid_now
        code, owner = _exec(container, ["stat", "-c", "%u", f"{_BENCH}/logs"])
        assert code == 0 and owner.strip() == str(_OLD_UID), owner

        remapped, err = core_docker.align_container_user_to_host(container, bench_paths=[_BENCH])
        assert err is None, f"align reported a failure: {err}"
        assert remapped is False, "no id change was needed; the fix must key on the owner mismatch"

        # The mismatch-gated re-own recovered the stranded workspace.
        code, owner = _exec(container, ["stat", "-c", "%u", f"{_BENCH}/logs"])
        assert code == 0 and owner.strip() == str(host_uid), (
            f"logs/ still owned by {owner.strip()}, not {host_uid} - an armed instance "
            "was not recovered (the ids-only gate would skip it)"
        )

        code, out = _exec(
            container,
            ["bash", "-c", f"echo bench-start-log > {_LOG} && cat {_LOG}"],
            user="frappe",
        )
        assert (
            code == 0 and "bench-start-log" in out
        ), f"frappe user could not write {_LOG} after recovery (exit {code}):\n{out}"
    finally:
        container.remove(force=True)
