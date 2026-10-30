"""Remote hosts: other machines' cwcli daemons shown and driven from this web UI (UI-pure).

This machine is the **hub**. It keeps one SSH key pair (``<cwcli home>/keys/hub_ed25519``,
0600, never returned by any API) and a store of the machines it may reach
(``<cwcli home>/hosts.json``, 0600). Each **remote** machine runs its own ordinary
``cwcli ui`` daemon on loopback and nothing new; its owner granted this hub with
``cwcli remote allow`` (``core.remote_access``), which restricts the hub's public key
to the bridge command (``caffeinated_whale_cli.bridge``) and caps it at a role.

Trust, both ways, pinned at enrolment and never on first use:

* the hub trusts a remote because the SSH host key it presents matches the key
  pinned when the host was added (``StrictHostKeyChecking=yes`` against the hub's own
  ``known_hosts``, keyed by a per-host alias); a changed key is a hard stop that an
  Admin clears by pinning the new key, never by the link reconnecting on its own;
* the remote trusts the hub only through the one restricted line in its
  ``authorized_keys``, and only up to the role cap written in that line; the bridge
  there re-reads the grant on every channel and lowers whatever role the hub claims.

A **transport** answers one question: a byte stream to host X's cwcli API
(:class:`Transport`: ``open``, ``close``). SSH dial-in is the one shipped here
(:class:`SshTransport`); reverse SSH and an overlay network would plug into the
same interface, which is why hosts carry a ``transport`` id and several transport
integrations may be on at once (``core.integrations.providers``). Over that
stream the hub speaks plain HTTP/1.1, one request per channel, and OpenSSH
multiplexes the channels over one connection per host (``ControlMaster``).

Presence is reported, never acted on: a host that stops answering is ``unreachable``
with its last-known instances kept (each shown ``unknown``) and a last-seen time, the
page disables actions on it, and nothing here starts, stops or retries work
because a host went quiet. The version rule: actions are allowed only while the
remote's ``protocol`` equals this daemon's; a different one keeps the read-only
views and names the side to update. On a shared install only the cwcli service
account's daemon runs links in the shared home, because it alone can read the hub key:
a group member's own web UI there lists every host as ``service_account_only`` and
dials none of them. A member opted back to a private home (``CWCLI_HOME``) owns that
home's key and store, so it runs a hub of its own as a per-user install does.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import random
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from ..utils import config_utils, shared_home
from . import integrations
from .errors import CwcliError, ErrorKind
from .protocol import BRIDGE_PROTOCOL, PROTOCOL_VERSION
from .schedules import StoreLock

HOST_TRANSPORT = "host_transport"
SSH_TRANSPORT = "ssh"
INTEGRATION_ID = "remote_ssh"

# Link states (the failure table in the design): what the page shows per host.
CONNECTING = "connecting"
CONNECTED = "connected"
UNREACHABLE = "unreachable"
AUTH_REFUSED = "auth_refused"
HOST_KEY_CHANGED = "host_key_changed"
DAEMON_DOWN = "daemon_down"
VERSION_MISMATCH = "version_mismatch"
TRANSPORT_OFF = "transport_off"
SERVICE_ACCOUNT_ONLY = "service_account_only"
LINK_STATES = (
    CONNECTING,
    CONNECTED,
    UNREACHABLE,
    AUTH_REFUSED,
    HOST_KEY_CHANGED,
    DAEMON_DOWN,
    VERSION_MISMATCH,
    TRANSPORT_OFF,
    SERVICE_ACCOUNT_ONLY,
)
TRANSPORT_OFF_REASON = "The Remote hosts over SSH integration is off."
SERVICE_ACCOUNT_REASON = (
    "Remote hosts run from the cwcli service account's web UI; only it can read the hub key."
)
# The states in which the hub forwards a request at all; actions need CONNECTED.
READABLE_STATES = (CONNECTED, VERSION_MISMATCH)

ROLES = ("viewer", "operator", "admin")
DEFAULT_CAP = "operator"
PAIRING_PREFIX = "cwcli-remote:v1:"
KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)

KEEPALIVE_S = 15.0  # the daemon's SSE keepalive; the stream idle limit is a multiple
STREAM_IDLE_S = 4 * KEEPALIVE_S
CONNECT_TIMEOUT_S = 25.0  # ssh ConnectTimeout (10s) plus auth and the bridge start-up
# A forwarded request's answer: the connect above, then as long as the bridge itself waits
# for its daemon (a backup download's head comes only after the whole tar is written).
FORWARD_TIMEOUT_S = CONNECT_TIMEOUT_S + 120.0
MAX_BACKOFF_S = 60.0
DROP_WINDOW_S = 600.0
_HEAD_LIMIT = 64 * 1024
_BODY_LIMIT = 50 * 1024 * 1024
_FRAME_LIMIT = 8 * 1024 * 1024  # one SSE frame from a remote; the snapshot is the largest

_lock = threading.RLock()
_change_listeners: list[Callable[[], None]] = []

_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}[A-Za-z0-9])?")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,31}")
_HUB = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
# The request line the remote's bridge reads, and the header fields it can be sent.
_TARGET = re.compile(r"/api/v1/[\x21-\x7e]*")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE = re.compile(r"[\x20-\x7e]*")


# ----------------------------------------------------------------- validation


def _usage(code: str, message: str, hint: str | None = None) -> CwcliError:
    return CwcliError(ErrorKind.USAGE, code, message, hint=hint)


def check_hostname(address: str) -> str:
    """A hostname or IP literal ``ssh`` may be handed as a destination: letters,
    digits, dots, dashes and underscores, never a leading dash (the shape that once
    turned a "host" of ``-oProxyCommand=...`` into a command), or a valid IP."""
    value = (address or "").strip()
    if not value:
        raise _usage("host.address", "An address is required.")
    if "\n" not in value and "\r" not in value:
        try:
            ipaddress.ip_address(value.strip("[]"))
            return value.strip("[]")
        except ValueError:
            pass
    if value.startswith("-") or not _HOSTNAME.fullmatch(value):
        raise _usage("host.address", f"Not a hostname or IP address: {value!r}.")
    return value


def check_user(user: str) -> str:
    value = (user or "").strip()
    if not value or value.startswith("-") or not _USER.fullmatch(value):
        raise _usage("host.user", f"Not an SSH user name: {user!r}.")
    return value


def check_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise _usage("host.port", f"Not a port: {port!r}.")
    try:
        value = int(port)
    except ValueError as e:
        raise _usage("host.port", f"Not a port: {port!r}.") from e
    if not 1 <= value <= 65535:
        raise _usage("host.port", f"Not a port: {port!r}.")
    return value


def check_hub_name(name: str) -> str:
    value = (name or "").strip()
    if not value or not _HUB.fullmatch(value):
        raise _usage(
            "host.hub_name",
            f"Not a hub name: {name!r} (letters, digits, dots, dashes, underscores; up to 64).",
        )
    return value


def check_role(role: str) -> str:
    value = (role or "").strip().lower()
    if value not in ROLES:
        raise _usage("host.role", f"Not a role: {role!r} (viewer, operator or admin).")
    return value


def check_name(name: str) -> str:
    value = (name or "").strip()
    if not value or not _NAME.fullmatch(value):
        raise _usage(
            "host.name", f"Not a host name: {name!r} (letters, digits, spaces, dots, dashes)."
        )
    return value


def parse_public_key(text: str) -> tuple[str, str]:
    """``(type, base64 blob)`` of one OpenSSH public key line, or a USAGE error.

    The line's comment and any options are dropped: a key that arrives with
    ``command=`` or a second line in it must never reach ``authorized_keys`` as is.
    The blob is decoded and must embed the type it claims, so a pasted fragment or
    an unrelated base64 string is refused too.
    """
    parts = (text or "").strip().split()
    if len(parts) < 2:
        raise _usage("host.key", "Not an SSH public key (expected '<type> <base64> [comment]').")
    kind, blob = parts[0], parts[1]
    if kind not in KEY_TYPES:
        raise _usage("host.key", f"Unsupported key type {kind!r}; use an ed25519 key.")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as e:
        raise _usage("host.key", "The key's base64 part does not decode.") from e
    if len(raw) < 4:
        raise _usage("host.key", "The key's base64 part is too short.")
    n = int.from_bytes(raw[:4], "big")
    if raw[4 : 4 + n] != kind.encode("ascii"):
        raise _usage("host.key", "The key's base64 part does not match its type.")
    return kind, blob


def fingerprint(kind: str, blob: str) -> str:
    """``SHA256:<base64>`` the way ``ssh-keygen -lf`` prints it."""
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40]
    return slug or "host"


# ------------------------------------------------------------- the pairing


def make_pairing(fields: dict[str, Any]) -> str:
    """The string a remote prints and an Admin pastes into the hub: a compact,
    self-describing record of how to reach that machine and the host key to pin."""
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return PAIRING_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_pairing(text: str) -> dict[str, Any]:
    """The validated fields of a pairing string. Every field is checked here, so a
    pairing string a stranger wrote can carry nothing ``ssh`` would misread."""
    value = (text or "").strip()
    if not value.startswith(PAIRING_PREFIX):
        raise _usage(
            "host.pairing",
            "Not a pairing string.",
            hint="Run `cwcli remote allow ...` on the other machine and paste what it prints.",
        )
    body = value[len(PAIRING_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise _usage("host.pairing", "The pairing string is damaged.") from e
    if not isinstance(data, dict):
        raise _usage("host.pairing", "The pairing string is damaged.")
    out: dict[str, Any] = {
        "user": check_user(str(data.get("user") or "")),
        "address": check_hostname(str(data.get("address") or "")),
        "port": check_port(data.get("port") or 22),
        "name": check_name(str(data.get("name") or data.get("address") or "")),
        "hub": check_hub_name(str(data.get("hub") or "")),
        "cap": check_role(str(data.get("cap") or DEFAULT_CAP)),
        "host_key": None,
    }
    key = data.get("host_key")
    if key:
        kind, blob = parse_public_key(str(key))
        out["host_key"] = f"{kind} {blob}"
    return out


# ----------------------------------------------------------------- the store


def store_path() -> Path:
    return config_utils.cwcli_home() / "hosts.json"


def keys_dir() -> Path:
    return config_utils.cwcli_home() / "keys"


def hub_key_path() -> Path:
    return keys_dir() / "hub_ed25519"


def known_hosts_path() -> Path:
    return keys_dir() / "known_hosts"


@dataclass(frozen=True, slots=True)
class HostRecord:
    id: str
    name: str
    transport: str
    address: str
    port: int
    user: str
    host_key: str  # "<type> <base64>", the pinned SSH host key
    added_at: float
    added_by: str

    @property
    def key_alias(self) -> str:
        """The ``HostKeyAlias`` this host is pinned under in the hub's ``known_hosts``,
        so the pin never depends on how the address or port is spelled."""
        return f"cwcli-host-{self.id}"

    def fingerprint(self) -> str:
        kind, blob = self.host_key.split(" ", 1)
        return fingerprint(kind, blob)


def _load() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "hosts": {}}
    except (OSError, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "hosts.unreadable", f"{store_path()} is unreadable: {e}"
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), dict):
        raise CwcliError(
            ErrorKind.PRECONDITION, "hosts.unreadable", f"{store_path()} is not a hosts store"
        )
    return data


def write_state(path: Path, text: str) -> None:
    """Replace a cwcli state file atomically, 0600 (group-shared 0660 on a shared install,
    where the service account's daemon reads what a group member's CLI wrote)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, shared_home.file_mode(0o600))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, shared_home.file_mode(0o600))
    shared_home.apply_group(path)


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_state(path, json.dumps(data, indent=1, sort_keys=True))
    _write_known_hosts(data)


