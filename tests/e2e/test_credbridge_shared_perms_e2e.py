"""Committed real-Docker E2E for the shared-mode credential-bridge helper
permission fix (v3.1.3).

Reproduces the third face of the shared-mode ownership bug: in shared mode
`align_container_user_to_host` remaps the container's `frappe` user to the low
`cwcli` service uid, but the credential-bridge shim was written into the
workspace bind mount with only an umask-derived mode. Under a restrictive umask
the shim landed owner-only, owned by whoever ran `cwcli apps update`, so the
remapped `frappe` git process - a DIFFERENT identity - could not open it:

    /usr/bin/python3: can't open file '/workspace/.git-credential-bridge-<h>.py':
    [Errno 13] Permission denied
    fatal: could not read Username for 'https://...'

The socket was always chmod'd explicitly; only the shim relied on the umask.
The fix makes the shim world-READABLE after every write (it carries no secret -
the credential stays behind the group-gated socket - and stays non-writable, so
git safely executes it), so any container uid can open it.

This proves the fix with REAL uids, a REAL bind-mount socket and REAL open()
permission semantics - a mock only ever asserts a generated mode. A lightweight
`python:3.12-slim` container (no multi-GB Frappe image, no docker-in-docker)
stands in for the frappe container: what matters is a `frappe` user remapped to
a LOW uid distinct from the writer, and a shim on the bind mount it must open.
The real `core.credbridge.credential_bridge` runs against it, a fake host `gh`
answers deterministic (obviously fake) credentials, and the remapped `frappe`
user runs the shim end to end - reading it (the Errno 13 that used to fire) and
completing the credential exchange over the bind-mount socket.

The restrictive umask is the crux: it makes the raw shim owner-only (the failing
state), while the socket the bridge chmods explicitly stays group-connectable -
exactly the asymmetry that WAS the bug. So this fails without the fix rather
than riding a lenient default umask to a world-readable shim.

Marked `standalone`: it never touches the shared session instance.
"""

from __future__ import annotations

import os

import pytest

from caffeinated_whale_cli.core import credbridge
from caffeinated_whale_cli.utils import shared_home

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

# The container "frappe" user's uid - a LOW value standing in for the shared-mode
# service uid, distinct from the host writer so the shim's owner never matches the
# reader. Its gid is the host's own gid so the 0660 shared-mode socket stays
# group-connectable (the socket is explicitly chmod'd; only the shim was not).
_FRAPPE_UID = 999

# Obviously-fake credentials the fake host `gh` answers; no real credential can
# flow into this exchange.
_FAKE_USER = "cwe2e-fake-user"
_FAKE_PASSWORD = "cwe2e-fake-password"


def _exec(container, cmd, **kwargs) -> tuple[int, str]:
    code, out = container.exec_run(cmd, **kwargs)
    return code, out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)


@pytest.mark.skipif(
    credbridge._prefer_tcp(),
    reason="the shared-mode read bug and its AF_UNIX socket are native-Linux only",
)
def test_shared_mode_shim_is_readable_and_runnable_by_the_remapped_frappe_uid(
    tmp_path, monkeypatch
):
    import docker

    host_uid = os.getuid()
    assert host_uid != _FRAPPE_UID, "writer uid must differ from the frappe uid to exercise the bug"

    # Shared mode ON via a relocatable marker (no root, no /etc/cwcli). No real
    # `cwcli` group exists, so socket group-application falls back to the writer's
    # gid, which the frappe user shares - the socket stays connectable while the
    # shim's readability is what the fix has to guarantee.
    marker = tmp_path / "shared.toml"
    marker.write_text("enabled = true\n")
    monkeypatch.setenv("CWCLI_SHARED_MARKER", str(marker))
    assert shared_home.shared_mode(), "expected shared mode on with the marker present"

    # A fake host `gh` first on PATH: the bridge's host listener dispatches a
    # credential request to `gh auth git-credential get`, so this makes the whole
    # path deterministic and keeps any real gh out of the exchange.
    bindir = tmp_path / "fake-tools"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        '[ "$1" = auth ] && [ "$2" = git-credential ] && [ "$3" = get ] || exit 1\n'
        "cat >/dev/null\n"
        f"printf 'username={_FAKE_USER}\\npassword={_FAKE_PASSWORD}\\n'\n"
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    # The bind-mounted workspace: a bench dir on the host, mounted at /workspace.
    bench_host = tmp_path / "data" / "frappe-bench"
    bench_host.mkdir(parents=True)

    client = docker.from_env()
    container = client.containers.run(
        "python:3.12-slim",
        ["sleep", "300"],
        detach=True,
        auto_remove=False,
        volumes={str(tmp_path / "data"): {"bind": "/workspace", "mode": "rw"}},
    )
    try:
        # A `frappe` user remapped to the low service uid, its gid the host's own.
        setup = (
            f"set -e\n"
            f"groupadd -o -g {os.getgid()} frappe\n"
            f"useradd -o -u {_FRAPPE_UID} -g {os.getgid()} -m frappe\n"
            f"echo SETUP_OK\n"
        )
        code, out = _exec(container, ["bash", "-c", setup])
        assert code == 0 and "SETUP_OK" in out, f"container setup failed:\n{out}"

        # The crux: a restrictive umask so the RAW shim write is owner-only (the
        # failing state) - the fix's explicit chmod is what makes it readable.
        old_umask = os.umask(0o077)
        try:
            with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
                shim = next((tmp_path / "data").glob(".git-credential-bridge-*.py"))
                shim_container = f"/workspace/{shim.name}"

                # 1) The remapped frappe uid can OPEN the shim - the literal
                #    `[Errno 13] Permission denied` the bug hit is gone.
                code, out = _exec(container, ["cat", shim_container], user="frappe")
                assert (
                    code == 0
                ), f"remapped frappe uid could not read the shim (exit {code}):\n{out}"

                # 2) ...and running it end to end reaches the auth step and comes
                #    back with the (fake) credential: shim -> bind-mount socket ->
                #    in-process host listener -> fake `gh`.
                code, out = _exec(
                    container,
                    [
                        "sh",
                        "-c",
                        f"printf 'protocol=https\\nhost=github.com\\n\\n' | "
                        f"python3 {shim_container} get",
                    ],
                    user="frappe",
                )
                assert "Permission denied" not in out, out
                assert code == 0, f"shim run failed as the frappe uid (exit {code}):\n{out}"
                assert f"password={_FAKE_PASSWORD}" in out, (
                    "the remapped frappe uid ran the shim but did not get the credential "
                    f"back over the bind-mount socket:\n{out}"
                )
        finally:
            os.umask(old_umask)
    finally:
        container.remove(force=True)
