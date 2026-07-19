"""``core.credbridge`` - the git credential bridge, exercised without Docker.

The real end-to-end proof runs against a live private repo (see the E2E tier); this
pins the mechanism's contract in isolation: the host dispatcher keys on ``host=``,
the context manager stands up a connectable socket + shim + git config, and it tears
ALL of it down on exit and on failure - including under two overlapping bridges
against the same bind mount, where each must keep its own socket/shim/config line.
"""

from __future__ import annotations

import re
import socket
import time

import pytest

from caffeinated_whale_cli.core import credbridge


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
        ("dev.egov.gy", "glab"),
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


# ------------------------------------------------------------ context manager


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
