"""The remote side of a hub link: the forced command a hub's SSH key is restricted to.

``cwcli remote allow`` writes one line into this machine's ``authorized_keys``::

    restrict,command="<python> -m caffeinated_whale_cli.bridge --hub <name>
        --max-role <role> --home <cwcli home> --state <ui state file>
        --keys <authorized_keys> --key-fp <SHA256 of the key>" ssh-ed25519 AAAA... cwcli-hub:<name>

so the hub's key can run this module and nothing else (``restrict`` also turns off
port forwarding, agent forwarding, X11 and the pty). Each SSH channel the hub opens
carries exactly ONE HTTP/1.1 request on stdin; the bridge

1. refuses to be anything but a bridge: a command passed by the client
   (``SSH_ORIGINAL_COMMAND``) or stdin that is not an HTTP request exits 2;
2. re-reads the grant: ``authorized_keys`` must still carry this channel's own
   line, the same forced command with the key it names. sshd binds the command
   when a connection authenticates and a hub keeps its connection open, so a
   revoke, a new cap or a new key refuses every later channel of a connection
   that authenticated before, and the hub reconnects against the line as it is;
3. forwards only ``/api/v1/`` paths, never the session or hosts routes;
4. drops every credential and identity header the hub sent (``Authorization``,
   ``Cookie``, the delegation headers, ``Host``, ``Connection`` and whatever it
   names), refuses a folded header line and a delegation header that is sent twice
   or named in ``Connection``, lowers the claimed role to the cap in its own command
   line, writes the delegated user as ``<user>@<hub>`` with the hub name from its own
   command line, and adds this machine's own daemon token - the hub never holds it;
5. pipes the exchange to the loopback ``cwcli ui`` daemon and streams the
   daemon's response back verbatim, once the daemon's pid file shows the process
   that wrote the state file is still alive and still itself (the check ``cwcli ui``
   makes before reusing a daemon). ``GET /api/v1/bridge`` is answered here, with
   the cap and the daemon's ``/api/v1/meta``, and starts the daemon the way
   ``cwcli ui`` does when it is not running.

Stdlib only, and it imports nothing else from cwcli but the package ``__init__`` and
the stdlib-only ``utils.daemon_identity``: the hub opens a channel per request, so
this must start in milliseconds (importing the command layer costs a third of a second).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import select
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .utils import daemon_identity

BRIDGE_PROTOCOL = 1  # keep equal to core.protocol.BRIDGE_PROTOCOL (not imported: cost)
BRIDGE_MODULE = "caffeinated_whale_cli.bridge"
ROLES = ("viewer", "operator", "admin")
_RANK = {"viewer": 0, "operator": 1, "admin": 2}

_HEAD_LIMIT = 64 * 1024
_BODY_LIMIT = 1024 * 1024
_HEAD_TIMEOUT_S = 30.0
_DAEMON_READ_TIMEOUT_S = 120.0
_DAEMON_START_WAIT_S = 30.0

_REQUEST_LINE = re.compile(rb"^(GET|POST) (/api/v1/[\x21-\x7e]*) HTTP/1\.[01]$")
_GRANT_LINE = re.compile(r'restrict,command="([^"]*)" (\S+) (\S+) cwcli-hub:(\S+)')
_USER = re.compile(r"[A-Za-z0-9._+-]{1,80}")
# Headers the bridge never forwards: credentials, identity, routing, hop-by-hop,
# and the browser's own provenance (the daemon's same-origin guard then treats the
# request as the non-browser client it is).
_DROP = {
    "authorization",
    "cookie",
    "host",
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "origin",
    "referer",
    "x-cwcli-delegated-user",
    "x-cwcli-delegated-role",
}
_DROP_PREFIXES = ("proxy-", "sec-fetch-", "x-forwarded-")
_DELEGATED_USER = "x-cwcli-delegated-user"
_DELEGATED_ROLE = "x-cwcli-delegated-role"
_DELEGATION = {_DELEGATED_USER, _DELEGATED_ROLE}
# The paths a hub never reaches through the bridge: the remote's own sign-in, and
# its hosts (no hopping from one hub through another).
_REFUSED_PREFIXES = ("/api/v1/session", "/api/v1/hosts")


class BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ------------------------------------------------------------------ the grant


def _fingerprint(blob: str) -> str | None:
    """``SHA256:<base64>`` of a key blob, the way ``ssh-keygen -lf`` prints it."""
    try:
        digest = hashlib.sha256(base64.b64decode(blob, validate=True)).digest()
    except (binascii.Error, ValueError):
        return None
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def still_allowed(args: argparse.Namespace, argv: list[str]) -> bool:
    """Whether ``authorized_keys`` still carries the grant this channel runs under.

    That is this hub's line whose forced command is exactly this bridge's own
    command line (``argv``) and whose key is the one the command names
    (``--key-fp``). Any other line, a line for the hub that differs in cap, key or
    paths, or a key that merely mentions the hub's marker keeps nothing alive.
    Fails closed: an unreadable file is a missing grant.
    """
    try:
        with open(args.keys, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    for line in lines:
        match = _GRANT_LINE.fullmatch(line.strip())
        if match is None or match.group(4) != args.hub:
            continue
        try:
            command = shlex.split(match.group(1))
        except ValueError:
            continue
        if (
            command[1:3] == ["-m", BRIDGE_MODULE]
            and command[3:] == argv
            and _fingerprint(match.group(3)) == args.key_fp
        ):
            return True
    return False


# --------------------------------------------------------------- the request


def _read_head(fd: int, deadline: float) -> tuple[bytes, bytes]:
    """The request head up to and including the blank line, plus any body bytes
    read along with it. Bounded in size and time."""
    buf = b""
    while True:
        marker = buf.find(b"\r\n\r\n")
        if marker >= 0:
            return buf[: marker + 4], buf[marker + 4 :]
        marker = buf.find(b"\n\n")
        if marker >= 0:
            return buf[: marker + 2], buf[marker + 2 :]
        if len(buf) > _HEAD_LIMIT:
            raise BridgeError(431, "bridge.head_too_large", "request head too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError(408, "bridge.head_timeout", "no request arrived in time")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise BridgeError(408, "bridge.head_timeout", "no request arrived in time")
        chunk = os.read(fd, 65536)
        if not chunk:
            first = buf.replace(b"\r\n", b"\n").split(b"\n", 1)[0].strip()
            if not _REQUEST_LINE.match(first):
                raise BridgeError(400, "bridge.not_http", "expected an HTTP request")
            raise BridgeError(400, "bridge.truncated", "the request ended before its head")
        buf += chunk


def parse_request(head: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    """``(method, target, headers)`` from a request head, or a ``BridgeError``.

    Only ``GET`` and ``POST`` on an ``/api/v1/`` target are HTTP requests this bridge
    recognises; anything else - a shell command, a different method, a path
    outside the API - is refused before any header is looked at.
    """
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    match = _REQUEST_LINE.match(lines[0].strip())
    if match is None:
        raise BridgeError(400, "bridge.not_http", "expected an HTTP request")
    method = match.group(1).decode()
    target = match.group(2).decode()
    headers: list[tuple[str, str]] = []
    for raw in lines[1:]:
        if not raw.strip():
            continue
        if raw[:1] in (b" ", b"\t"):
            raise BridgeError(400, "bridge.bad_header", "a folded header line is not accepted")
        name, sep, value = raw.partition(b":")
        if not sep or not name.strip():
            raise BridgeError(400, "bridge.bad_header", "malformed header line")
        try:
            headers.append((name.strip().decode("ascii"), value.strip().decode("latin-1")))
        except UnicodeDecodeError as e:
            raise BridgeError(400, "bridge.bad_header", "malformed header line") from e
    return method, target, headers


def _cap_role(claimed: str | None, cap: str) -> str:
    """The role the daemon is told: the hub's claim, never above the cap, and a
    claim that is not a role at all reads as the lowest one."""
    claimed = (claimed or "").strip().lower()
    role = claimed if claimed in _RANK else "viewer"
    return role if _RANK[role] <= _RANK[cap] else cap


def _delegated_user(claimed: str | None, hub: str) -> str:
    """``<user>@<hub>``: the hub's bare username (a claim it may make) under the hub
    name from this bridge's own command line (a claim it may not)."""
    bare = (claimed or "").strip().split("@", 1)[0]
    if not _USER.fullmatch(bare):
        bare = "hub"
    return f"{bare}@{hub}"


