"""The web UI's administrator account and its sign-in sessions (UI-pure).

One account store, ``<cwcli home>/accounts.json`` (0600; in shared mode the shared
state tree, so one account serves the machine), holds the account's scrypt hash and
the server-side sessions, keyed by the SHA-256 of the session id the browser holds
in its cookie - the file never contains a password or a usable session id.

* The default account is ``admin`` / ``admin`` with ``must_change`` set: the first
  sign-in (and every sign-in after ``reset_password``) has to pick a new password
  before anything else is reachable; the daemon enforces that gate, this module
  only records the flag.
* Sessions: 8 hours idle, 7 days absolute; "keep me signed in" makes both 30 days.
* Failed sign-ins are counted per username and per address; after
  ``LOCKOUT_AFTER`` failures inside ``LOCKOUT_WINDOW_S`` the next attempts are
  refused for ``LOCKOUT_S`` seconds. The counters live in this process only - a
  restart clears them, which is a shorter lockout, never a longer one.

Every function takes ``now`` so a test controls the clock; nothing here sleeps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..utils import config_utils, shared_home
from .errors import CwcliError, ErrorKind

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 8

IDLE_S = 8 * 3600
ABSOLUTE_S = 7 * 24 * 3600
REMEMBER_S = 30 * 24 * 3600

LOCKOUT_AFTER = 5
LOCKOUT_WINDOW_S = 10 * 60
LOCKOUT_S = 60

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_lock = threading.RLock()


@dataclass(frozen=True, slots=True)
class Session:
    username: str
    created: float
    last_seen: float
    remember: bool
    agent: str
    current: bool = False


@dataclass(frozen=True, slots=True)
class Account:
    username: str
    must_change: bool


def store_path() -> Path:
    return config_utils.cwcli_home() / "accounts.json"


# ----------------------------------------------------------------- the file


def _load() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "accounts": {}, "sessions": {}}
    except (OSError, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "accounts.unreadable", f"{store_path()} is unreadable: {e}"
        ) from e
    if not isinstance(data, dict):
        raise CwcliError(
            ErrorKind.PRECONDITION, "accounts.unreadable", f"{store_path()} is not a JSON object"
        )
    data.setdefault("accounts", {})
    data.setdefault("sessions", {})
    return data


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, shared_home.file_mode(0o600))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, shared_home.file_mode(0o600))
    shared_home.apply_group(path)


# ------------------------------------------------------------- passwords


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)


def _record(password: str, *, must_change: bool, now: float) -> dict:
    salt = secrets.token_bytes(16)
    return {
        "salt": salt.hex(),
        "hash": _hash(password, salt).hex(),
        "must_change": must_change,
        "changed_at": now,
    }


def _matches(record: dict, password: str) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(_hash(password, salt), expected)


def check_new_password(password: str, *, username: str = DEFAULT_USERNAME) -> str | None:
    """The reason a new password is refused, or None when it is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() == DEFAULT_PASSWORD or password.lower() == username.lower():
        return "The password must differ from the default and from the username."
    return None


def ensure_default(*, now: float | None = None) -> bool:
    """Create ``admin``/``admin`` (change forced) when no account exists yet.

    Called at every daemon start, so an install that predates accounts gets the
    default account on its first start of the new version. Returns True when the
    account was created now.
    """
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        if data["accounts"]:
            return False
        data["accounts"][DEFAULT_USERNAME] = _record(DEFAULT_PASSWORD, must_change=True, now=now)
        _save(data)
        return True


def account(username: str) -> Account | None:
    with _lock:
        rec = _load()["accounts"].get(username)
    return Account(username, bool(rec.get("must_change"))) if rec else None


def verify(username: str, password: str) -> Account | None:
    """The account when the password is right, else None (the same answer for an
    unknown username, after the same hash work, so timing tells the two apart no
    more than it must)."""
    with _lock:
        rec = _load()["accounts"].get(username)
    if rec is None:
        _hash(password, b"\0" * 16)  # burn the same time for an unknown name
        return None
    if not _matches(rec, password):
        return None
    return Account(username, bool(rec.get("must_change")))


def change_password(
    username: str, new_password: str, *, current: str | None = None, now: float | None = None
) -> None:
    """Set a new password. ``current`` must be right unless the change is the forced
    one (``must_change``), where the sign-in a moment ago already proved it."""
    now = time.time() if now is None else now
    reason = check_new_password(new_password, username=username)
    if reason:
        raise CwcliError(ErrorKind.USAGE, "password.weak", reason)
    with _lock:
        data = _load()
        rec = data["accounts"].get(username)
        if rec is None:
            raise CwcliError(ErrorKind.NOT_FOUND, "account.unknown", f"No account '{username}'.")
        if not rec.get("must_change"):
            if current is None or not _matches(rec, current):
                raise CwcliError(
                    ErrorKind.PRECONDITION,
                    "password.current_wrong",
                    "The current password is wrong.",
                )
        if _matches(rec, new_password):
            raise CwcliError(
                ErrorKind.USAGE, "password.unchanged", "The new password is the current one."
            )
        data["accounts"][username] = _record(new_password, must_change=False, now=now)
        _save(data)