def _record(host_id: str, rec: dict) -> HostRecord:
    return HostRecord(
        id=host_id,
        name=str(rec.get("name") or host_id),
        transport=str(rec.get("transport") or SSH_TRANSPORT),
        address=str(rec["address"]),
        port=int(rec.get("port") or 22),
        user=str(rec["user"]),
        host_key=str(rec["host_key"]),
        added_at=float(rec.get("added_at") or 0),
        added_by=str(rec.get("added_by") or ""),
    )


def list_hosts() -> list[HostRecord]:
    with _lock:
        data = _load()
    return [_record(k, v) for k, v in sorted(data["hosts"].items())]


def get_host(host_id: str) -> HostRecord:
    with _lock:
        rec = _load()["hosts"].get(host_id)
    if rec is None:
        raise CwcliError(ErrorKind.NOT_FOUND, "host.unknown", f"No remote host '{host_id}'.")
    return _record(host_id, rec)


def store_mtime() -> float:
    try:
        return store_path().stat().st_mtime
    except OSError:
        return 0.0


def _write_known_hosts(data: dict) -> None:
    """The hub's own pins, regenerated from the store: one line per host under its
    alias. Never the user's ``~/.ssh/known_hosts``, and never anything ssh learned
    by itself (``UpdateHostKeys`` is off and ``StrictHostKeyChecking`` is on)."""
    shared_home.ensure_private_dir(keys_dir())
    lines = [
        f"cwcli-host-{host_id} {rec['host_key']}\n"
        for host_id, rec in sorted(data["hosts"].items())
        if rec.get("host_key")
    ]
    write_state(known_hosts_path(), "".join(lines))


