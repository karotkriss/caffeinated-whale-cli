"""The remote-side bridge (``caffeinated_whale_cli.bridge``): the forced command a
hub's restricted SSH key runs. Each test here guards one of the design's security
lessons: the key reaches cwcli's typed API and nothing else, the remote caps what a
hub may do, the remote's own token never leaves the machine, revocation reaches a
channel opened after the grant is gone, and a stripped hop-by-hop header can never
run a request as the proxy itself."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from caffeinated_whale_cli import bridge
from caffeinated_whale_cli.core import remote_access
from caffeinated_whale_cli.core.protocol import PROTOCOL_VERSION

HUB = "hub-test"
TOKEN = "the-remote-daemon-token"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoLvGEvGjS97I/0qCfGZ07VM9w5bY4Gd4/g9hbWOwYm"


class _Daemon:
    """A loopback stand-in for the remote ``cwcli ui`` daemon that records what the
    bridge sends it and answers like the real one (JSON, Content-Length, close)."""

    def __init__(self):
        self.requests: list[dict] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parent.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if self.path == "/api/v1/meta":
                    payload = {"version": "9.9.9", "protocol": PROTOCOL_VERSION, "build": "x"}
                else:
                    payload = {"data": {"echo": self.path, "seen": self.headers.get("X-Cwcli-Delegated-Role")}}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Set-Cookie", "remote_session=should-not-matter; Path=/")
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _answer  # noqa: N815

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def daemon():
    d = _Daemon()
    try:
        yield d
    finally:
        d.close()


@pytest.fixture
def grant(tmp_path, daemon):
    """A granted hub: the authorized_keys line ``cwcli remote allow`` writes, a state
    file naming the daemon, and the argv the line puts on the bridge."""
    keys = tmp_path / "authorized_keys"
    remote_access.allow(
        hub=HUB,
        key=KEY,
        cap="operator",
        keys_path=keys,
        python=sys.executable,
        home=tmp_path / "home",
        state_path=str(tmp_path / "ui.json"),
        host_key_file=str(tmp_path / "missing-host-key"),
    )
    (tmp_path / "ui.json").write_text(json.dumps({"port": daemon.port, "token": TOKEN}))
    return [
        "--hub", HUB, "--max-role", "operator", "--home", str(tmp_path / "home"),
        "--state", str(tmp_path / "ui.json"), "--keys", str(keys), "--no-start",
    ]  # fmt: skip


def _run(argv, stdin: bytes, env=None):
    """Drive ``bridge.main`` the way sshd does: the request on a pipe, the answer on a
    buffer. Returns ``(exit code, stdout bytes)``."""
    r, w = os.pipe()
    os.write(w, stdin)
    os.close(w)
    out = io.BytesIO()
    try:
        code = bridge.main(argv, stdin_fd=r, out=out, environ=env or {})
    finally:
        os.close(r)
    return code, out.getvalue()


def _status_and_json(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body or b"{}")


def _error_code(raw: bytes) -> str:
    return _status_and_json(raw)[1]["errors"][0]["title"]


class TestNotAShell:
    def test_a_command_passed_by_the_client_is_refused_before_anything_is_read(self, grant, capsys):
        code, out = _run(grant, b"GET /api/v1/meta HTTP/1.1\r\n\r\n", env={"SSH_ORIGINAL_COMMAND": "cat /etc/hostname; id"})
        assert code == 2 and out == b""
        assert "restricted to the cwcli bridge" in capsys.readouterr().err

    def test_stdin_that_is_not_an_http_request_exits_2(self, grant, capsys):
        code, out = _run(grant, b"cat /etc/hostname; id\n")
        assert code == 2 and out == b""
        assert "expected an HTTP request" in capsys.readouterr().err

    def test_an_empty_channel_exits_2(self, grant):
        assert _run(grant, b"")[0] == 2

    @pytest.mark.parametrize(
        "line",
        [b"GET /etc/passwd HTTP/1.1", b"GET / HTTP/1.1", b"DELETE /api/v1/jobs/x HTTP/1.1", b"GET /api/v2/x HTTP/1.1"],
    )
    def test_only_get_and_post_on_the_api_are_requests_at_all(self, grant, line):
        code, out = _run(grant, line + b"\r\n\r\n")
        assert code == 2 and out == b""

    @pytest.mark.parametrize("path", ["/api/v1/session", "/api/v1/session/password", "/api/v1/hosts", "/api/v1/hosts/x/instances"])
    def test_the_session_and_hosts_routes_are_never_forwarded(self, grant, path, daemon):
        code, out = _run(grant, f"GET {path} HTTP/1.1\r\n\r\n".encode())
        assert code == 1 and _error_code(out) == "bridge.path_refused"
        assert daemon.requests == []


class TestTheGrantIsRechecked:
    def test_a_channel_opened_after_the_line_is_gone_is_refused(self, grant, tmp_path, daemon):
        remote_access.revoke(hub=HUB, keys_path=tmp_path / "authorized_keys", proc_root=tmp_path)
        code, out = _run(grant, b"GET /api/v1/meta HTTP/1.1\r\n\r\n")
        assert code == 1 and _error_code(out) == "bridge.revoked"
        assert daemon.requests == []

    def test_an_unreadable_keys_file_is_a_missing_grant(self, tmp_path):
        assert bridge.still_allowed(str(tmp_path / "nope"), HUB) is False

    def test_a_different_hubs_line_does_not_keep_this_one_alive(self, tmp_path):
        keys = tmp_path / "authorized_keys"
        remote_access.allow(hub="other-hub", key=KEY, keys_path=keys, python=sys.executable, home=tmp_path, state_path="s", host_key_file="/nonexistent")
        assert bridge.still_allowed(str(keys), HUB) is False
        assert bridge.still_allowed(str(keys), "other-hub") is True

    def test_a_key_that_merely_mentions_the_marker_is_not_a_grant(self, tmp_path):
        keys = tmp_path / "authorized_keys"
        keys.write_text(f"{KEY} cwcli-hub:{HUB}\n")  # no restrict, no bridge command
        assert bridge.still_allowed(str(keys), HUB) is False


class TestRewrite:
    def _headers(self, **extra):
        headers = [
            ("Host", "evil.example"),
            ("Authorization", "Bearer forged-by-the-hub"),
            ("Cookie", "cwcli_serve_session_1=stolen"),
            ("Origin", "http://attacker"),
            ("Sec-Fetch-Site", "cross-site"),
            ("Referer", "http://attacker/x"),
            ("X-Forwarded-For", "1.2.3.4"),
            ("Accept", "application/json"),
            ("Content-Type", "application/json"),
            ("User-Agent", "cwcli-hub/1"),
        ]
        headers.extend(extra.items())
        return headers

    def _rewrite(self, headers, cap="operator"):
        out = bridge.rewrite("POST", "/api/v1/jobs", headers, hub=HUB, cap=cap, port=1234, token=TOKEN, body_length=2)
        return {k.lower(): v for k, v in out}

    def test_credentials_identity_and_provenance_from_the_hub_are_dropped(self):
        sent = self._rewrite(self._headers(**{"X-Cwcli-Delegated-User": "alice", "X-Cwcli-Delegated-Role": "operator"}))
        assert sent["authorization"] == f"Bearer {TOKEN}"
        assert "cookie" not in sent and "origin" not in sent and "referer" not in sent
        assert "sec-fetch-site" not in sent and "x-forwarded-for" not in sent
        assert sent["host"] == "127.0.0.1:1234"
        assert sent["connection"] == "close"
        assert sent["content-length"] == "2"
        assert sent["accept"] == "application/json" and sent["user-agent"] == "cwcli-hub/1"

    def test_the_claimed_role_is_lowered_to_the_cap_and_never_raised(self):
        assert self._rewrite(self._headers(**{"X-Cwcli-Delegated-Role": "admin"}))["x-cwcli-delegated-role"] == "operator"
        assert self._rewrite(self._headers(**{"X-Cwcli-Delegated-Role": "viewer"}))["x-cwcli-delegated-role"] == "viewer"
        assert self._rewrite(self._headers(**{"X-Cwcli-Delegated-Role": "admin"}), cap="admin")["x-cwcli-delegated-role"] == "admin"
        assert self._rewrite(self._headers(**{"X-Cwcli-Delegated-Role": "root"}))["x-cwcli-delegated-role"] == "viewer"
        assert self._rewrite(self._headers())["x-cwcli-delegated-role"] == "viewer"  # no claim: the lowest

    def test_the_hub_name_on_the_delegated_user_is_the_bridges_own(self):
        assert self._rewrite(self._headers(**{"X-Cwcli-Delegated-User": "alice@some-other-hub"}))["x-cwcli-delegated-user"] == f"alice@{HUB}"
        assert self._rewrite(self._headers(**{"X-Cwcli-Delegated-User": "al ice\r\nX-Injected: 1"}))["x-cwcli-delegated-user"] == f"hub@{HUB}"
        assert self._rewrite(self._headers())["x-cwcli-delegated-user"] == f"hub@{HUB}"
        assert self._rewrite(self._headers())["x-cwcli-hub"] == HUB

    def test_a_connection_header_naming_a_delegation_header_is_refused(self):
        with pytest.raises(bridge.BridgeError) as e:
            bridge.rewrite(
                "GET", "/api/v1/instances",
                [("Connection", "X-Cwcli-Delegated-Role"), ("X-Cwcli-Delegated-Role", "admin")],
                hub=HUB, cap="operator", port=1, token=TOKEN, body_length=0,
            )  # fmt: skip
        assert e.value.status == 400 and e.value.code == "bridge.connection_header"

    def test_headers_named_in_connection_are_dropped_as_hop_by_hop(self):
        sent = self._rewrite([("Connection", "X-Custom, keep-alive"), ("X-Custom", "gone"), ("Accept", "a")])
        assert "x-custom" not in sent and sent["accept"] == "a"

    def test_chunked_bodies_are_refused(self):
        with pytest.raises(bridge.BridgeError) as e:
            self._rewrite([("Transfer-Encoding", "chunked")])
        assert e.value.status == 411


class TestTheRealExchange:
    def test_a_read_reaches_the_daemon_with_the_machines_own_token_and_identity(self, grant, daemon):
        request = (
            "GET /api/v1/instances?x=1 HTTP/1.1\r\nHost: hub\r\nAuthorization: Bearer forged\r\n"
            "X-Cwcli-Delegated-User: alice\r\nX-Cwcli-Delegated-Role: admin\r\nAccept: application/json\r\n\r\n"
        ).encode()
        code, out = _run(grant, request)
        assert code == 0
        status, body = _status_and_json(out)
        assert status == 200 and body["data"]["echo"] == "/api/v1/instances?x=1"
        (seen,) = daemon.requests
        assert seen["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert seen["headers"]["x-cwcli-delegated-user"] == f"alice@{HUB}"
        assert seen["headers"]["x-cwcli-delegated-role"] == "operator"
        assert seen["headers"]["x-cwcli-via"] == "hub"
        assert seen["headers"]["host"].startswith("127.0.0.1:")
        assert "cookie" not in seen["headers"]
        # The daemon's own bytes come back untouched; the hub decides what to relay.
        assert b"Set-Cookie: remote_session" in out

    def test_a_post_body_is_forwarded_whole(self, grant, daemon):
        payload = json.dumps({"verb": "start", "target": {"project": "p"}}).encode()
        request = (
            f"POST /api/v1/jobs HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        code, out = _run(grant, request)
        assert code == 0 and _status_and_json(out)[0] == 200
        (seen,) = daemon.requests
        assert seen["method"] == "POST" and seen["body"] == payload
        assert seen["headers"]["content-length"] == str(len(payload))

    def test_the_bridge_route_answers_the_cap_and_the_daemons_meta(self, grant, daemon):
        code, out = _run(grant, b"GET /api/v1/bridge HTTP/1.1\r\n\r\n")
        assert code == 0
        status, body = _status_and_json(out)
        assert status == 200
        data = body["data"]
        assert data["hub"] == HUB and data["max_role"] == "operator"
        assert data["bridge_protocol"] == bridge.BRIDGE_PROTOCOL
        assert data["daemon"]["protocol"] == PROTOCOL_VERSION and data["daemon_error"] is None
        assert [r["path"] for r in daemon.requests] == ["/api/v1/meta"]

    def test_a_daemon_that_is_down_is_reported_not_guessed(self, grant, tmp_path, daemon):
        daemon.close()  # the port in the state file no longer answers
        code, out = _run(grant, b"GET /api/v1/bridge HTTP/1.1\r\n\r\n")
        data = _status_and_json(out)[1]["data"]
        assert code == 0 and data["daemon"] is None and "not running" in data["daemon_error"]
        code, out = _run(grant, b"GET /api/v1/instances HTTP/1.1\r\n\r\n")
        assert code == 0 and _status_and_json(out)[0] == 503
        assert _error_code(out) == "bridge.daemon_down"

    def test_a_missing_state_file_means_the_daemon_is_down(self, grant, tmp_path):
        os.unlink(tmp_path / "ui.json")
        code, out = _run(grant, b"GET /api/v1/instances HTTP/1.1\r\n\r\n")
        assert _error_code(out) == "bridge.daemon_down"

    def test_a_daemon_that_is_down_is_started_once_the_way_cwcli_ui_does(self, tmp_path, daemon, monkeypatch):
        """Without ``--no-start`` the bridge starts the remote's own daemon (as a person
        running ``cwcli ui`` would) when nothing answers, once, then reads its state file;
        the hub never learns the token that file holds."""
        state = tmp_path / "ui.json"
        calls = []

        def fake_start(home, log_path):
            calls.append((home, log_path))
            state.write_text(json.dumps({"port": daemon.port, "token": TOKEN}))

        monkeypatch.setattr(bridge, "_start_daemon", fake_start)
        found, error = bridge.daemon_endpoint(str(tmp_path / "home"), str(state), start=True)
        assert found == {"port": daemon.port, "token": TOKEN} and error is None
        assert calls == [(str(tmp_path / "home"), str(tmp_path / "home" / "run" / "bridge.log"))]
        # Up now: a second call reads the state file and starts nothing.
        bridge.daemon_endpoint(str(tmp_path / "home"), str(state), start=True)
        assert len(calls) == 1
        # Refused to start: down is reported, not guessed.
        state.unlink()
        found, error = bridge.daemon_endpoint(str(tmp_path / "home"), str(state), start=False)
        assert found is None and "not running" in error and len(calls) == 1


def test_the_bridge_imports_only_the_stdlib_and_the_package_init():
    """One channel per request means one interpreter start per request: the bridge
    must never pull the command layer (typer, rich, docker) into the process."""
    script = (
        "import sys; import caffeinated_whale_cli.bridge; "
        "print(sorted(m for m in sys.modules if m.startswith('caffeinated_whale_cli'))); "
        "print(sorted(m for m in ('typer', 'rich', 'docker', 'toml', 'peewee', 'requests') if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    lines = done.stdout.strip().splitlines()
    assert lines[0] == "['caffeinated_whale_cli', 'caffeinated_whale_cli.bridge']"
    assert lines[1] == "[]"
