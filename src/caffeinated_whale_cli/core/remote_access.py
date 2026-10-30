"""The remote side of a hub link: granting and revoking a hub's restricted key (UI-pure).

``cwcli remote allow`` and ``cwcli remote revoke`` are renderers over this module.
A grant is ONE line in the invoking user's ``authorized_keys``::

    restrict,command="<python> -m caffeinated_whale_cli.bridge --hub <name> --max-role <cap>
        --home <cwcli home> --state <ui state file> --keys <this file> --key-fp <SHA256:...>"
        <type> <key> cwcli-hub:<name>

``restrict`` turns off port forwarding, agent forwarding, X11 and the pty, and the
forced command means the key can run the bridge (``caffeinated_whale_cli.bridge``)
and nothing else: a shell, a different command, or a forward to another port are
all refused by sshd itself. The cap in that line is the most the hub may do here,
however the hub's own user is ranked; the bridge lowers every claim to it. The
command names the key's fingerprint, so the bridge can require the exact line a
channel runs under: a new cap or a new key for the hub refuses the channels of a
connection that authenticated against the line before.

The public key the hub hands over is parsed, not pasted: its options and comment
are dropped and the blob must decode as the type it claims, so a key that arrives
with ``command=`` of its own or a second line in it cannot widen the grant. The
pairing string ``allow`` prints carries this machine's SSH host key, so the hub pins
it and never trusts the first connection.

``revoke`` removes the line AND ends the bridge processes that hub still has running
(OpenSSH checks ``authorized_keys`` only when a connection authenticates, so an open
multiplexed connection would otherwise keep working), and ``allow`` does the same when
it replaces a hub's line; the bridge also re-reads the grant on every channel, which
closes the window for a channel opened after the line is gone or changed.

``allow`` records which file holds each hub's line (``hub-grants.json`` in the cwcli
home the bridge reaches), so ``revoke``, ``hubs`` and the schedule owner check read the
grant the bridge enforces, whichever file and whichever account wrote it.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..utils import config_utils
from .errors import CwcliError, ErrorKind
from .hosts import (
    DEFAULT_CAP,
    check_hostname,
    check_hub_name,
    check_name,
    check_port,
    check_role,
    check_user,
    fingerprint,
    make_pairing,
    not_service_account,
    parse_public_key,
    write_state,
)
from .schedules import StoreLock

BRIDGE_MODULE = "caffeinated_whale_cli.bridge"
HOST_KEY_FILES = (
    "/etc/ssh/ssh_host_ed25519_key.pub",
    "/etc/ssh/ssh_host_ecdsa_key.pub",
    "/etc/ssh/ssh_host_rsa_key.pub",
)
_MARKER = "cwcli-hub:"
_ARG_BAD = re.compile(r"[\"'\\\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class Grant:
    hub: str
    cap: str
    line: str
    keys_path: str
    replaced: bool
    pairing: str
    fields: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    ended_pids: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class HubGrant:
    hub: str
    cap: str
    key_type: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Revocation:
    hub: str
    removed: int
    ended_pids: list[int]
    keys_path: str


def authorized_keys_path() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def grants_record(home: Path | None = None) -> Path:
    return (home or config_utils.cwcli_home()) / "hub-grants.json"


def recorded_grants(home: Path | None = None) -> dict[str, str]:
    """Which file holds each hub's grant line (hub to ``authorized_keys`` path), as
    ``allow`` wrote it. Raises ``CwcliError`` naming the record when it cannot be read."""
    path = grants_record(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "remote.grants_unreadable", f"{path} is unreadable: {e}"
        ) from e
    grants = data.get("grants") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in grants.items()} if isinstance(grants, dict) else {}


def _record_grant(home: Path, hub: str, keys: Path | None) -> None:
    path = grants_record(home)
    with StoreLock(path):
        grants = recorded_grants(home)
        if keys is None:
            grants.pop(hub, None)
        else:
            grants[hub] = str(keys)
        write_state(path, json.dumps({"version": 1, "grants": grants}, indent=1, sort_keys=True))


def grant_cap(hub: str) -> str | None:
    """The cap this machine's grant gives ``hub`` now, read from the file ``allow`` wrote
    it to (the file the bridge reads), or None when there is no grant. Raises
    ``CwcliError`` naming the file when it cannot be read."""
    path = recorded_grants().get(hub)
    if path is None:
        return None
    return next((g.cap for g in _grants_in(Path(path)) if g.hub == hub), None)


def bridge_command(
    *,
    python: str,
    hub: str,
    cap: str,
    home: str,
    state: str,
    keys: str,
    key_fp: str,
    start_daemon: bool = True,
) -> str:
    """The forced command. Every argument is shell-quoted (sshd runs the command
    through the user's shell) and refused outright when it carries a quote, a
    backslash or a control character: those are the characters that could end the
    ``command="..."`` option early or smuggle a second command."""
    argv = [
        python,
        "-m",
        BRIDGE_MODULE,
        "--hub",
        hub,
        "--max-role",
        cap,
        "--home",
        home,
        "--state",
        state,
        "--keys",
        keys,
    ]
    if not start_daemon:
        argv.append("--no-start")
    argv.extend(["--key-fp", key_fp])
    for arg in argv:
        if _ARG_BAD.search(arg):
            raise CwcliError(
                ErrorKind.USAGE,
                "remote.unsafe_path",
                f"{arg!r} cannot be written into an authorized_keys command (quotes, backslashes "
                "and control characters are not allowed).",
            )
    return " ".join(shlex.quote(arg) for arg in argv)


def grant_line(*, key_type: str, key_blob: str, hub: str, command: str) -> str:
    return f'restrict,command="{command}" {key_type} {key_blob} {_MARKER}{hub}'


def _is_grant_for(line: str, hub: str) -> bool:
    stripped = line.strip()
    return stripped.endswith(f" {_MARKER}{hub}") and BRIDGE_MODULE in stripped


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "remote.keys_unreadable", f"{path} is unreadable: {e}"
        ) from e


def _write_lines(path: Path, lines: list[str]) -> None:
    """Rewrite ``authorized_keys`` atomically, keeping every other line byte for byte,
    with the modes sshd insists on (``.ssh`` 0700, the file 0600)."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".cwcli-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_host_key(path: str | None = None) -> tuple[str, str] | None:
    """This machine's SSH host public key ``(type, blob)`` from the sshd key files
    (ed25519 first), or the named file; None when none is readable."""
    candidates = [path] if path else list(HOST_KEY_FILES)
    for candidate in candidates:
        try:
            text = Path(candidate).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                try:
                    return parse_public_key(line)
                except CwcliError:
                    continue
    return None


def outbound_address() -> str | None:
    """This machine's address on the interface that reaches out (a UDP connect to a
    documentation address sends nothing); None when it cannot be found."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        addr = str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()
    return None if addr.startswith("127.") else addr


def _ui_state_path() -> str:
    from ..commands import ui as ui_cmd  # the daemon's own notion of its state file

    return str(ui_cmd.state_file())


def allow(
    *,
    hub: str,
    key: str,
    cap: str = DEFAULT_CAP,
    address: str | None = None,
    port: int = 22,
    name: str | None = None,
    host_key_file: str | None = None,
    keys_path: Path | None = None,
    python: str | None = None,
    home: Path | None = None,
    state_path: str | None = None,
    start_daemon: bool = True,
    proc_root: Path = Path("/proc"),
) -> Grant:
    """Grant ``hub``: write (or replace) its restricted line and build the pairing string.

    Every field the hub will check in the pairing string is checked here first, so
    nothing is written for a grant the hub would refuse to add, and neither is a grant
    another line carrying the same key would shadow. Replacing a hub's line ends the
    bridges it still runs under the old one, as ``revoke`` does.
    """
    hub = check_hub_name(hub)
    cap = check_role(cap)
    key_type, key_blob = parse_public_key(key)
    port = check_port(port)
    user = check_user(getpass.getuser())
    label = check_name(name or socket.gethostname().split(".")[0])
    path = (keys_path or authorized_keys_path()).absolute()
    cwcli_home = home or config_utils.cwcli_home()
    command = bridge_command(
        python=python or sys.executable,
        hub=hub,
        cap=cap,
        home=str(cwcli_home),
        state=state_path or _ui_state_path(),
        keys=str(path),
        key_fp=fingerprint(key_type, key_blob),
        start_daemon=start_daemon,
    )
    line = grant_line(key_type=key_type, key_blob=key_blob, hub=hub, command=command)
    lines = _read_lines(path)
    for number, existing in enumerate(lines, 1):
        stripped = existing.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and key_blob in stripped.split()
            and not _is_grant_for(existing, hub)
        ):
            raise CwcliError(
                ErrorKind.CONFLICT,
                "remote.key_in_use",
                f"Line {number} of {path} already carries this key. sshd applies the first line "
                "that matches a key, so that line, not the restricted grant, would decide what "
                "the hub's key may do here.",
                hint="Remove that line (or give the hub a key of its own), then run cwcli remote "
                "allow again.",
            )
    kept = [existing for existing in lines if not _is_grant_for(existing, hub)]
    replaced = len(kept) != len(lines)
    _write_lines(path, kept + [line])
    _record_grant(cwcli_home, hub, path)
    ended = end_bridges(hub, proc_root=proc_root) if replaced else []

    warnings: list[str] = []
    host_key = read_host_key(host_key_file)
    if host_key is None:
        warnings.append(
            "This machine's SSH host key could not be read (/etc/ssh/ssh_host_*_key.pub); "
            "the hub needs it to pin this machine. Pass --host-key-file, or paste the key on the hub."
        )
    final_address = address or outbound_address() or socket.gethostname()
    try:
        final_address = check_hostname(final_address)
    except CwcliError:
        warnings.append(
            f"{final_address!r} is not an address a hub can use; set the address on the hub."
        )
        final_address = socket.gethostname().split(".")[0] or "remote"
    fields = {
        "v": 1,
        "hub": hub,
        "cap": cap,
        "user": user,
        "address": final_address,
        "port": port,
        "name": label,
        "host_key": f"{host_key[0]} {host_key[1]}" if host_key else None,
    }
    return Grant(
        hub=hub,
        cap=cap,
        line=line,
        keys_path=str(path),
        replaced=replaced,
        pairing=make_pairing(fields),
        fields={**fields, "host_key_fingerprint": fingerprint(*host_key) if host_key else None},
        warnings=warnings,
        ended_pids=ended,
    )


def list_grants(keys_path: Path | None = None) -> list[HubGrant]:
    """The hubs this machine lets in, with each one's cap: from ``keys_path``, else from
    the file ``allow`` wrote each hub's grant to."""
    if keys_path is not None:
        return _grants_in(keys_path)
    return [
        g
        for hub, path in sorted(recorded_grants().items())
        for g in _grants_in(Path(path))
        if g.hub == hub
    ]


def _grants_in(keys_path: Path) -> list[HubGrant]:
    out = []
    for line in _read_lines(keys_path):
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or _MARKER not in stripped
            or BRIDGE_MODULE not in stripped
        ):
            continue
        hub = stripped.rsplit(_MARKER, 1)[1].strip()
        cap_match = re.search(r"--max-role (\w+)", stripped)
        # The key sits right before the comment: "... <type> <blob> cwcli-hub:<hub>".
        parts = stripped.rsplit(" ", 3)
        key_type, blob = (parts[-3], parts[-2]) if len(parts) >= 3 else ("", "")
        try:
            fp = fingerprint(key_type, blob) if key_type and blob else ""
        except Exception:
            fp = ""
        out.append(
            HubGrant(
                hub=hub,
                cap=cap_match.group(1) if cap_match else "",
                key_type=key_type,
                fingerprint=fp,
            )
        )
    return out