def rewrite(
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    *,
    hub: str,
    cap: str,
    port: int,
    token: str,
    body_length: int,
) -> list[tuple[str, str]]:
    """The headers sent to the daemon for a hub's request.

    Every incoming credential, identity and hop-by-hop header is dropped, so the
    only identity the daemon sees is the one this function writes; a request
    whose ``Connection`` header names a delegation header is refused outright
    (the hop-by-hop stripping trick that once ran a request as the proxy itself),
    and so is one that sends a delegation header twice.
    """
    if target.startswith(_REFUSED_PREFIXES):
        raise BridgeError(
            403, "bridge.path_refused", f"{target.split('?')[0]} is not reachable through a hub"
        )
    hop_by_hop: set[str] = set()
    claimed: dict[str, str] = {}
    for name, value in headers:
        low = name.lower()
        if low == "connection":
            for token_name in value.split(","):
                named = token_name.strip().lower()
                if named in _DELEGATION:
                    raise BridgeError(
                        400,
                        "bridge.connection_header",
                        "a delegation header may not be named in Connection",
                    )
                if named:
                    hop_by_hop.add(named)
        elif low in _DELEGATION:
            if low in claimed:
                raise BridgeError(
                    400, "bridge.delegation_repeated", f"{name} may be sent only once"
                )
            claimed[low] = value
        elif low == "transfer-encoding":
            raise BridgeError(
                411, "bridge.length_required", "send a Content-Length, not Transfer-Encoding"
            )
    out: list[tuple[str, str]] = [("Host", f"127.0.0.1:{port}")]
    for name, value in headers:
        low = name.lower()
        if low in _DROP or low in hop_by_hop or low.startswith(_DROP_PREFIXES):
            continue
        if "\r" in value or "\n" in value:
            raise BridgeError(400, "bridge.bad_header", "malformed header line")
        out.append((name, value))
    out.extend(
        [
            ("Authorization", f"Bearer {token}"),
            ("X-Cwcli-Delegated-User", _delegated_user(claimed.get(_DELEGATED_USER), hub)),
            ("X-Cwcli-Delegated-Role", _cap_role(claimed.get(_DELEGATED_ROLE), cap)),
            ("Connection", "close"),
        ]
    )
    if method == "POST" or body_length:
        out.append(("Content-Length", str(body_length)))
    return out


