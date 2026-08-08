"""``core.credbridge`` - the git credential bridge, exercised without Docker.

The real end-to-end proof runs against a live private repo (see the E2E tier); this
pins the mechanism's contract in isolation: the host dispatcher keys on ``host=``,
the context manager stands up a connectable socket + shim + git config, and it tears
ALL of it down on exit and on failure - including under two overlapping bridges
against the same bind mount, where each must keep its own socket/shim/config line.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

from caffeinated_whale_cli.core import credbridge

# The AF_UNIX transport is native-Linux only: it is the default transport only where
# `_prefer_tcp()` is False (native Linux Docker). On Windows AND macOS the default is
# the loopback-TCP path (Docker Desktop), and these tests connect over an AF_UNIX
# socket / assert on the ``.sock`` file. The TCP tests below cover the Docker Desktop
# transport (and run on every platform), so a Windows/macOS leg runs the whole file
# with these skipped rather than the AF_UNIX path erroring.
unix_only = pytest.mark.skipif(
    credbridge._prefer_tcp(),
    reason="AF_UNIX transport is native-Linux only; the TCP tests cover Windows and macOS",
)


def _drain(sock: socket.socket) -> bytes:
    resp = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
    return resp


class FakeContainer:
    """Records the git-config exec calls; supplies the workspace bind mount."""

    def __init__(self, host_dir, container_dir="/workspace"):
        self.attrs = {
            "Mounts": [
                {"Type": "bind", "Source": str(host_dir), "Destination": container_dir},
                {"Type": "volume", "Source": "vol", "Destination": "/var/lib/mysql"},
            ]
        }
        self.exec_calls = []

    def exec_run(self, cmd, **kwargs):
        self.exec_calls.append(cmd)
        return 0, b""


def _only(paths):
    (path,) = paths
    return path


# ------------------------------------------------------------------ host dispatch


@pytest.mark.parametrize(
    "host, expected_tool",
    [
        ("github.com", "gh"),
        ("api.github.com", "gh"),
        ("gitlab.com", "glab"),
        ("gitlab.example.com", "glab"),
    ],
)
def test_host_credential_dispatches_by_host(monkeypatch, host, expected_tool):
    seen = {}

    class Done:
        stdout = b"password=SECRET\n"

    def fake_run(argv, input=None, capture_output=None):
        seen["argv"] = argv
        seen["input"] = input
        return Done()

    monkeypatch.setattr(credbridge.subprocess, "run", fake_run)
    out = credbridge.host_credential(f"protocol=https\nhost={host}\n\n".encode())

    assert seen["argv"] == [expected_tool, "auth", "git-credential", "get"]
    assert out == b"password=SECRET\n"


def test_host_credential_missing_tool_is_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(credbridge.subprocess, "run", boom)
    assert credbridge.host_credential(b"host=github.com\n\n") == b""


def test_host_credential_audit_hook_gets_host_and_answered_never_a_credential(monkeypatch):
    """The persistent daemon passes an ``audit`` callback; it is called with the
    host and a bool - and NEVER a credential byte - so the daemon can log one line
    per request safely. Answered=True when the tool returned something, else False.
    """
    monkeypatch.setattr(
        credbridge.subprocess,
        "run",
        lambda *a, **k: type("D", (), {"stdout": b"password=SECRET\n"})(),
    )
    seen = []
    out = credbridge.host_credential(
        b"protocol=https\nhost=github.com\n\n", audit=lambda host, ok: seen.append((host, ok))
    )
    assert out == b"password=SECRET\n"  # credential still flows back to the caller
    assert seen == [("github.com", True)]  # ...but the audit hook saw only host+bool

    # an empty answer records answered=False
    monkeypatch.setattr(
        credbridge.subprocess, "run", lambda *a, **k: type("D", (), {"stdout": b""})()
    )
    seen.clear()
    credbridge.host_credential(
        b"host=gitlab.com\n\n", audit=lambda host, ok: seen.append((host, ok))
    )
    assert seen == [("gitlab.com", False)]


def test_host_credential_allowlist_refuses_before_any_tool_runs(monkeypatch):
    """A non-member ``host=`` is answered with nothing and the host tool is NEVER
    invoked; membership is an EXACT string match (a port-qualified self-hosted
    entry matches only itself); ``None`` means unrestricted - the per-invocation
    bridge's unchanged behavior."""
    calls = []
    monkeypatch.setattr(
        credbridge.subprocess,
        "run",
        lambda *a, **k: calls.append(a) or type("D", (), {"stdout": b"password=SECRET\n"})(),
    )
    seen = []
    out = credbridge.host_credential(
        b"protocol=https\nhost=git.corp.example\n\n",
        audit=lambda host, ok: seen.append((host, ok)),
        allowlist={"github.com", "gitlab.com"},
    )
    assert out == b""
    assert calls == []  # refused BEFORE gh/glab ran
    assert seen == [("git.corp.example", False)]

    # exact match: the port-qualified entry serves only the port-qualified host
    out = credbridge.host_credential(
        b"host=git.corp.example:8443\n\n", allowlist={"git.corp.example:8443"}
    )
    assert out == b"password=SECRET\n"
    assert credbridge.host_credential(b"host=git.corp.example\n\n", allowlist=set()) == b""

    # None = unrestricted (the per-invocation bridge passes nothing)
    out = credbridge.host_credential(b"host=anything.example\n\n")
    assert out == b"password=SECRET\n"


