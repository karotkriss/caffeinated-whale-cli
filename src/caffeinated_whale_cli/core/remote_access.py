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

``allow`` records each hub's grant (``hub-grants.json`` in the cwcli home the bridge
reaches): its file, that home, the account that wrote it, the cap, the key fingerprint
and when. A question about one hub reads only that hub's file, never another hub's: the
schedule owner check reads the recorded file (else this account's own
``authorized_keys``, so a line no home recorded still counts) and counts a line only
when its bridge serves this home; ``revoke`` cleans this account's own
``authorized_keys`` and the recorded file. ``hubs`` parses this account's
``authorized_keys`` and every recorded file live, and lists a grant whose file this
account cannot read (another account's 0700 ``.ssh``) from its record. Such a file is an
error that names the account that wrote the grant for ``revoke``, and leaves the owner
check unable to confirm the cap. Grant files are read and rewritten byte for byte, so
another key's comment that is not UTF-8 survives unchanged. A rewrite never changes who can
read the file: a symlinked file is rewritten where it really lives (the link stays a link),
it keeps its mode (and, under root, its owner), and a file with other hard links is refused,
as is a file another account owns unless this is root, whose ``revoke`` also ends that
account's bridges for the hub.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import config_utils
from .errors import CwcliError, ErrorKind
from .hosts import (
    DEFAULT_CAP,
    ROLES,
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
    keys_path: str
    home: str = ""  # the cwcli home the line's bridge serves
    unreadable: str = ""  # why keys_path could not be read: the rest comes from the record
    recorded_by: str = ""  # the account the record names, for a grant listed from it


@dataclass(frozen=True, slots=True)
class Revocation:
    hub: str
    removed: int
    ended_pids: list[int]
    files: dict[str, int]  # each authorized_keys file searched: the lines removed from it


def authorized_keys_path() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def grants_record(home: Path | None = None) -> Path:
    return (home or config_utils.cwcli_home()) / "hub-grants.json"


def recorded_grants(home: Path | None = None) -> dict[str, dict[str, Any]]:
    """Each hub's grant as ``allow`` recorded it: ``keys`` (the file holding the line),
    ``home`` (the cwcli home its bridge serves), ``user`` and ``uid`` (the account that
    wrote it), ``cap``, ``key_fp`` and ``at``. Raises ``CwcliError`` naming the record when
    it cannot be read."""
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
    if not isinstance(grants, dict):
        return {}
    return {
        str(hub): entry
        for hub, entry in grants.items()
        if isinstance(entry, dict) and isinstance(entry.get("keys"), str)
    }


def _record_grant(home: Path, hub: str, entry: dict[str, Any] | None) -> None:
    """Record (or, with ``entry`` None, forget) ``hub``'s grant, dropping entries whose file
    no longer exists."""
    path = grants_record(home)
    with StoreLock(path):
        grants = {h: e for h, e in recorded_grants(home).items() if not _gone(e["keys"])}
        if entry is None:
            grants.pop(hub, None)
        else:
            grants[hub] = entry
        write_state(path, json.dumps({"version": 1, "grants": grants}, indent=1, sort_keys=True))


def _gone(path: str) -> bool:
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def _grant_files(recorded: Iterable[Path]) -> list[Path]:
    """This account's own ``authorized_keys`` and each of ``recorded``, once each (a missing
    one reads as empty)."""
    out = [authorized_keys_path().absolute()]
    for path in recorded:
        if path not in out:
            out.append(path)
    return out


def _account(entry: dict[str, Any]) -> str:
    user, uid = entry.get("user") or "an unknown account", entry.get("uid")
    return str(user) if uid is None else f"{user} (uid {uid})"


def _lines_of(path: Path, hub: str, entry: dict[str, Any] | None) -> list[str]:
    """``path``'s lines. When it is the file the record names for ``hub`` and this account
    cannot read it, the error names the account that wrote the grant."""
    try:
        return _read_lines(path)
    except CwcliError as e:
        if entry is None or Path(entry["keys"]) != path:
            raise
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "remote.keys_unreadable",
            f"{path} holds hub {hub}'s grant, written by {_account(entry)}, and is not readable "
            "from this account",
            hint=f"Run this as {entry.get('user') or 'the account that wrote it'}.",
        ) from e


def grant_cap(hub: str) -> str | None:
    """The cap ``hub``'s grant gives it in this cwcli home, from the file the record names for
    the hub (else this account's own ``authorized_keys``): the lowest cap of the lines there
    for the hub whose bridge serves this home, or None when there is none. Never reads
    another hub's file. Raises ``CwcliError`` when that file cannot be read."""
    entry = recorded_grants().get(hub)
    path = Path(entry["keys"]) if entry else authorized_keys_path().absolute()
    home = config_utils.cwcli_home().resolve()
    caps = [
        g.cap
        for g in _grants_in(path, _lines_of(path, hub, entry))
        if g.hub == hub and g.home and Path(g.home).resolve() == home
    ]
    return min(caps, key=lambda c: ROLES.index(c) if c in ROLES else -1) if caps else None


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
    """``path``'s lines, split on newlines only and decoded so that ``_write_lines`` gives
    back every byte (OpenSSH treats a key's comment as opaque bytes)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "remote.keys_unreadable", f"{path} is unreadable: {e}"
        ) from e
    lines = data.decode("utf-8", "surrogateescape").split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _owner_name(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def _write_lines(path: Path, lines: list[str]) -> int:
    """Rewrite ``authorized_keys`` atomically without changing who can read it, and return
    the uid that owned it before (this process's, for a file this call creates).

    A symlinked file or directory is rewritten where it really lives, so the link stays a
    link. sshd opens the file as its owner, so the rewrite keeps every other line byte for
    byte, the file's mode and, under root, its owner and group. A file another account owns
    is refused unless this is root, and so is a file with other hard links (the new file
    would leave them with the old lines). The new content goes through a uniquely named file
    in the real file's directory (never a planted link), removed if anything fails. Only a
    ``.ssh`` or a file this call creates gets sshd's modes (0700, 0600).
    """
    real = Path(os.path.realpath(path))
    me = os.getuid() if hasattr(os, "getuid") else None
    tmp: str | None = None
    try:
        try:
            st: os.stat_result | None = real.stat()
        except FileNotFoundError:
            st = None
        if st is not None and me not in (None, 0, st.st_uid):
            owner = _owner_name(st.st_uid)
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "remote.keys_not_owned",
                f"{path} belongs to {owner}, not to this account, and rewriting it from here "
                "would change who can read it",
                hint=f"Run this as {owner}.",
            )
        if st is not None and st.st_nlink > 1:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "remote.keys_hard_linked",
                f"{path} has {st.st_nlink} hard links, and rewriting it would leave the others "
                "with the old lines",
                hint="Edit it in place, or remove its other links, then run this again.",
            )
        real.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{real.name}.", dir=real.parent)
        with os.fdopen(fd, "wb") as f:
            f.write("".join(line + "\n" for line in lines).encode("utf-8", "surrogateescape"))
            if st is not None:
                if me == 0:
                    os.fchown(f.fileno(), st.st_uid, st.st_gid)
                os.fchmod(f.fileno(), stat.S_IMODE(st.st_mode))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, real)
        tmp = None
    except OSError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "remote.keys_unwritable", f"{path} could not be written: {e}"
        ) from e
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return st.st_uid if st is not None else (me or 0)


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
    key_fp = fingerprint(key_type, key_blob)
    command = bridge_command(
        python=python or sys.executable,
        hub=hub,
        cap=cap,
        home=str(cwcli_home),
        state=state_path or _ui_state_path(),
        keys=str(path),
        key_fp=key_fp,
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
    owner = _write_lines(path, kept + [line])
    _record_grant(
        cwcli_home,
        hub,
        {
            "keys": str(path),
            "home": str(cwcli_home),
            "user": user,
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "cap": cap,
            "key_fp": key_fp,
            "at": time.time(),
        },
    )
    ended = end_bridges(hub, uids={owner}, proc_root=proc_root) if replaced else []

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
    """The hubs this machine lets in, with each one's cap, home and file: from ``keys_path``,
    else parsed live from this account's own ``authorized_keys`` and every recorded file,
    one entry per file and hub. A recorded file this account cannot read is listed from its
    record, with the account that wrote it (``recorded_by``) and why (``unreadable``)."""
    if keys_path is not None:
        return _grants_in(keys_path, _read_lines(keys_path))
    recorded = recorded_grants()
    found: dict[tuple[str, str], HubGrant] = {}
    for path in _grant_files(Path(e["keys"]) for e in recorded.values()):
        try:
            grants = _grants_in(path, _read_lines(path))
        except CwcliError as err:
            grants = [
                HubGrant(
                    hub=hub,
                    cap=str(entry.get("cap") or ""),
                    key_type="",
                    fingerprint=str(entry.get("key_fp") or ""),
                    keys_path=str(path),
                    home=str(entry.get("home") or ""),
                    unreadable=err.message,
                    recorded_by=_account(entry),
                )
                for hub, entry in recorded.items()
                if Path(entry["keys"]) == path
            ]
            if not grants:
                raise
        for g in grants:
            found.setdefault((g.keys_path, g.hub), g)
    return list(found.values())


def _grants_in(keys_path: Path, lines: list[str]) -> list[HubGrant]:
    out = []
    for line in lines:
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or _MARKER not in stripped
            or BRIDGE_MODULE not in stripped
        ):
            continue
        hub = stripped.rsplit(_MARKER, 1)[1].strip()
        command = re.search(r'command="([^"]*)"', stripped)
        try:
            argv = shlex.split(command.group(1)) if command else []
        except ValueError:
            argv = []
        opts = dict(zip(argv, argv[1:], strict=False))
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
                cap=opts.get("--max-role", ""),
                key_type=key_type,
                fingerprint=fp,
                keys_path=str(keys_path),
                home=opts.get("--home", ""),
            )
        )
    return out


def bridge_pids(hub: str, *, uids: set[int], proc_root: Path = Path("/proc")) -> list[int]:
    """The running bridge processes for ``hub`` of the accounts in ``uids``, matched on exact
    argv tokens (never a name pattern that could catch another hub's process)."""
    found: list[int] = []
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid not in uids:
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
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if int(parts[1]) not in uids:
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


def end_bridges(
    hub: str, *, uids: set[int], proc_root: Path = Path("/proc"), grace: float = 2.0
) -> list[int]:
    """SIGTERM the running bridges of ``hub`` of the accounts in ``uids``, then SIGKILL what
    is still there."""
    pids = bridge_pids(hub, uids=uids, proc_root=proc_root)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and bridge_pids(hub, uids=uids, proc_root=proc_root):
        time.sleep(0.1)
    for pid in bridge_pids(hub, uids=uids, proc_root=proc_root):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def revoke(
    *, hub: str, keys_path: Path | None = None, proc_root: Path = Path("/proc")
) -> Revocation:
    """Remove ``hub``'s grant lines and end its running bridges: from ``keys_path``, else
    from this account's own ``authorized_keys`` and the file the record names for the hub
    (never another hub's). Removing a hub that was never granted is a no-op that says so
    (``removed == 0``), never an error. A record that cannot be read never blocks a revoke:
    the default file is then searched. A recorded file this account cannot read, or
    another account's file it may not rewrite (anyone but root), is an error naming the
    account to run it as (its line stays, and so does its record). This account's bridges
    for the hub are ended whatever happens, and under root also those of each account whose
    file it rewrote, never anyone else's."""
    hub = check_hub_name(hub)
    home = config_utils.cwcli_home()
    try:
        entry = recorded_grants(home).get(hub)
    except CwcliError:
        entry = None
    recorded = Path(entry["keys"]) if entry else None
    if keys_path is not None:
        paths = [keys_path.absolute()]
    else:
        paths = _grant_files([recorded] if recorded else [])
    files: dict[str, int] = {}
    uids = {os.getuid() if hasattr(os, "getuid") else 0}
    try:
        for path in paths:
            lines = _lines_of(path, hub, entry)
            kept = [existing for existing in lines if not _is_grant_for(existing, hub)]
            if len(kept) != len(lines):
                uids.add(_write_lines(path, kept))
            files[str(path)] = len(lines) - len(kept)
        if recorded is not None and str(recorded) in files:
            _record_grant(home, hub, None)
    finally:
        ended = end_bridges(hub, uids=uids, proc_root=proc_root)
    return Revocation(hub=hub, removed=sum(files.values()), ended_pids=ended, files=files)


def shared_mode_note() -> str | None:
    """On a shared install a group member's grant is that member's own: the hub logs in as
    them and their bridge reaches their daemon on the shared home. Say so, and that only
    that account can revoke it."""
    if not not_service_account():
        return None
    return (
        "This is a shared install: the hub will log in as you, and the bridge reaches your "
        f"cwcli ui daemon on the shared home ({config_utils.cwcli_home()}). The line is in "
        "your authorized_keys, so run `cwcli remote revoke` as this same account to remove "
        "it; `cwcli remote hubs` lists it from any account, because the shared home records it."
    )