def _content_length(headers: list[tuple[str, str]]) -> int:
    for name, value in headers:
        if name.lower() == "content-length":
            try:
                length = int(value)
            except ValueError as e:
                raise BridgeError(400, "bridge.bad_length", "invalid Content-Length") from e
            if length < 0:
                raise BridgeError(400, "bridge.bad_length", "invalid Content-Length")
            if length > _BODY_LIMIT:
                raise BridgeError(413, "bridge.body_too_large", "request body too large")
            return length
    return 0


def _read_body(fd: int, already: bytes, length: int, deadline: float) -> bytes:
    body = already[:length]
    while len(body) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError(408, "bridge.body_timeout", "the request body did not arrive in time")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise BridgeError(408, "bridge.body_timeout", "the request body did not arrive in time")
        chunk = os.read(fd, min(65536, length - len(body)))
        if not chunk:
            raise BridgeError(400, "bridge.truncated", "the request body ended early")
        body += chunk
    return body


# ---------------------------------------------------------------- the daemon


def _read_state(path: str) -> dict | None:
    """The daemon's port and token, only while the daemon that wrote them is alive and
    still itself: its pid file beside the state file (``ui.pid`` next to ``ui.json``,
    as ``cwcli ui`` writes them) must name a live process with the recorded start
    time. After the daemon dies the state file keeps its port, and whoever binds that
    port next must never receive the token or a hub's request."""
    if not daemon_identity.is_running(Path(os.path.splitext(path)[0] + ".pid")):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("port"), int) and data.get("token"):
        return data
    return None


def _meta(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/meta", timeout=3
        ) as r:  # noqa: S310
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _start_daemon(home: str, log_path: str) -> None:
    """``cwcli ui --no-browser`` the way a person would run it: a detached daemon on
    this machine's loopback, under this user's cwcli home. The hub never learns its
    token; the bridge reads it from the state file afterwards."""
    env = {**os.environ, "CWCLI_HOME": home, "CWCLI_NO_UPDATE_CHECK": "1"}
    env.pop("SSH_ORIGINAL_COMMAND", None)
    try:
        os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        fd = os.open(os.devnull, os.O_WRONLY)
    with os.fdopen(fd, "a") as log:
        subprocess.run(
            [sys.executable, "-m", "caffeinated_whale_cli", "ui", "--no-browser"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            timeout=_DAEMON_START_WAIT_S,
            check=False,
        )


def daemon_endpoint(
    home: str, state_path: str, *, start: bool = True
) -> tuple[dict | None, str | None]:
    """``(state, error)``: the daemon's port and token when it answers, else why not.
    Starts it once when it is not running and ``start`` is set."""
    state = _read_state(state_path)
    if state is not None and _meta(state["port"]) is not None:
        return state, None
    if not start:
        return None, "the cwcli ui daemon is not running on this machine"
    try:
        _start_daemon(home, os.path.join(home, "run", "bridge.log"))
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"could not start the cwcli ui daemon: {e}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = _read_state(state_path)
        if state is not None and _meta(state["port"]) is not None:
            return state, None
        time.sleep(0.3)
    return None, "the cwcli ui daemon did not come up on this machine"


# --------------------------------------------------------------- responses


def _json_response(status: int, payload: dict, extra: str = "") -> bytes:
    body = json.dumps(payload).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 408: "Request Timeout",
              411: "Length Required", 413: "Payload Too Large", 431: "Request Header Fields Too Large",
              502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "Error")  # fmt: skip
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        f"{extra}"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body