def bridge_pids(hub: str, *, proc_root: Path = Path("/proc")) -> list[int]:
    """This user's running bridge processes for ``hub``, matched on exact argv tokens
    (never a name pattern that could catch another hub's or another user's process)."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    found: list[int] = []
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if uid is not None and entry.stat().st_uid != uid:
                    continue
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            tokens = [t.decode("utf-8", "replace") for t in argv if t]
            if _is_bridge_argv(tokens, hub):
                found.append(int(entry.name))
        return found
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,uid=,args="], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return found
    for row in listing.splitlines():
        parts = row.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        if uid is not None and parts[1].isdigit() and int(parts[1]) != uid:
            continue
        # ``ps args`` joins argv with spaces, unquoted: a plain split is the exact-token
        # read (shlex would raise on another process's stray quote).
        if _is_bridge_argv(parts[2].split(), hub):
            found.append(int(parts[0]))
    return found


def _is_bridge_argv(tokens: list[str], hub: str) -> bool:
    if BRIDGE_MODULE not in tokens or "-m" not in tokens:
        return False
    try:
        return tokens[tokens.index("--hub") + 1] == hub
    except (ValueError, IndexError):
        return False


def end_bridges(hub: str, *, proc_root: Path = Path("/proc"), grace: float = 2.0) -> list[int]:
    """SIGTERM every running bridge of ``hub``, then SIGKILL what is still there."""
    pids = bridge_pids(hub, proc_root=proc_root)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and bridge_pids(hub, proc_root=proc_root):
        time.sleep(0.1)
    for pid in bridge_pids(hub, proc_root=proc_root):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def revoke(
    *, hub: str, keys_path: Path | None = None, proc_root: Path = Path("/proc")
) -> Revocation:
    """Remove ``hub``'s line, from the file ``allow`` recorded for it unless ``keys_path``
    names one, and end its running bridges. Removing a hub that was never granted is a
    no-op that says so (``removed == 0``), never an error. A record that cannot be read
    never blocks a revoke: the line is then looked for in ``keys_path`` or the default."""
    hub = check_hub_name(hub)
    home = config_utils.cwcli_home()
    try:
        recorded = recorded_grants(home).get(hub)
    except CwcliError:
        recorded = None
    path = (keys_path or (Path(recorded) if recorded else authorized_keys_path())).absolute()
    lines = _read_lines(path)
    kept = [existing for existing in lines if not _is_grant_for(existing, hub)]
    removed = len(lines) - len(kept)
    if removed:
        _write_lines(path, kept)
    if recorded == str(path):
        _record_grant(home, hub, None)
    ended = end_bridges(hub, proc_root=proc_root)
    return Revocation(hub=hub, removed=removed, ended_pids=ended, keys_path=str(path))


def shared_mode_note() -> str | None:
    """On a shared install the bridge must run as the service account (the daemon's
    state lives in its home); say so when someone else runs ``allow``."""
    if not not_service_account():
        return None
    return (
        "This is a shared install: run `cwcli remote allow` as the cwcli service account "
        "(the user whose cwcli ui daemon the hub should reach), not as yourself."
    )