def add_host(
    pairing: str | None = None,
    *,
    name: str | None = None,
    address: str | None = None,
    port: int | None = None,
    user: str | None = None,
    host_key: str | None = None,
    transport: str = SSH_TRANSPORT,
    by: str = "",
    now: float | None = None,
) -> HostRecord:
    """Pin a remote machine. The pairing string from ``cwcli remote allow`` fills the
    fields; explicit values override it (the address the remote guessed may not be
    the one this hub reaches it by). Without a host key nothing is added: the hub
    never trusts a machine on first contact."""
    now = time.time() if now is None else now
    fields = parse_pairing(pairing) if pairing else {}
    if transport != SSH_TRANSPORT:
        raise _usage("host.transport", f"Unknown transport {transport!r}; only 'ssh' ships.")
    final_address = check_hostname(address or fields.get("address") or "")
    final_user = check_user(user or fields.get("user") or "")
    final_port = check_port(port if port is not None else fields.get("port") or 22)
    final_name = check_name(name or fields.get("name") or final_address)
    key_text = host_key or fields.get("host_key")
    if not key_text:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "host.key_required",
            "The remote's SSH host key is required; nothing is trusted on first contact.",
            hint="Run `cwcli remote allow` on the remote (it reads /etc/ssh/ssh_host_ed25519_key.pub), "
            "or paste that file's contents as the host key.",
        )
    kind, blob = parse_public_key(key_text)
    with _lock, StoreLock(store_path()):
        data = _load()
        base = slugify(final_name)
        host_id, n = base, 2
        while host_id in data["hosts"]:
            host_id, n = f"{base}-{n}", n + 1
        for other_id, other in data["hosts"].items():
            if (
                other.get("address") == final_address
                and int(other.get("port") or 22) == final_port
                and other.get("user") == final_user
            ):
                raise CwcliError(
                    ErrorKind.CONFLICT,
                    "host.exists",
                    f"{final_user}@{final_address}:{final_port} is already added as '{other_id}'.",
                )
        data["hosts"][host_id] = {
            "name": final_name,
            "transport": transport,
            "address": final_address,
            "port": final_port,
            "user": final_user,
            "host_key": f"{kind} {blob}",
            "added_at": now,
            "added_by": by,
        }
        _save(data)
        rec = _record(host_id, data["hosts"][host_id])
    notify_change()
    return rec


def remove_host(host_id: str) -> HostRecord:
    with _lock, StoreLock(store_path()):
        data = _load()
        rec = data["hosts"].pop(host_id, None)
        if rec is None:
            raise CwcliError(ErrorKind.NOT_FOUND, "host.unknown", f"No remote host '{host_id}'.")
        _save(data)
    notify_change()
    return _record(host_id, rec)


def repin_host(host_id: str, host_key: str) -> HostRecord:
    """Pin a new host key for a host whose key changed (an Admin's explicit act)."""
    kind, blob = parse_public_key(host_key)
    with _lock, StoreLock(store_path()):
        data = _load()
        rec = data["hosts"].get(host_id)
        if rec is None:
            raise CwcliError(ErrorKind.NOT_FOUND, "host.unknown", f"No remote host '{host_id}'.")
        rec["host_key"] = f"{kind} {blob}"
        _save(data)
        out = _record(host_id, rec)
    notify_change()
    return out


# ------------------------------------------------------------ hub identity


def not_service_account() -> bool:
    """On a shared install, whether this process works in the shared home as someone other
    than the cwcli service account, whose daemon holds the links there (its key, its pins,
    its control sockets). A private home (``CWCLI_HOME`` always wins) is its owner's own."""
    state = shared_home.state_dir()
    if state is None or not hasattr(os, "getuid"):
        return False
    if config_utils.cwcli_home().resolve() != state.resolve():
        return False
    service_uid = shared_home.service_uid()
    return service_uid is not None and os.getuid() != service_uid


def shared_mode_note() -> str | None:
    """What a hub command run by someone else on a shared install should say."""
    if not not_service_account():
        return None
    return (
        "This is a shared install: remote hosts run from the cwcli service account's web UI, "
        "with its key. A check from this account is not what that web UI sees; run hub "
        "commands as the service account, or use Settings > Remote hosts in its web UI."
    )


def default_hub_name() -> str:
    host = socket.gethostname().split(".")[0]
    return f"hub-{slugify(host)}"


def hub_name() -> str:
    """How this machine names itself to the remotes: the integration's ``hub_name``
    when set, else ``hub-<hostname>``."""
    try:
        name = str(integrations.settings_of(INTEGRATION_ID).get("hub_name") or "")
    except CwcliError:
        name = ""
    return name or default_hub_name()


def hub_public_key() -> str | None:
    """The hub's public key line, or None until the key exists. Never the private half."""
    try:
        return hub_key_path().with_suffix(".pub").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def ensure_hub_key(name: str | None = None) -> str:
    """Create the hub's key pair once (``ssh-keygen``, Ed25519, no passphrase: the
    daemon connects with nobody present) and return the public key line."""
    existing = hub_public_key()
    if existing:
        return existing
    if not_service_account():
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "hosts.not_service_account",
            "This is a shared install: the hub key belongs to the cwcli service account, whose "
            "daemon dials the remotes, and ssh refuses a key that account cannot read alone.",
            hint="Run this command as the cwcli service account, or turn on the Remote hosts "
            "over SSH integration in that account's web UI (its test creates the key).",
        )
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "hosts.ssh_keygen_missing",
            "ssh-keygen was not found; install OpenSSH on this machine.",
        )
    shared_home.ensure_private_dir(keys_dir())
    path = hub_key_path()
    comment = f"cwcli-hub:{check_hub_name(name or hub_name())}"
    done = subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0 and not path.exists():
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "hosts.keygen_failed",
            f"ssh-keygen could not create the hub key: {done.stderr.strip() or done.returncode}",
        )
    os.chmod(path, 0o600)
    key = hub_public_key()
    if not key:
        raise CwcliError(
            ErrorKind.PRECONDITION, "hosts.keygen_failed", "The hub key was not written."
        )
    return key


def allow_command(cap: str = DEFAULT_CAP) -> str:
    """The one command a remote's owner runs to grant this hub (the hub's own public
    key quoted inside it)."""
    key = hub_public_key() or "<run the Remote hosts integration test first>"
    return f'cwcli remote allow --hub {hub_name()} --key "{key}" --max-role {cap}'


# ------------------------------------------------------- HTTP over a channel


class LinkError(Exception):
    """A request could not reach the remote's API; ``state`` is the link state it
    evidences (``unreachable``, ``auth_refused``, ``host_key_changed``, ``daemon_down``)."""

    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class Channel(Protocol):
    """A byte stream to one remote's bridge: write the request, read the response."""

    def write(self, data: bytes) -> None: ...
    def close_write(self) -> None: ...
    def read(self, size: int, timeout: float) -> bytes | None:
        """Up to ``size`` bytes; ``b""`` at end of stream; None when nothing arrived
        within ``timeout`` seconds."""
        ...

    def failure(self) -> LinkError | None:
        """After the stream ended with no HTTP answer: why, as a link state."""
        ...

    def cancel(self) -> None:
        """End the stream from another thread; the thread reading it sees the end and
        closes it."""
        ...

    def close(self) -> None: ...


class Transport(Protocol):
    """Give me a byte stream to host X's cwcli API."""

    id: str

    def open(self, host: HostRecord) -> Channel: ...
    def close(self, host: HostRecord) -> None: ...