# ------------------------------------------------------------ context manager


@unix_only
def test_bridge_stands_up_and_answers_then_tears_down(tmp_path, monkeypatch):
    def fake_run(argv, input=None, capture_output=None):
        # Prove the request reached the dispatcher, and answer like gh would.
        assert b"host=github.com" in input

        class Done:
            stdout = b"username=karotkriss\npassword=TOKEN\n"

        return Done()

    monkeypatch.setattr(credbridge.subprocess, "run", fake_run)
    container = FakeContainer(tmp_path)

    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        # shim written into the bind mount, git config points at it
        helper = _only(tmp_path.glob(".git-credential-bridge-*.py"))
        sock = _only(tmp_path.glob(".git-cred-*.sock"))
        assert helper.exists()
        assert sock.exists()
        config_value = f"!/usr/bin/python3 /workspace/{helper.name}"
        assert container.exec_calls[0] == [
            "git",
            "config",
            "--global",
            "--add",
            "credential.helper",
            config_value,
        ]
        # a client (standing in for the container's git) gets a real answer
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock))
        client.sendall(b"protocol=https\nhost=github.com\n\n")
        client.shutdown(socket.SHUT_WR)
        resp = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            resp += chunk
        client.close()
        assert resp == b"username=karotkriss\npassword=TOKEN\n"

    # teardown: socket + shim gone, git config unset by its OWN exact value
    assert not sock.exists()
    assert not helper.exists()
    assert container.exec_calls[-1] == [
        "git",
        "config",
        "--global",
        "--unset",
        "credential.helper",
        f"^{re.escape(config_value)}$",
    ]


@unix_only
def test_daemon_survives_accept_timeouts(tmp_path, monkeypatch):
    """The listener must outlive its accept-timeout cycles.

    Regression: ``socket.timeout`` is an ``OSError`` subclass, so an
    ``except OSError: break`` killed the daemon on the FIRST idle timeout - about
    half a second in - and a git clone that reaches the credential step seconds
    later found nothing accepting and hung forever. Here the client connects only
    AFTER several timeout cycles have elapsed, so it fails unless the loop keeps
    listening across them.
    """
    monkeypatch.setattr(
        credbridge.subprocess, "run", lambda *a, **k: type("D", (), {"stdout": b"password=OK\n"})()
    )
    monkeypatch.setattr(credbridge, "_ACCEPT_TIMEOUT", 0.05)
    container = FakeContainer(tmp_path)

    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        sock = _only(tmp_path.glob(".git-cred-*.sock"))
        time.sleep(0.3)  # >> several _ACCEPT_TIMEOUT cycles
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock))
        client.sendall(b"host=github.com\n\n")
        client.shutdown(socket.SHUT_WR)
        resp = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            resp += chunk
        client.close()
        assert resp == b"password=OK\n"


@unix_only
def test_bridge_tears_down_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(credbridge.subprocess, "run", lambda *a, **k: None)
    container = FakeContainer(tmp_path)

    with pytest.raises(RuntimeError):
        with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
            sock = _only(tmp_path.glob(".git-cred-*.sock"))
            helper = _only(tmp_path.glob(".git-credential-bridge-*.py"))
            assert sock.exists() and helper.exists()
            raise RuntimeError("op blew up")

    assert not sock.exists()
    assert not helper.exists()
    assert container.exec_calls[-1][:4] == ["git", "config", "--global", "--unset"]


