"""Committed real-Docker E2E for the root-uid host init fix (issue #229).

Reproduces the exact mechanism that made `bench init` fail with exit 127 on a
host running as uid 0 (a common CI-runner shape: root inside a container with the
Docker socket bind-mounted). Before the fix, `align_container_user_to_host`
targeted `os.getuid()`, so on a root host it remapped the container's `frappe`
user to uid 0 - which then aliases the container's OWN `root` row in
`/etc/passwd`. `docker exec -u frappe` resolves `$HOME` via `getpwuid(0)`, which
returns the FIRST row at uid 0 (`root`, listed before `frappe`), so the exec
lands with `HOME=/root`, drops every PATH entry bench needs (which live under
`/home/frappe`), and `bench init` dies with `bench: not found` (exit 127).

This proves the fix with REAL uids and a REAL `docker exec -u frappe`, which a
mock cannot: it exercises the container runtime's own uid->HOME resolution. A
lightweight `python:3.12-slim` container (no multi-GB Frappe image, no
docker-in-docker, no host root needed) stands in for the frappe container - what
matters is a `frappe` user whose home holds its toolchain and a container `root`
row at uid 0, exactly the collision the fix avoids. A root host is SIMULATED by
monkeypatching `os.getuid`/`os.getgid` to 0 (the test process itself need not be
root, since the align never chowns the host filesystem here); the host-side
data-dir chown is unit-covered in
`tests/test_core_docker.py::TestAlignBindMountSourceOwner`.

Marked `standalone`: each test builds its own throwaway container and never
touches the shared session instance.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import docker as core_docker

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

_HOME = "/home/frappe"
_DEFAULT_UID = 1000  # the frappe/bench image's own default uid for `frappe`

# A frappe user at the image default uid whose HOME holds a `bench` stub reachable
# only relative to /home/frappe - the same way the real image puts pyenv/nvm shims
# and bench there. `docker exec -u frappe` can only find it when HOME resolves to
# /home/frappe, so a wrong HOME (the bug) reproduces the exact "bench: not found".
_SETUP = f"""
set -e
groupadd -g {_DEFAULT_UID} frappe
useradd -u {_DEFAULT_UID} -g {_DEFAULT_UID} -m -d {_HOME} frappe
mkdir -p {_HOME}/bin
printf '#!/bin/sh\\necho bench-ran\\n' > {_HOME}/bin/bench
chmod +x {_HOME}/bin/bench
printf 'export PATH="$HOME/bin:$PATH"\\n' > {_HOME}/.profile
chown -R {_DEFAULT_UID}:{_DEFAULT_UID} {_HOME}
echo SETUP_OK
"""

# Simulate the PRE-FIX align on a root host: remap `frappe` to uid 0, aliasing the
# container's own root row (the whole bug).
_REMAP_TO_ROOT = r"""
set -e
sed -i 's/^frappe:\([^:]*\):[^:]*:/frappe:\1:0:/' /etc/passwd
echo REMAPPED
"""


def _exec(container, cmd, **kwargs) -> tuple[int, str]:
    code, out = container.exec_run(cmd, **kwargs)
    return code, out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)


def _frappe_login(container) -> tuple[int, str]:
    """What bench sees: a login shell as `frappe`, reporting HOME and whether the
    home-relative `bench` stub is on PATH (the exit-127 proxy)."""
    return _exec(
        container,
        ["bash", "-lc", 'echo "HOME=$HOME"; command -v bench && bench || echo BENCH_MISSING'],
        user="frappe",
    )


def _new_container():
    import docker

    return docker.from_env().containers.run(
        "python:3.12-slim", ["sleep", "300"], detach=True, auto_remove=False
    )


def _as_root_host(monkeypatch):
    """Simulate a uid-0 host and per-user (non-shared) mode."""
    monkeypatch.setattr(core_docker.os, "getuid", lambda: 0)
    monkeypatch.setattr(core_docker.os, "getgid", lambda: 0)
    monkeypatch.setattr(core_docker.shared_home, "shared_mode", lambda: False)


def test_pre_fix_remap_to_uid_0_reproduces_the_exit_127_mechanism():
    """The bug: with `frappe` remapped to uid 0, `docker exec -u frappe` resolves
    HOME=/root (getpwuid(0) returns the root row), so the home-relative toolchain is
    off PATH - exactly the `bench: not found` (exit 127) init failure. This pins the
    mechanism the fix must avoid; if this stops reproducing, the E2E below proves
    nothing."""
    container = _new_container()
    try:
        code, out = _exec(container, ["bash", "-c", _SETUP])
        assert code == 0 and "SETUP_OK" in out, out

        # Healthy baseline: at the default uid, frappe's login shell finds bench.
        code, out = _frappe_login(container)
        assert f"HOME={_HOME}" in out and "bench-ran" in out, out

        # Apply the pre-fix remap-to-root, then observe the breakage.
        code, out = _exec(container, ["bash", "-c", _REMAP_TO_ROOT])
        assert code == 0 and "REMAPPED" in out, out
        code, out = _frappe_login(container)
        assert "HOME=/root" in out, f"expected the uid-0 collision to force HOME=/root:\n{out}"
        assert "BENCH_MISSING" in out, f"expected bench off PATH under HOME=/root:\n{out}"
    finally:
        container.remove(force=True)


def test_align_on_a_root_host_keeps_frappe_usable(monkeypatch):
    """The fix: on a root host with no --uid, the REAL align leaves `frappe` at the
    image default instead of remapping it to 0, so `docker exec -u frappe` keeps
    HOME=/home/frappe and the toolchain stays on PATH - no exit 127."""
    _as_root_host(monkeypatch)
    container = _new_container()
    try:
        code, out = _exec(container, ["bash", "-c", _SETUP])
        assert code == 0 and "SETUP_OK" in out, out

        remapped, err = core_docker.align_container_user_to_host(container, chown_home=True)
        assert err is None, f"align reported a failure on a root host: {err}"

        # `frappe` is STILL at the image default - never remapped to 0.
        code, uid_now = _exec(container, ["id", "-u", "frappe"])
        assert code == 0 and uid_now.strip() == str(_DEFAULT_UID), (
            f"frappe uid is {uid_now.strip()}, not the image default {_DEFAULT_UID} - a root "
            "host must never remap frappe to 0"
        )
        # ...so a login shell as frappe finds its home and its toolchain.
        code, out = _frappe_login(container)
        assert (
            f"HOME={_HOME}" in out and "bench-ran" in out
        ), f"root-host align broke frappe's HOME/PATH (the exit-127 bug):\n{out}"
    finally:
        container.remove(force=True)


def test_uid_override_on_a_root_host_remaps_to_that_uid(monkeypatch):
    """The escape hatch: an explicit --uid on a root host remaps `frappe` to that
    uid (not 0, not the default), and the user stays usable (HOME unchanged, bench on
    PATH) - proven against real Docker."""
    _as_root_host(monkeypatch)
    override = 1234
    container = _new_container()
    try:
        code, out = _exec(container, ["bash", "-c", _SETUP])
        assert code == 0 and "SETUP_OK" in out, out

        remapped, err = core_docker.align_container_user_to_host(
            container, chown_home=True, uid_override=override
        )
        assert (remapped, err) == (True, None), f"expected a real remap: remapped={remapped} {err}"

        code, uid_now = _exec(container, ["id", "-u", "frappe"])
        assert code == 0 and uid_now.strip() == str(override), uid_now
        # The passwd home field is unchanged, so getpwuid(override) still yields
        # /home/frappe and the login shell stays healthy.
        code, out = _frappe_login(container)
        assert f"HOME={_HOME}" in out and "bench-ran" in out, out
    finally:
        container.remove(force=True)