class HttpResponse:
    """One response read off a channel: the head now, the body on demand."""

    def __init__(
        self,
        channel: Channel,
        status: int,
        reason: str,
        headers: list[tuple[str, str]],
        buffered: bytes,
    ) -> None:
        self._channel = channel
        self.status = status
        self.reason = reason
        self.headers = headers
        self._buffered = buffered
        self._done = False
        length = self.header("content-length")
        self.content_length = int(length) if length is not None and length.isdigit() else None

    def header(self, name: str) -> str | None:
        low = name.lower()
        return next((v for k, v in self.headers if k.lower() == low), None)

    def iter_body(self, *, idle_timeout: float = STREAM_IDLE_S) -> Iterator[bytes]:
        """The body as it arrives. Ends at ``Content-Length`` or the stream's end; a
        stream silent for ``idle_timeout`` seconds (four missed keepalives) is lost."""
        remaining = self.content_length
        if self._buffered:
            chunk, self._buffered = self._buffered, b""
            if remaining is not None:
                chunk, remaining = chunk[:remaining], remaining - min(len(chunk), remaining)
            if chunk:
                yield chunk
        while remaining is None or remaining > 0:
            piece = self._channel.read(
                65536 if remaining is None else min(65536, remaining), idle_timeout
            )
            if piece is None:
                raise LinkError(UNREACHABLE, "the remote stopped answering (no keepalive)")
            if not piece:
                if remaining is not None and remaining > 0:
                    raise LinkError(UNREACHABLE, "the response ended early")
                break
            if remaining is not None:
                remaining -= len(piece)
            yield piece
        self._done = True

    def read(self, limit: int = _BODY_LIMIT) -> bytes:
        out = bytearray()
        for chunk in self.iter_body():
            out.extend(chunk)
            if len(out) > limit:
                raise LinkError(UNREACHABLE, "the response is larger than the hub accepts")
        return bytes(out)

    def cancel(self) -> None:
        self._channel.cancel()

    def close(self) -> None:
        self._channel.close()


def _parse_head(head: bytes) -> tuple[int, str, list[tuple[str, str]]]:
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    parts = lines[0].decode("latin-1").split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise LinkError(UNREACHABLE, f"the remote did not answer with HTTP ({lines[0][:60]!r})")
    headers: list[tuple[str, str]] = []
    for raw in lines[1:]:
        if not raw.strip():
            continue
        name, sep, value = raw.partition(b":")
        if sep:
            headers.append((name.strip().decode("latin-1"), value.strip().decode("latin-1")))
    return int(parts[1]), parts[2] if len(parts) > 2 else "", headers