def test_bridge_tears_down_on_setup_failure(tmp_path, monkeypatch):
    """A failure DURING setup (e.g. the initial git-config exec) must still tear
    down the socket/shim/daemon thread, not just a failure inside the `with` body.
    """
    monkeypatch.setattr(credbridge.subprocess, "run", lambda *a, **k: None)

    class FailingContainer(FakeContainer):
        def exec_run(self, cmd, **kwargs):
            self.exec_calls.append(cmd)
            if cmd[:4] == ["git", "config", "--global", "--add"]:
                raise RuntimeError("docker exec blew up")
            return 0, b""

    container = FailingContainer(tmp_path)

    with pytest.raises(RuntimeError):
        with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
            pytest.fail("setup should have raised before yield")

    assert not list(tmp_path.glob(".git-cred-*.sock"))
    assert not list(tmp_path.glob(".git-credential-bridge-*.py"))


@unix_only
def test_concurrent_bridges_do_not_clobber_each_other(tmp_path, monkeypatch):
    """Two overlapping bridges against the SAME bind mount (standing in for two
    concurrent `cwcli apps install`/`apps update` runs on the same bench) must each
    get their own socket/shim/config line, and tearing one down must not remove the
    other's artifacts or unset the other's credential.helper entry.
    """
    monkeypatch.setattr(
        credbridge.subprocess, "run", lambda *a, **k: type("D", (), {"stdout": b"password=OK\n"})()
    )
    container = FakeContainer(tmp_path)

    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        outer_helper = _only(tmp_path.glob(".git-credential-bridge-*.py"))
        outer_sock = _only(tmp_path.glob(".git-cred-*.sock"))

        with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
            all_helpers = list(tmp_path.glob(".git-credential-bridge-*.py"))
            all_socks = list(tmp_path.glob(".git-cred-*.sock"))
            assert len(all_helpers) == 2
            assert len(all_socks) == 2
            inner_helper = next(h for h in all_helpers if h != outer_helper)
            inner_sock = next(s for s in all_socks if s != outer_sock)
            assert outer_helper.exists() and inner_helper.exists()
            assert outer_sock.exists() and inner_sock.exists()

        # inner torn down: its own files gone, outer's still present
        assert outer_helper.exists()
        assert outer_sock.exists()
        assert not inner_helper.exists()
        assert not inner_sock.exists()

    # outer torn down too
    assert not outer_helper.exists()
    assert not outer_sock.exists()

    add_calls = [c for c in container.exec_calls if c[:4] == ["git", "config", "--global", "--add"]]
    unset_calls = [
        c for c in container.exec_calls if c[:4] == ["git", "config", "--global", "--unset"]
    ]
    assert len(add_calls) == 2
    assert len(unset_calls) == 2
    outer_value, inner_value = add_calls[0][5], add_calls[1][5]
    assert outer_value != inner_value  # each bridge points git at its OWN shim

    # inner tears down first and must unset ONLY its own value
    assert unset_calls[0][5] == f"^{re.escape(inner_value)}$"
    # outer tears down last and must unset ONLY its own value
    assert unset_calls[1][5] == f"^{re.escape(outer_value)}$"


def test_bridge_is_noop_without_bind_mount(tmp_path):
    """No workspace bind mount resolvable -> yield with no socket, no git config."""
    container = FakeContainer(tmp_path, container_dir="/somewhere-else")
    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        pass
    assert container.exec_calls == []  # never touched git config
    assert not list(tmp_path.glob(".git-cred-*.sock"))


@pytest.mark.parametrize(
    "name, platform, expected_tcp",
    [
        ("nt", "win32", True),  # Windows
        ("posix", "darwin", True),  # macOS Docker Desktop
        ("posix", "linux", False),  # native Linux Docker
    ],
)
def test_prefer_tcp_selects_transport_by_platform(monkeypatch, name, platform, expected_tcp):
    """Windows and macOS (both Docker Desktop) use TCP; only native Linux keeps AF_UNIX."""
    monkeypatch.setattr(credbridge.os, "name", name)
    monkeypatch.setattr(credbridge.sys, "platform", platform)
    assert credbridge._prefer_tcp() is expected_tcp


# ------------------------------------------------ TCP transport (Docker Desktop host)
#
# These run on EVERY platform (loopback TCP works everywhere), forcing the TCP path
# on Unix via `_prefer_tcp`. On a Windows/macOS CI leg they are the only tests
# that exercise the bridge, since the AF_UNIX ones above are skipped there. On the
# OLD AF_UNIX-only code, entering the bridge on Windows raised AttributeError at
# `socket.AF_UNIX` before any handshake, so `test_tcp_bridge_runs_the_generated_shim`
# would have failed there and passes after the fix.