def reset_password(username: str = DEFAULT_USERNAME, *, now: float | None = None) -> None:
    """Host-side recovery: back to ``admin``/``admin`` with the change forced again,
    and every session of the account closed."""
    now = time.time() if now is None else now
    if shared_home.shared_mode() and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "accounts.needs_root",
            "On a shared install the account reset needs root (sudo), like 'cwcli setup'.",
        )
    with _lock:
        data = _load()
        data["accounts"][username] = _record(DEFAULT_PASSWORD, must_change=True, now=now)
        data["sessions"] = {
            k: v for k, v in data["sessions"].items() if v.get("username") != username
        }
        _save(data)


# -------------------------------------------------------------- sessions


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _alive(rec: dict, now: float) -> bool:
    remember = bool(rec.get("remember"))
    idle = REMEMBER_S if remember else IDLE_S
    absolute = REMEMBER_S if remember else ABSOLUTE_S
    return bool(now - rec["last_seen"] <= idle and now - rec["created"] <= absolute)


def open_session(
    username: str, *, remember: bool, agent: str = "", now: float | None = None
) -> str:
    """Record a session and return the id the browser's cookie carries (never stored)."""
    now = time.time() if now is None else now
    session_id = secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        data["sessions"] = {k: v for k, v in data["sessions"].items() if _alive(v, now)}
        data["sessions"][_key(session_id)] = {
            "username": username,
            "created": now,
            "last_seen": now,
            "remember": remember,
            "agent": agent[:80],
        }
        _save(data)
    return session_id


def session_account(session_id: str, *, now: float | None = None) -> Account | None:
    """The signed-in account for a cookie, touching ``last_seen`` (at most once a
    minute, so a polling page does not rewrite the file on every request)."""
    if not session_id:
        return None
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        rec = data["sessions"].get(_key(session_id))
        if rec is None or not _alive(rec, now):
            return None
        acct = data["accounts"].get(rec["username"])
        if acct is None:
            return None
        if now - rec["last_seen"] >= 60:
            rec["last_seen"] = now
            _save(data)
    return Account(rec["username"], bool(acct.get("must_change")))


def close_session(session_id: str) -> None:
    with _lock:
        data = _load()
        if data["sessions"].pop(_key(session_id), None) is not None:
            _save(data)


def close_other_sessions(username: str, keep: str) -> int:
    """Sign the account out everywhere but ``keep``; returns how many were closed."""
    kept = _key(keep)
    with _lock:
        data = _load()
        before = len(data["sessions"])
        data["sessions"] = {
            k: v for k, v in data["sessions"].items() if k == kept or v.get("username") != username
        }
        closed = before - len(data["sessions"])
        if closed:
            _save(data)
    return closed


def list_sessions(username: str, *, current: str = "", now: float | None = None) -> list[Session]:
    now = time.time() if now is None else now
    cur = _key(current) if current else ""
    with _lock:
        data = _load()
    out = [
        Session(
            v["username"],
            v["created"],
            v["last_seen"],
            bool(v.get("remember")),
            v.get("agent", ""),
            k == cur,
        )
        for k, v in data["sessions"].items()
        if v.get("username") == username and _alive(v, now)
    ]
    return sorted(out, key=lambda s: s.last_seen, reverse=True)


# ------------------------------------------------------------- rate limit


class Lockout:
    """Failed sign-ins per key (a username, an address): ``LOCKOUT_AFTER`` inside
    ``LOCKOUT_WINDOW_S`` locks the key for ``LOCKOUT_S``."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, *keys: str, now: float | None = None) -> int:
        """Seconds until the keys may try again; 0 when they may now."""
        now = time.time() if now is None else now
        with self._lock:
            until = max((self._locked_until.get(k, 0.0) for k in keys), default=0.0)
        return max(0, int(until - now + 0.999)) if until > now else 0

    def failed(self, *keys: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for k in keys:
                recent = [t for t in self._failures.get(k, []) if now - t <= LOCKOUT_WINDOW_S]
                recent.append(now)
                self._failures[k] = recent
                if len(recent) >= LOCKOUT_AFTER:
                    self._locked_until[k] = now + LOCKOUT_S
                    self._failures[k] = []

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)
                self._locked_until.pop(k, None)