def _error(status: int, code: str, message: str) -> bytes:
    """The bridge's own refusal; ``X-Cwcli-Bridge-Error`` tells the hub it came from here
    and not from the daemon (a ``bridge.revoked`` makes the hub reconnect)."""
    return _json_response(
        status,
        {"errors": [{"type": "precondition", "title": code, "message": message, "hint": None}]},
        extra=f"X-Cwcli-Bridge-Error: {code}\r\n",
    )


def _bridge_answer(args: argparse.Namespace) -> bytes:
    state = _read_state(args.state)
    meta = _meta(state["port"]) if state is not None else None
    error = None
    if meta is None:
        state, error = daemon_endpoint(args.home, args.state, start=not args.no_start)
        meta = _meta(state["port"]) if state is not None else None
    return _json_response(
        200,
        {
            "data": {
                "bridge_protocol": BRIDGE_PROTOCOL,
                "bridge_version": __version__,
                "hub": args.hub,
                "max_role": args.max_role,
                "daemon": meta,
                "daemon_error": (
                    (error or "the cwcli ui daemon is not running on this machine")
                    if meta is None
                    else None
                ),
            }
        },
    )


def _pipe(
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes,
    args: argparse.Namespace,
    out,
) -> None:
    """Forward one request to the loopback daemon and stream its answer back.

    The daemon is connected to directly from the state file (no probe first, so a
    forward costs one round trip); only when nothing answers on that port is the
    daemon started the way ``cwcli ui`` does, once, and the connect retried.
    """
    if target.startswith(_REFUSED_PREFIXES):
        raise BridgeError(
            403, "bridge.path_refused", f"{target.split('?')[0]} is not reachable through a hub"
        )
    state = _read_state(args.state)
    conn = _connect(state["port"]) if state is not None else None
    if conn is None:
        state, error = daemon_endpoint(args.home, args.state, start=not args.no_start)
        conn = _connect(state["port"]) if state is not None else None
        if conn is None:
            out.write(
                _error(503, "bridge.daemon_down", error or "the cwcli ui daemon is not running")
            )
            out.flush()
            return
    assert state is not None
    forwarded = rewrite(
        method,
        target,
        headers,
        hub=args.hub,
        cap=args.max_role,
        port=state["port"],
        token=state["token"],
        body_length=len(body),
    )
    head = (
        f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in forwarded) + "\r\n"
    )
    try:
        conn.sendall(head.encode("latin-1") + body)
        conn.settimeout(_DAEMON_READ_TIMEOUT_S)
        while True:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            out.write(chunk)
            out.flush()
    except BrokenPipeError:
        pass  # the hub closed its channel; nothing more is owed
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _connect(port: int) -> socket.socket | None:
    try:
        return socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        return None


# ------------------------------------------------------------------- main


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m caffeinated_whale_cli.bridge", add_help=False)
    p.add_argument("--hub", required=True)
    p.add_argument("--max-role", required=True, choices=ROLES)
    p.add_argument("--home", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--key-fp", required=True)
    return p


def main(
    argv: list[str] | None = None, *, stdin_fd: int | None = None, out=None, environ=None
) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    out = sys.stdout.buffer if out is None else out
    fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    if env.get("SSH_ORIGINAL_COMMAND"):
        sys.stderr.write("bridge: this key is restricted to the cwcli bridge; it runs no command\n")
        return 2
    try:
        deadline = time.monotonic() + _HEAD_TIMEOUT_S
        head, rest = _read_head(fd, deadline)
        method, target, headers = parse_request(head)
    except BridgeError as e:
        if e.code in ("bridge.not_http", "bridge.truncated", "bridge.head_timeout"):
            sys.stderr.write(f"bridge: {e.message}\n")
            return 2
        out.write(_error(e.status, e.code, e.message))
        out.flush()
        return 1
    try:
        if not still_allowed(args, argv):
            raise BridgeError(
                403, "bridge.revoked", f"this machine no longer accepts hub {args.hub}"
            )
        length = _content_length(headers)
        body = _read_body(fd, rest, length, deadline) if length else b""
        if target.split("?", 1)[0] == "/api/v1/bridge" and method == "GET":
            out.write(_bridge_answer(args))
            out.flush()
            return 0
        _pipe(method, target, headers, body, args, out)
    except BridgeError as e:
        try:
            out.write(_error(e.status, e.code, e.message))
            out.flush()
        except BrokenPipeError:
            pass
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
