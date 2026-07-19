"""``core.credbridge`` - the git credential bridge, exercised without Docker.

The real end-to-end proof runs against a live private repo (see the E2E tier); this
pins the mechanism's contract in isolation: the host dispatcher keys on ``host=``,
the context manager stands up a connectable socket + shim + git config, and it tears
ALL of it down on exit and on failure.
"""

from __future__ import annotations

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

    sock = tmp_path / credbridge._SOCK_NAME
    helper = tmp_path / credbridge._HELPER_NAME

    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        # shim written into the bind mount, git config points at it
        assert helper.exists()
        assert sock.exists()
        assert container.exec_calls[0] == [
            "git",
            "config",
            "--global",
            "credential.helper",
            "!/usr/bin/python3 /workspace/.git-credential-bridge.py",
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

    # teardown: socket + shim gone, git config unset
    assert not sock.exists()
    assert not helper.exists()
    assert container.exec_calls[-1] == [
        "git",
        "config",
        "--global",
        "--unset",
        "credential.helper",
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
    sock = tmp_path / credbridge._SOCK_NAME

    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
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
    sock = tmp_path / credbridge._SOCK_NAME
    helper = tmp_path / credbridge._HELPER_NAME

    with pytest.raises(RuntimeError):
        with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
            assert sock.exists() and helper.exists()
            raise RuntimeError("op blew up")

    assert not sock.exists()
    assert not helper.exists()
    assert container.exec_calls[-1][:4] == ["git", "config", "--global", "--unset"]


def test_bridge_is_noop_without_bind_mount(tmp_path):
    """No workspace bind mount resolvable -> yield with no socket, no git config."""
    container = FakeContainer(tmp_path, container_dir="/somewhere-else")
    with credbridge.credential_bridge(container, "/workspace/frappe-bench"):
        pass
    assert container.exec_calls == []  # never touched git config
    assert not (tmp_path / credbridge._SOCK_NAME).exists()