def encode_request(
    method: str, target: str, headers: list[tuple[str, str]], body: bytes = b""
) -> bytes:
    """One HTTP/1.1 request as bytes, or a USAGE error for a request the remote's bridge
    would not read as exactly this one, so it is refused before ssh starts: a target
    outside the bridge's request line, a header name that is not an RFC 7230 token, a
    value outside printable ASCII (a CR or LF would end the line early and smuggle a
    header of its own past every check the hub made), or a delegation header sent twice."""
    if method not in ("GET", "POST") or not _TARGET.fullmatch(target):
        raise _usage("host.bad_request", "This path cannot be sent to a remote host.")
    for name, value in headers:
        if not _HEADER_NAME.fullmatch(name) or not _HEADER_VALUE.fullmatch(value):
            raise _usage(
                "host.bad_request", f"The {name!r} header cannot be sent to a remote host."
            )
    delegation = [n.lower() for n, _v in headers if n.lower().startswith("x-cwcli-delegated-")]
    if len(delegation) != len(set(delegation)):
        raise _usage("host.bad_request", "A delegation header can be sent to a remote only once.")
    lines = [f"{method} {target} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers]
    if body or method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def exchange(
    channel: Channel, request: bytes, *, timeout: float = CONNECT_TIMEOUT_S
) -> HttpResponse:
    """Send one encoded request (``encode_request``) down the channel and read the
    response head.

    A channel that ends before any head arrived (ssh could not connect, the key
    was refused, the host key mismatched) raises the channel's own ``failure``.
    """
    try:
        channel.write(request)
        channel.close_write()
    except OSError as e:
        failure = channel.failure()  # before close(): the exit code and stderr are read here
        channel.close()
        raise failure or LinkError(UNREACHABLE, f"could not send the request: {e}") from e
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        marker = buf.find(b"\r\n\r\n")
        if marker >= 0:
            head, rest = buf[:marker], buf[marker + 4 :]
            break
        if len(buf) > _HEAD_LIMIT:
            channel.close()
            raise LinkError(UNREACHABLE, "the remote's response head is too large")
        chunk = channel.read(65536, max(0.0, deadline - time.monotonic()))
        if chunk is None:
            channel.close()
            raise LinkError(UNREACHABLE, "the remote did not answer in time")
        if not chunk:
            failure = channel.failure()  # before close(): the exit code and stderr are read here
            channel.close()
            raise failure or LinkError(
                UNREACHABLE, "the remote closed the connection before answering"
            )
        buf += chunk
    status, reason, response_headers = _parse_head(head)
    return HttpResponse(channel, status, reason, response_headers, rest)


def _ask(
    transport: Transport,
    host: HostRecord,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes = b"",
    *,
    timeout: float = CONNECT_TIMEOUT_S,
) -> HttpResponse:
    """One request to a host's API through its transport, its head awaited ``timeout``
    seconds.

    The request is encoded before anything is dialled, so one the bridge would refuse
    is a USAGE error with no ssh started. sshd binds the bridge's command line when a
    connection authenticates, and the bridge refuses (``bridge.revoked``) a channel whose
    grant line is gone or changed: on that answer the connection is dropped and the
    request asked once more on a fresh one, which authenticates against the grant as
    it is now. The bridge refuses before it forwards anything to the daemon.
    """
    request = encode_request(method, target, headers, body)
    resp = exchange(transport.open(host), request, timeout=timeout)
    if resp.status == 403 and resp.header("x-cwcli-bridge-error") == "bridge.revoked":
        resp.close()
        transport.close(host)
        resp = exchange(transport.open(host), request, timeout=timeout)
    return resp


# ------------------------------------------------------------ the SSH transport


def control_dir() -> Path:
    """Where the per-host ``ControlMaster`` sockets live: short, because a Unix socket
    path is capped at ~108 bytes and ``ControlPath`` takes a 40-character hash."""
    base = (
        Path("/tmp")
        if Path("/tmp").is_dir() and os.access("/tmp", os.W_OK)
        else Path(tempfile.gettempdir())
    )
    uid = os.getuid() if hasattr(os, "getuid") else None
    path = base / f"cwcli-hub-{uid if uid is not None else 'u'}"
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as e:
        raise LinkError(UNREACHABLE, f"could not create {path}: {e}") from e
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or (uid is not None and info.st_uid != uid):
        raise LinkError(
            UNREACHABLE,
            f"{path} is not a directory of this user; ssh's control sockets cannot live there",
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def classify_ssh_failure(returncode: int | None, stderr: str) -> LinkError:
    """What an ssh exit means for the link, from the one line ssh prints."""
    text = stderr.strip()
    last = text.splitlines()[-1] if text else ""
    low = text.lower()
    if "identification has changed" in low or "host key verification failed" in low:
        return LinkError(HOST_KEY_CHANGED, "the host key does not match the pinned key")
    if "permission denied" in low:
        return LinkError(AUTH_REFUSED, "this host no longer accepts this hub's key")
    if "restricted to the cwcli bridge" in low:
        return LinkError(AUTH_REFUSED, last)
    return LinkError(UNREACHABLE, last or f"ssh exited {returncode}")


class SshChannel:
    """One ``ssh`` process: its stdin carries the request, its stdout the response."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._stdout_fd = proc.stdout.fileno()  # type: ignore[union-attr]

    def write(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def close_write(self) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.close()
        except OSError:
            pass

    def read(self, size: int, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self._stdout_fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        try:
            return os.read(self._stdout_fd, size)
        except OSError:
            return b""

    def failure(self) -> LinkError | None:
        try:
            code = self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            code = self._proc.wait()
        err = ""
        if self._proc.stderr is not None:
            try:
                err = self._proc.stderr.read(8192).decode("utf-8", "replace")
            except (OSError, ValueError):  # a closed pipe reads as no message, never a crash
                err = ""
        return classify_ssh_failure(code, err)

    def cancel(self) -> None:
        """Kill ssh, and only that: its stdout then reads as ended, and the reader closes
        the pipe (closing it here would free the descriptor while the reader still
        selects on it)."""
        if self._proc.poll() is None:
            self._proc.kill()

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class SshTransport:
    """The hub dials each remote with the system ``ssh``: the hub key only, the hub's
    own ``known_hosts`` with strict checking, batch mode (never a prompt), keepalives
    that notice a dead link in about 45 seconds, and one multiplexed connection per
    host. The destination is passed after ``--`` and only after validation."""

    id = SSH_TRANSPORT

    def __init__(
        self,
        *,
        ssh: str | None = None,
        key_path: Path | None = None,
        known_hosts: Path | None = None,
        control: Path | None = None,
    ) -> None:
        self._ssh = ssh
        self._key = key_path
        self._known_hosts = known_hosts
        self._control = control

    def _binary(self) -> str:
        found = self._ssh or shutil.which("ssh")
        if not found:
            raise LinkError(UNREACHABLE, "ssh was not found on this machine")
        return found

    def argv(self, host: HostRecord, *extra: str) -> list[str]:
        key = self._key or hub_key_path()
        known = self._known_hosts or known_hosts_path()
        control = self._control or control_dir()
        return [
            self._binary(),
            "-F", "/dev/null",
            "-i", str(key),
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={known}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "CheckHostIP=no",
            "-o", f"HostKeyAlias={host.key_alias}",
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "ConnectTimeout=10",
            "-o", f"ServerAliveInterval={int(KEEPALIVE_S)}",
            "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control}/%C",
            "-o", "ControlPersist=120",
            "-o", "LogLevel=ERROR",
            "-T",
            "-p", str(host.port),
            *extra,
            "--",
            f"{check_user(host.user)}@{check_hostname(host.address)}",
        ]  # fmt: skip

    def open(self, host: HostRecord) -> Channel:
        argv = self.argv(host)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            raise LinkError(UNREACHABLE, f"could not run ssh: {e}") from e
        return SshChannel(proc)

    def close(self, host: HostRecord) -> None:
        """Drop the multiplexed connection, so a removed or revoked host is not kept
        reachable by a master that already authenticated."""
        try:
            subprocess.run(
                self.argv(host, "-O", "exit"),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, LinkError):
            pass


# ------------------------------------------------------------------ probing


@dataclass(frozen=True, slots=True)
class Probe:
    state: str
    reason: str | None
    cap: str | None = None
    remote: dict[str, Any] | None = None  # the remote daemon's /api/v1/meta
    bridge: dict[str, Any] | None = None


def probe_host(
    transport: Transport, host: HostRecord, *, protocol: int = PROTOCOL_VERSION
) -> Probe:
    """One ``GET /api/v1/bridge`` through the transport: is the key still accepted,
    is the daemon up, does its protocol match. Raises ``LinkError`` when the channel
    itself fails."""
    resp = _ask(transport, host, "GET", "/api/v1/bridge", [("Accept", "application/json")])
    try:
        body = resp.read(1024 * 1024)
    finally:
        resp.close()
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        payload = {}
    if resp.status == 403:
        code = ((payload.get("errors") or [{}])[0]).get("title")
        if code == "bridge.revoked":
            return Probe(AUTH_REFUSED, "this host no longer accepts this hub", None, None)
        return Probe(
            AUTH_REFUSED, f"the bridge refused the hub ({code or resp.status})", None, None
        )
    if resp.status != 200:
        return Probe(UNREACHABLE, f"the bridge answered {resp.status}", None, None)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return Probe(UNREACHABLE, "the bridge's answer was not understood", None, None)
    cap = data.get("max_role") if data.get("max_role") in ROLES else "viewer"
    if data.get("bridge_protocol") != BRIDGE_PROTOCOL:
        return Probe(
            VERSION_MISMATCH,
            f"the remote's cwcli speaks bridge protocol {data.get('bridge_protocol')}, this hub {BRIDGE_PROTOCOL}",
            cap,
            None,
            data,
        )
    remote = data.get("daemon")
    if not isinstance(remote, dict):
        return Probe(
            DAEMON_DOWN,
            str(data.get("daemon_error") or "cwcli is not running on this host"),
            cap,
            None,
            data,
        )
    if remote.get("protocol") != protocol:
        side = (
            "this hub"
            if isinstance(remote.get("protocol"), int) and remote["protocol"] > protocol
            else "the remote host"
        )
        return Probe(
            VERSION_MISMATCH,
            f"the remote runs cwcli {remote.get('version')} (protocol {remote.get('protocol')}); "
            f"this hub speaks protocol {protocol}: update cwcli on {side}",
            cap,
            remote,
            data,
        )
    return Probe(CONNECTED, None, cap, remote, data)


def parse_sse(buffer: bytes) -> tuple[list[tuple[str | None, str, str | None]], bytes]:
    """Complete frames in ``buffer`` as ``(event, data, id)`` plus the unconsumed
    tail; a comment-only frame (the keepalive) comes back as ``(None, "", None)``."""
    frames: list[tuple[str | None, str, str | None]] = []
    while True:
        marker = buffer.find(b"\n\n")
        if marker < 0:
            return frames, buffer
        raw, buffer = buffer[:marker], buffer[marker + 2 :]
        event: str | None = None
        event_id: str | None = None
        data: list[str] = []
        for line in raw.decode("utf-8", "replace").split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif line.startswith("id:"):
                event_id = line[3:].strip()
        frames.append((event, "\n".join(data), event_id))


# ----------------------------------------------------------------- the link


@dataclass(slots=True)
class LinkStatus:
    state: str = CONNECTING
    reason: str | None = None
    since: float = 0.0
    last_seen: float | None = None
    cap: str | None = None
    remote: dict[str, Any] | None = None
    drops_10m: int = 0


class HostLink:
    """The hub's standing connection to one host: a probe, then the remote's SSE stream
    held open and re-published with the host's id, with explicit link states and a
    jittered backoff when it drops. ``step`` is one cycle, so a test drives it with a
    fake transport and no threads."""

    def __init__(
        self,
        host: HostRecord,
        transport: Transport,
        *,
        on_state: Callable[[HostLink], None],
        on_frame: Callable[[HostLink, str, Any], None],
        protocol: int = PROTOCOL_VERSION,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.host = host
        self.transport = transport
        self._on_state = on_state
        self._on_frame = on_frame
        self._protocol = protocol
        self._clock = clock
        self._jitter = jitter
        self._lock = threading.Lock()
        self.status = LinkStatus(since=clock())
        self._drops: deque[float] = deque()
        self._attempts = 0
        self._grace = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._current: HttpResponse | None = None
        self.thread: threading.Thread | None = None
        self.probe_count = 0
        self.focus: str | None = None  # the instance a hub tab has open here (``?focus=``)

    # -- state ------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            while self._drops and now - self._drops[0] > DROP_WINDOW_S:
                self._drops.popleft()
            status = replace(self.status, drops_10m=len(self._drops))
        host = self.host
        return {
            "id": host.id,
            "name": host.name,
            "transport": host.transport,
            "address": host.address,
            "port": host.port,
            "user": host.user,
            "host_key_fingerprint": host.fingerprint(),
            "added_at": host.added_at,
            "added_by": host.added_by,
            **asdict(status),
            "actions_allowed": status.state == CONNECTED,
            "readable": status.state in READABLE_STATES,
        }

    def _set(self, state: str, reason: str | None = None, **more: Any) -> None:
        """Record a state (and the probe's ``cap``/``remote``); publish when anything moved."""
        with self._lock:
            moved = self.status.state != state or self.status.reason != reason
            changed = moved or any(getattr(self.status, k) != v for k, v in more.items())
            if moved:
                self.status.since = self._clock()
            self.status.state = state
            self.status.reason = reason
            for key, value in more.items():
                setattr(self.status, key, value)
        if changed:
            self._on_state(self)

    def touch(self) -> None:
        with self._lock:
            self.status.last_seen = self._clock()

    def request_check(self) -> None:
        """Reconnect now (an Admin's Check, a re-pin, a store change)."""
        self._grace = False
        self._attempts = 0
        self._wake.set()

    def set_focus(self, project: str | None) -> None:
        """Hold the stream with ``?focus=<project>`` (a hub tab has that instance open, so
        the remote's daemon probes it live) or without: the held stream is ended and
        reopened with the new query at once, never counted as a drop."""
        if project == self.focus:
            return
        self.focus = project
        current = self._current
        if current is not None:
            current.cancel()

    # -- the cycle --------------------------------------------------------

    def step(self) -> float:
        """One cycle: probe, then hold the stream until it ends. Returns how long to
        wait before the next cycle: 0 right after a drop, so one lost packet never
        flashes a host's instances to unknown (the grace probe decides), but a backoff
        when the grace probe's own stream ended again within a keepalive, so a remote
        whose stream keeps closing as soon as it opens is not dialled in a loop."""
        if self.status.state == HOST_KEY_CHANGED and not self._wake.is_set():
            return MAX_BACKOFF_S  # never reconnects by itself; an Admin re-pins
        self._wake.clear()
        after_drop = self._grace
        if not self._grace:
            self._set(CONNECTING, None)
        try:
            probe = probe_host(self.transport, self.host, protocol=self._protocol)
        except LinkError as e:
            self._grace = False
            self._set(e.state, e.reason)
            self.probe_count += 1
            return self._backoff()
        self._grace = False
        more: dict[str, Any] = {"cap": probe.cap}
        if probe.remote is not None:
            more["remote"] = probe.remote
        self._set(probe.state, probe.reason, **more)
        self.probe_count += 1
        if probe.state not in READABLE_STATES:
            return self._backoff()
        self.touch()
        opened = self._clock()
        try:
            self._hold_stream()
        except LinkError:
            if self._stop.is_set():
                return 0.0
            self._drops.append(self._clock())
            self._grace = True
            if self._clock() - opened < KEEPALIVE_S:
                return self._backoff() if after_drop else 0.0
            self._attempts = 0
            return 0.0
        return 0.0

    def _hold_stream(self) -> None:
        """Hold the remote's stream until it ends; a focus change reopens it at once."""
        while True:
            focus = self.focus
            try:
                self._stream_once(focus)
                return
            except LinkError:
                if self.focus == focus or self._stop.is_set():
                    raise

    def _stream_once(self, focus: str | None) -> None:
        target = f"/api/v1/stream?focus={quote(focus, safe='')}" if focus else "/api/v1/stream"
        resp = _ask(self.transport, self.host, "GET", target, [("Accept", "text/event-stream")])
        self._current = resp
        try:
            if resp.status != 200:
                raise LinkError(UNREACHABLE, f"the remote's stream answered {resp.status}")
            buffer = b""
            for chunk in resp.iter_body(idle_timeout=STREAM_IDLE_S):
                if self._stop.is_set():
                    return
                if self.focus != focus:
                    raise LinkError(UNREACHABLE, "the stream's focus changed")
                self.touch()
                frames, buffer = parse_sse(buffer + chunk)
                if len(buffer) > _FRAME_LIMIT:
                    raise LinkError(
                        UNREACHABLE, "the remote sent a frame larger than the hub accepts"
                    )
                for event, data, _event_id in frames:
                    if event is None:
                        continue  # a keepalive comment
                    try:
                        payload = json.loads(data) if data else None
                    except ValueError:
                        continue
                    self._on_frame(self, event, payload)
            raise LinkError(UNREACHABLE, "the remote closed the stream")
        finally:
            self._current = None
            resp.close()

    def _backoff(self) -> float:
        self._attempts = min(self._attempts + 1, 6)
        base = min(MAX_BACKOFF_S, float(2**self._attempts))
        if self.status.state == AUTH_REFUSED:
            base = MAX_BACKOFF_S
        return base * (0.5 + self._jitter())

    # -- threads ----------------------------------------------------------

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"host-{self.host.id}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.step()
            except Exception as e:  # a link thread must never die on an odd answer
                self._set(UNREACHABLE, f"{type(e).__name__}: {e}")
                delay = self._backoff()
            if delay > 0:
                self._wake.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        current = self._current
        if current is not None:
            current.cancel()


# -------------------------------------------------------------- the manager


def register_change_listener(fn: Callable[[], None]) -> None:
    _change_listeners.append(fn)


def unregister_change_listener(fn: Callable[[], None]) -> None:
    with _lock:
        if fn in _change_listeners:
            _change_listeners.remove(fn)


def notify_change() -> None:
    """The store or the transport integration changed: every running manager resyncs."""
    for fn in list(_change_listeners):
        try:
            fn()
        except Exception:
            pass


def transports() -> dict[str, Transport]:
    """Every transport an integration that is on provides, by id. Official
    integrations only: a transport sees every request to a remote, job passwords
    included, so community code never serves this hook point."""
    out: dict[str, Transport] = {}
    try:
        found = integrations.providers(HOST_TRANSPORT)
    except CwcliError:
        return out
    for integ, settings, secrets in found:
        if integ.publisher != integrations.OFFICIAL:
            continue
        fn = integ.hooks.get(HOST_TRANSPORT)
        if fn is None:
            continue
        try:
            transport = fn(settings, secrets)
        except Exception:
            continue
        out[str(getattr(transport, "id", ""))] = transport
    return out


def _unknownify(instances: list[dict], view: dict[str, Any]) -> list[dict]:
    """A quiet host's last-known instances, marked the way this daemon marks an instance
    whose probe failed: ``unknown``, with a ``probe_error`` naming why (every page shows
    the readings under it as the last successful probe's), and each bench ``unknown``."""
    why = (
        f"{view['name']}: {view['reason']}"
        if view.get("reason")
        else f"{view['name']} is {view['state'].replace('_', ' ')}"
    )
    out = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        benches = [
            {**b, "overall": "unknown"} for b in inst.get("benches") or [] if isinstance(b, dict)
        ]
        out.append(
            {
                **inst,
                "overall": "unknown",
                "web_probed": False,
                "probe_error": why,
                "benches": benches,
            }
        )
    return out


class HostManager:
    """Owns one :class:`HostLink` per stored host, republishes their frames on the hub's
    own stream, and remembers each host's last snapshot so a quiet host still shows
    its instances (as ``unknown``) with the time it was last seen."""

    RESYNC_S = 15.0

    def __init__(
        self,
        *,
        publish: Callable[[str, dict], None],
        protocol: int = PROTOCOL_VERSION,
        transports_fn: Callable[[], dict[str, Transport]] = transports,
    ) -> None:
        self._publish = publish
        self._protocol = protocol
        self._transports_fn = transports_fn
        self._lock = threading.RLock()
        self._links: dict[str, HostLink] = {}
        self._off: dict[str, HostRecord] = {}  # hosts this daemon runs no link for
        self._off_state = (TRANSPORT_OFF, TRANSPORT_OFF_REASON)
        self._instances: dict[str, list[dict]] = {}
        self._jobs: dict[str, list[dict]] = {}
        self._transports: dict[str, Transport] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mtime = -1.0
        self._focus: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        register_change_listener(self.resync)
        self.resync()
        self._thread = threading.Thread(target=self._watch, name="hosts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        unregister_change_listener(self.resync)
        with self._lock:
            links = list(self._links.values())
            self._links.clear()
        for link in links:
            link.stop()
            try:
                link.transport.close(link.host)
            except Exception:
                pass

    def _watch(self) -> None:
        while not self._stop.wait(self.RESYNC_S):
            if store_mtime() != self._mtime:
                self.resync()

    def resync(self) -> None:
        """Make the running links match the store and the transports that are on, reading
        the store under this manager's lock so concurrent resyncs never apply a stale one.

        A host that left the store, or whose id a new enrollment took (a different
        ``added_at``; a re-pin keeps it), is announced removed and its last-known
        instances and jobs are dropped, with or without a running link. A host left with
        no link keeps them, announced as unknown. On a shared
        install a daemon working in the shared home as someone other than the service
        account runs no link: it cannot read the hub key, so every host is listed with
        the reason and nothing is dialled."""
        if self._stop.is_set():
            return
        with self._lock:
            try:
                records = {r.id: r for r in list_hosts()}
            except CwcliError:
                return
            self._mtime = store_mtime()
            member = not_service_account()
            try:
                available = self._transports_fn()
            except Exception:
                available = {}
            self._transports = available
            self._off_state = (
                (SERVICE_ACCOUNT_ONLY, SERVICE_ACCOUNT_REASON)
                if member
                else (TRANSPORT_OFF, TRANSPORT_OFF_REASON)
            )
            known = {host_id: link.host for host_id, link in self._links.items()}
            known.update(self._off)
            for host_id in set(known) | set(self._instances) | set(self._jobs):
                rec, old = records.get(host_id), known.get(host_id)
                if rec is None or (old is not None and rec.added_at != old.added_at):
                    self._instances.pop(host_id, None)
                    self._jobs.pop(host_id, None)
                    self._publish("host_removed", {"host": host_id})
            for host_id, link in list(self._links.items()):
                rec = records.get(host_id)
                if member or rec is None or rec != link.host or rec.transport not in available:
                    del self._links[host_id]
                    link.stop()
                    try:
                        link.transport.close(link.host)
                    except Exception:
                        pass
            self._off = {}
            for host_id, rec in records.items():
                if host_id in self._links:
                    continue
                transport = None if member else available.get(rec.transport)
                if transport is None:
                    self._off[host_id] = rec
                    self._publish_link(host_id, self._off_view(rec))
                    continue
                link = HostLink(
                    rec,
                    transport,
                    on_state=self._state_changed,
                    on_frame=self._frame,
                    protocol=self._protocol,
                )
                link.focus = self._focus.get(host_id)
                self._links[host_id] = link
                link.start()

    def set_focus(self, focus: dict[str, str]) -> None:
        """The instance a hub tab has open on each host (``{host: project}``, the most
        recent tab's): that host's held stream carries it as ``?focus=``, so the remote's
        daemon probes it live, and a host no tab focuses any more holds it without."""
        with self._lock:
            self._focus = dict(focus)
            links = list(self._links.items())
        for host_id, link in links:
            link.set_focus(focus.get(host_id))

    # -- views ------------------------------------------------------------

    def _off_view(self, rec: HostRecord) -> dict[str, Any]:
        state, reason = self._off_state
        return {
            "id": rec.id,
            "name": rec.name,
            "transport": rec.transport,
            "address": rec.address,
            "port": rec.port,
            "user": rec.user,
            "host_key_fingerprint": rec.fingerprint(),
            "added_at": rec.added_at,
            "added_by": rec.added_by,
            "state": state,
            "reason": reason,
            "since": None,
            "last_seen": None,
            "cap": None,
            "remote": None,
            "drops_10m": 0,
            "actions_allowed": False,
            "readable": False,
        }

    def hub_view(self) -> dict[str, Any]:
        return {
            "name": hub_name(),
            "public_key": hub_public_key(),
            "allow_command": allow_command(),
            "transport_on": bool(self._transports),
            "protocol": self._protocol,
        }

    def views(self) -> list[dict[str, Any]]:
        with self._lock:
            out = [link.view() for link in self._links.values()]
            out.extend(self._off_view(rec) for rec in self._off.values())
        return sorted(out, key=lambda v: v["name"].lower())

    def view(self) -> dict[str, Any]:
        return {"hub": self.hub_view(), "hosts": self.views()}

    def snapshot(self) -> list[dict[str, Any]]:
        """Every host with its last-known instances and jobs, for the stream's first frame."""
        out = []
        for view in self.views():
            with self._lock:
                instances = list(self._instances.get(view["id"], []))
                jobs = list(self._jobs.get(view["id"], []))
            if view["state"] not in READABLE_STATES:
                instances = _unknownify(instances, view)
            out.append({**view, "instances": instances, "jobs": jobs})
        return out

    def get(self, host_id: str) -> HostLink | None:
        with self._lock:
            return self._links.get(host_id)

    def link_view(self, host_id: str) -> dict[str, Any] | None:
        link = self.get(host_id)
        if link is not None:
            return link.view()
        with self._lock:
            rec = self._off.get(host_id)
        return self._off_view(rec) if rec is not None else None

    # -- callbacks --------------------------------------------------------

    def _current_link(self, link: HostLink) -> bool:
        """Whether ``link`` is still the one this manager runs for its host: a link that
        was stopped (its host removed, re-pinned or turned off) may still finish a probe
        or a frame, and must never publish the host back onto the page."""
        return self.get(link.host.id) is link

    def _state_changed(self, link: HostLink) -> None:
        with self._lock:
            if not self._current_link(link):
                return
            self._publish_link(link.host.id, link.view())

    def _publish_link(self, host_id: str, view: dict[str, Any]) -> None:
        """A host's link state and, while it is not readable, its last-known instances as
        unknown, so no open page keeps showing them live. Called under the lock."""
        self._publish("host_link", view)
        if view["state"] not in READABLE_STATES:
            self._publish(
                "host_snapshot",
                {
                    "host": host_id,
                    "instances": _unknownify(self._instances.get(host_id, []), view),
                    "jobs": list(self._jobs.get(host_id, [])),
                    "stale": True,
                    "last_seen": view["last_seen"],
                },
            )

    def _frame(self, link: HostLink, event: str, data: Any) -> None:
        host_id = link.host.id
        with self._lock:
            if not self._current_link(link):
                return
            if event == "snapshot" and isinstance(data, dict):
                instances = [i for i in data.get("instances") or [] if isinstance(i, dict)]
                jobs = [j for j in data.get("jobs") or [] if isinstance(j, dict)]
                self._instances[host_id] = instances
                self._jobs[host_id] = jobs
                self._publish(
                    "host_snapshot",
                    {"host": host_id, "instances": instances, "jobs": jobs, "stale": False},
                )
            elif event == "delta" and isinstance(data, dict):
                project = data.get("project")
                rows = [i for i in self._instances.get(host_id, []) if i.get("project") != project]
                if not data.get("removed") and isinstance(data.get("instance"), dict):
                    rows.append(data["instance"])
                self._instances[host_id] = rows
                self._publish("host_delta", {**data, "host": host_id})
            elif event == "job" and isinstance(data, dict):
                jobs = [j for j in self._jobs.get(host_id, []) if j.get("id") != data.get("id")]
                if not data.get("removed"):
                    jobs.append(data)
                self._jobs[host_id] = jobs
                self._publish("host_job", {**data, "host": host_id})
            else:
                self._publish("host_event", {"host": host_id, "event": event, "data": data})

    # -- requests ---------------------------------------------------------

    def request(
        self,
        host_id: str,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes = b"",
    ) -> HttpResponse:
        """One request to a host's API through its transport (``_ask``). A refused key or
        a changed host key updates the link state at once (the stream loop would notice
        later); any other failure is this request's, never the link's."""
        link = self.get(host_id)
        if link is None:
            view = self.link_view(host_id)
            if view is not None:
                raise LinkError(view["state"], view["reason"])
            raise CwcliError(ErrorKind.NOT_FOUND, "host.unknown", f"No remote host '{host_id}'.")
        try:
            return _ask(
                link.transport, link.host, method, target, headers, body, timeout=FORWARD_TIMEOUT_S
            )
        except LinkError as e:
            if e.state in (HOST_KEY_CHANGED, AUTH_REFUSED):
                link._set(e.state, e.reason)
            raise

    def wait_settled(self, host_id: str, timeout: float = 8.0) -> dict[str, Any] | None:
        """The host's view once its first probe answered (or after ``timeout``)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.link_view(host_id)
            if view is None or view["state"] != CONNECTING:
                return view
            time.sleep(0.1)
        return self.link_view(host_id)

    def check(self, host_id: str, *, timeout: float = 10.0) -> dict[str, Any] | None:
        """Check a host NOW and answer with what the check found.

        A host that is not connected is woken for a fresh probe, which this waits for
        (a stream it still holds, as a version mismatch does, is ended so the loop gets
        to that probe). A connected host is probed out of band on its own channel (the
        held stream cannot tell the page a daemon died until the stream notices); a
        probe that disagrees with the link is applied at once and the stale stream is
        ended, so the loop re-probes and settles. Returns the host's view.
        """
        link = self.get(host_id)
        if link is None:
            return self.link_view(host_id)
        if link.status.state == CONNECTED:
            try:
                probe = probe_host(link.transport, link.host, protocol=self._protocol)
            except LinkError as e:
                probe = Probe(e.state, e.reason)
            more = {
                k: v for k, v in (("cap", probe.cap), ("remote", probe.remote)) if v is not None
            }
            link._set(probe.state, probe.reason, **more)
            if probe.state == CONNECTED:
                link.touch()
                return link.view()
            current = link._current
            if current is not None:
                current.cancel()
            return link.view()
        before = link.probe_count
        link.request_check()
        current = link._current
        if current is not None:
            current.cancel()
        deadline = time.monotonic() + timeout
        while (
            time.monotonic() < deadline and link.probe_count == before and self.get(host_id) is link
        ):
            time.sleep(0.1)
        return self.link_view(host_id)


# ------------------------------------------------------- the SSH integration


def _ssh_normalize(settings: dict[str, Any]) -> dict[str, Any]:
    name = str(settings.get("hub_name") or "").strip() or default_hub_name()
    return {"hub_name": check_hub_name(name)}


def _ssh_test(settings: dict[str, Any], secrets: dict[str, str]) -> integrations.Check:
    if sys.platform == "win32":
        return integrations.Check(
            False,
            "A hub runs on Linux, macOS or WSL: the native Windows OpenSSH cannot multiplex connections.",
        )
    ssh = shutil.which("ssh")
    if ssh is None:
        return integrations.Check(False, "ssh was not found on this machine; install OpenSSH.")
    try:
        key = ensure_hub_key(settings.get("hub_name"))
    except CwcliError as e:
        return integrations.Check(False, e.message)
    version = subprocess.run([ssh, "-V"], capture_output=True, text=True, check=False)
    banner = (version.stderr or version.stdout).strip().split(",")[0] or "ssh"
    return integrations.Check(
        True,
        f"{banner} found; this hub is {settings['hub_name']} and its key is ready.",
        {"public_key": key, "hub_name": settings["hub_name"]},
    )


def _ssh_notices() -> list[str]:
    key = hub_public_key()
    if not key:
        return []
    return [
        f"Hub key: {key}",
        f"On each remote machine, as the user who runs cwcli there: {allow_command()}",
    ]


SSH_INTEGRATION = integrations.Integration(
    id=INTEGRATION_ID,
    name="Remote hosts over SSH",
    publisher=integrations.OFFICIAL,
    description="Show and drive the instances of other machines from this web UI. This hub dials "
    "each machine over SSH with one key that the machine's owner restricts to cwcli's API "
    "and caps at a role; nothing new listens on the remote.",
    provides=(HOST_TRANSPORT,),
    fields=(
        integrations.Field(
            "hub_name",
            "Hub name",
            help="How this machine names itself to the remotes; `cwcli remote allow --hub <name>` there uses it.",
            placeholder=default_hub_name(),
        ),
    ),
    normalize=_ssh_normalize,
    test=_ssh_test,
    on_change=notify_change,
    notices=_ssh_notices,
    hooks={HOST_TRANSPORT: lambda settings, secrets: SshTransport()},
)