def test_serve_tcp_requires_the_token(monkeypatch):
    """The TCP listener answers ONLY a request that leads with the exact token.

    The loopback port is reachable by any host process or co-resident container, so
    the token is the credential gate: a wrong/absent token gets nothing, the right
    one gets the credential with the token stripped before dispatch.
    """
    seen = {}

    def fake_run(argv, input=None, capture_output=None):
        seen["input"] = input
        return type("D", (), {"stdout": b"password=OK\n"})()

    monkeypatch.setattr(credbridge.subprocess, "run", fake_run)

    token = b"s3cr3t-token-abc"
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.settimeout(credbridge._ACCEPT_TIMEOUT)
    srv.listen(8)
    stop = threading.Event()
    thread = threading.Thread(target=credbridge._serve, args=(srv, stop, token), daemon=True)
    thread.start()
    try:
        # wrong token -> dropped, no answer, dispatcher never called
        bad = socket.create_connection(("127.0.0.1", port))
        bad.sendall(b"WRONGTOKEN" + b"host=github.com\n\n")
        bad.shutdown(socket.SHUT_WR)
        assert _drain(bad) == b""
        bad.close()
        assert "input" not in seen  # the bad request never reached host_credential

        # right token -> the credential, and the token is stripped before dispatch
        good = socket.create_connection(("127.0.0.1", port))
        good.sendall(token + b"protocol=https\nhost=github.com\n\n")
        good.shutdown(socket.SHUT_WR)
        assert _drain(good) == b"password=OK\n"
        good.close()
        assert seen["input"] == b"protocol=https\nhost=github.com\n\n"
    finally:
        stop.set()
        srv.close()
        thread.join(timeout=2)


def test_tcp_bridge_binds_loopback_only(tmp_path, monkeypatch):
    """The TCP transport binds 127.0.0.1 (never 0.0.0.0 / a routable address) and
    writes NO socket file into the bind mount."""
    monkeypatch.setattr(credbridge, "_prefer_tcp", lambda: True)
    monkeypatch.setattr(credbridge.subprocess, "run", lambda *a, **k: None)
    container = FakeContainer(tmp_path)

    with credbridge.credential_bridge(container, "/workspace/frappe-bench") as _:
        # exactly one shim, zero socket files
        assert len(list(tmp_path.glob(".git-credential-bridge-*.py"))) == 1
        assert not list(tmp_path.glob(".git-cred-*.sock"))


def test_tcp_bridge_runs_the_generated_shim(tmp_path, monkeypatch):
    """Force the TCP transport and drive the REAL generated shim as the container's
    git would - redirected from host.docker.internal to 127.0.0.1 so it reaches the
    listener the test process holds. The token-gated daemon answers, and teardown
    removes the shim and unsets git config by its own exact value.

    This is the Windows regression test: the old AF_UNIX-only bridge raised
    AttributeError on entry here on Windows.
    """

    # Patch host_credential (not subprocess.run): this test launches the real shim
    # via subprocess.run, and patching credbridge.subprocess.run mutates the shared
    # module object, so the test's own subprocess.run would hit the fake too.
    def fake_host_credential(req):
        # the shim's request reached the dispatcher with the token already stripped
        assert req.startswith(b"protocol=https")
        assert b"host=github.com" in req
        return b"username=karotkriss\npassword=TOKEN\n"

    monkeypatch.setattr(credbridge, "_prefer_tcp", lambda: True)
    monkeypatch.setattr(credbridge, "host_credential", fake_host_credential)
    container = FakeContainer(tmp_path)

    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        helper = _only(tmp_path.glob(".git-credential-bridge-*.py"))
        assert not list(tmp_path.glob(".git-cred-*.sock"))  # TCP: no socket file
        config_value = f"!/usr/bin/python3 /workspace/{helper.name}"
        assert container.exec_calls[0] == [
            "git",
            "config",
            "--global",
            "--add",
            "credential.helper",
            config_value,
        ]
        out = subprocess.run(
            [sys.executable, str(helper), "get"],
            input=b"protocol=https\nhost=github.com\n\n",
            capture_output=True,
            env={**os.environ, "CWCLI_CRED_HOST": "127.0.0.1"},
        )
        assert out.stdout == b"username=karotkriss\npassword=TOKEN\n"

    assert not helper.exists()  # shim removed on teardown
    assert container.exec_calls[-1] == [
        "git",
        "config",
        "--global",
        "--unset",
        "credential.helper",
        f"^{re.escape(config_value)}$",
    ]
