"""Integrations: optional connections to systems outside cwcli, off until turned on.

The contract (``Integration``) is what every integration implements: an id, a name,
a publisher, a settings schema whose ``secret`` fields are write-only, a connection
``test``, optional named ``actions`` (a check the setup page can run, such as looking
up a directory user), the hook points it ``provides``, and in ``hooks`` the function
behind each one. cwcli calls a hook point through ``provider(hook)``, the one
integration that is on and provides it, so official and community integrations plug
in the same way:

* ``sign_in``: ``(settings, secrets, username, password)`` -> the signed-in user
  (``core.directory.DirectoryUser``, or a dict with ``username``, ``role`` and
  optionally ``name`` and ``email``), or None for a wrong name or password. An
  optional ``current_role`` hook, ``(settings, secrets, username)`` -> role or None,
  lets a schedule confirm its owner is still an Admin.
* ``backup_target``: ``(settings, secrets)`` -> the store off-site copies go to
  (``BackupTarget``).
* ``dns_record``: ``(settings, secrets)`` -> the DNS zones a site's public name is
  written in (``DnsRecords``).
* ``proxy_host``: ``(settings, secrets)`` -> the reverse proxy that serves a site's
  public name and requests its certificate (``ProxyHosts``).

``core.publish`` owns what is read and written through the last two (what exists is
read before anything is written, and only what cwcli wrote is ever changed or
deleted), so a provider implements plain reads and writes.

Only one integration serves a hook point at a time: turning on a second one that
provides it is refused (``integration.hook_taken``) unless the Admin chooses to
switch (``take_over``), which turns the first one off - never silently.

Four official integrations use it now:

* ``ldap`` (``core.directory``) provides ``sign_in``: LDAP / Active Directory users
  sign in to the web UI with a role mapped from their directory groups.
* ``s3`` provides ``backup_target``: the bucket that receives the backup store's
  encrypted off-site copies (``core.backup_store``).
* ``cloudflare`` (``core.cloudflare``) provides ``dns_record``: a published site's A
  record.
* ``npm`` (``core.npm``) provides ``proxy_host``: a published site's Nginx Proxy
  Manager proxy host and Let's Encrypt certificate.

Community integrations (``core.community``) are installable packages an Admin adds
from a repository URL. Each registers one ``Integration`` under the
``cwcli.integrations`` entry-point group, shows as "Community, not reviewed"
(``publisher="community"``) and starts off. Community code loads only for the
Integrations page, its own id, and a hook point it is the one serving; while an
official integration serves a hook point, using it loads no community code.

The store is ``<cwcli home>/integrations.json`` (0600; 0660 to the cwcli group on a
shared install). Settings are plain JSON; every ``secret`` field is sealed with
AES-256-GCM under ``<cwcli home>/keys/integrations.key`` and bound to its
integration and field, so a value cannot be read back through the API or moved to
another field. A secret is replaced by sending a new value; an empty value keeps the
stored one, and nothing ever returns it.

Turning one on always runs its test first and enables it only when the test passes;
a change to an integration that is on is applied only when the new settings pass,
so a typo never takes a working sign-in or backup target down.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..utils import config_utils, shared_home
from .errors import CwcliError, ErrorKind

OFFICIAL = "official"
COMMUNITY = "community"

_lock = threading.RLock()
_depth = 0
# Stores already checked for a pre-integration S3 target (one scan per store per process).
_legacy_checked: set[Path] = set()


@dataclass(frozen=True, slots=True)
class Field:
    key: str
    label: str
    kind: str = "text"  # text | secret | bool | select | pem | group_roles
    required: bool = False
    default: Any = ""
    help: str = ""
    placeholder: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Check:
    ok: bool
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Integration:
    id: str
    name: str
    publisher: str
    description: str
    provides: tuple[str, ...]
    fields: tuple[Field, ...]
    # settings -> settings with defaults filled; raises CwcliError(USAGE) on bad input.
    normalize: Callable[[dict[str, Any]], dict[str, Any]]
    # (settings, secrets) -> Check. Never raises for a failed connection.
    test: Callable[[dict[str, Any], dict[str, str]], Check]
    # name -> (settings, secrets, args) -> dict
    actions: Mapping[str, Callable[[dict[str, Any], dict[str, str], dict[str, Any]], dict]] = field(
        default_factory=dict
    )
    # Runs after the integration is turned on, off, or changed.
    on_change: Callable[[], object] | None = None
    # Lines the Integrations page shows on the integration's card.
    notices: Callable[[], list[str]] | None = None
    # Settings naming where the secrets are sent: a change to any of them needs the
    # secrets typed again, so a stored one never goes to a server the request names.
    secrets_bound_to: tuple[str, ...] = ()
    # hook point -> the function cwcli calls for it (see the module docstring).
    hooks: Mapping[str, Callable[..., Any]] = field(default_factory=dict)

    def secret_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.kind == "secret")


HOOK_LABEL = {
    "sign_in": "sign-in",
    "backup_target": "the backup target",
    "dns_record": "DNS records",
    "proxy_host": "the proxy",
}


class BackupTarget(Protocol):
    """Where off-site copies go (``core.backup_store``): objects under string keys."""

    location: str  # shown to people, e.g. "s3://bucket" or "/srv/cwcli-offsite"

    def key(self, *parts: str) -> str: ...
    def ensure(self) -> None: ...
    def put_file(self, key: str, path: Path) -> None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_to_file(self, key: str, path: Path) -> int: ...
    def get_bytes(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str = "") -> list[dict[str, Any]]: ...


class DnsRecords(Protocol):
    """The ``dns_record`` hook point. A zone is ``{"id", "name"}``; a record is
    ``{"id", "type", "name", "content", "proxied", "comment"}``. A refusal raises
    ``CwcliError`` with a message that names its fix."""

    name: str  # shown to people, e.g. "Cloudflare"

    def zones(self) -> list[dict[str, Any]]: ...
    def records(self, zone_id: str, name: str) -> list[dict[str, Any]]: ...
    def record(self, zone_id: str, record_id: str) -> dict[str, Any] | None: ...
    def create(self, zone_id: str, record: dict[str, Any]) -> dict[str, Any]: ...
    def update(self, zone_id: str, record_id: str, record: dict[str, Any]) -> dict[str, Any]: ...
    def delete(self, zone_id: str, record_id: str) -> None: ...


class ProxyHosts(Protocol):
    """The ``proxy_host`` hook point. A host is Nginx Proxy Manager's proxy-host
    object (``id``, ``domain_names``, ``forward_host``, ``forward_port``,
    ``locations``, ``certificate_id``, ``owner_user_id``); a certificate is its
    certificate object (``id``, ``domain_names``, ``expires_on``)."""

    name: str
    challenge: str  # "http" | "dns": how certificates are validated

    def me(self) -> dict[str, Any]: ...
    def hosts(self) -> list[dict[str, Any]]: ...
    def host(self, host_id: int) -> dict[str, Any] | None: ...
    def create_host(self, spec: dict[str, Any]) -> dict[str, Any]: ...
    def update_host(self, host_id: int, spec: dict[str, Any]) -> dict[str, Any]: ...
    def delete_host(self, host_id: int) -> None: ...
    def certificates(self) -> list[dict[str, Any]]: ...
    def certificate(self, cert_id: int) -> dict[str, Any] | None: ...
    def request_certificate(self, domain: str) -> dict[str, Any]: ...
    def delete_certificate(self, cert_id: int) -> None: ...


def official() -> dict[str, Integration]:
    """The integrations cwcli ships, in display order."""
    from . import cloudflare, directory, npm

    return {
        i.id: i
        for i in (directory.INTEGRATION, S3_INTEGRATION, cloudflare.INTEGRATION, npm.INTEGRATION)
    }


def registry() -> dict[str, Integration]:
    """Every integration this install knows, in display order: the official ones,
    then the installed community ones (``core.community``), which this loads."""
    from . import community

    return {**official(), **community.load_all()}


def _find(integration_id: str) -> Integration | None:
    from . import community

    return official().get(integration_id) or community.load(integration_id)


def get(integration_id: str) -> Integration:
    found = _find(integration_id)
    if found is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "integration.unknown", f"No integration '{integration_id}'."
        )
    return found


# --------------------------------------------------------------------- the store


def store_path() -> Path:
    return config_utils.cwcli_home() / "integrations.json"


def key_path() -> Path:
    return config_utils.cwcli_home() / "keys" / "integrations.key"


@contextmanager
def _locked() -> Iterator[None]:
    """This process's lock plus the store's file lock, so a job process writing an
    outcome never undoes a change the daemon just saved. Reentrant within a thread."""
    global _depth
    with _lock:
        if _depth:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        from .schedules import StoreLock

        with StoreLock(store_path()):
            _depth = 1
            try:
                yield
            finally:
                _depth = 0


def ensure_adopted() -> None:
    """Run the one-time adoption of a pre-integration S3 target now (it otherwise runs
    on the first store read), so a backup-settings write can never drop it first."""
    with _locked():
        _load()


def _load() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"version": 1, "integrations": {}}
    except (OSError, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "integrations.unreadable",
            f"{store_path()} is unreadable: {e}",
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("integrations"), dict):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "integrations.unreadable",
            f"{store_path()} is not an integrations store",
        )
    if "s3" not in data["integrations"] and store_path() not in _legacy_checked:
        _legacy_checked.add(store_path())
        adopted = _adopt_legacy_s3()
        if adopted is not None:
            data["integrations"]["s3"], secret_file = adopted
            _save(data)
            secret_file.unlink(missing_ok=True)
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


def _key() -> bytes:
    path = key_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        shared_home.secure_dir_shared_only(path.parent)
        raw = os.urandom(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, shared_home.file_mode(0o600))
        except FileExistsError:  # another process made it first: use theirs
            return path.read_bytes()
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        shared_home.secure_file_shared_only(path)
        shared_home.apply_group(path)
    if len(raw) != 32:
        raise CwcliError(
            ErrorKind.PRECONDITION, "integrations.key_corrupt", f"{path} is not a 32-byte key."
        )
    return raw


def _seal(value: str, aad: str) -> str:
    nonce = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(nonce, value.encode("utf-8"), aad.encode("utf-8"))
    return "v1:" + base64.b64encode(nonce + sealed).decode("ascii")


def _unseal(token: str, aad: str) -> str:
    try:
        raw = base64.b64decode(token.removeprefix("v1:"))
        return AESGCM(_key()).decrypt(raw[:12], raw[12:], aad.encode("utf-8")).decode("utf-8")
    except (InvalidTag, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "integrations.secret_unreadable",
            f"A stored secret ({aad}) cannot be decrypted with this machine's key; "
            "set it again in Settings > Integrations.",
        ) from e


def _secrets(integ: Integration, rec: dict) -> dict[str, str]:
    stored = rec.get("secrets") or {}
    return {k: _unseal(stored[k], f"{integ.id}/{k}") for k in integ.secret_keys() if stored.get(k)}


# ------------------------------------------------------------------------ views


def _state(rec: dict) -> str:
    if rec.get("enabled"):
        return "error" if rec.get("last_error") else "on"
    if rec.get("settings") and rec.get("last_test") and not rec["last_test"].get("ok"):
        return "needs_setup"
    return "off"


def _view(integ: Integration, rec: dict) -> dict[str, Any]:
    stored = rec.get("secrets") or {}
    return {
        "id": integ.id,
        "name": integ.name,
        "publisher": integ.publisher,
        "description": integ.description,
        "provides": list(integ.provides),
        "fields": [asdict(f) for f in integ.fields],
        "actions": sorted(integ.actions),
        "state": _state(rec),
        "enabled": bool(rec.get("enabled")),
        "settings": rec.get("settings") or {},
        "secrets_set": {k: bool(stored.get(k)) for k in integ.secret_keys()},
        "last_test": rec.get("last_test"),
        "last_error": rec.get("last_error"),
        "notices": integ.notices() if integ.notices is not None else [],
        "updated_at": rec.get("updated_at"),
        "updated_by": rec.get("updated_by"),
        "community": None,
    }


def _community_view(integration_id: str, rec: dict) -> dict[str, Any]:
    from . import community

    keys = ("url", "ref", "commit", "publisher", "signed_in_with", "installed_at")
    return {
        **{k: rec.get(k) for k in keys},
        "installed_by": rec.get("installed_by"),
        "updated_at": rec.get("updated_at"),
        "updated_by": rec.get("updated_by"),
        "load_error": community.load_error(integration_id),
    }


def view(integration_id: str | None = None) -> list[dict[str, Any]]:
    """What the Integrations page shows: never a secret, only whether one is set. One
    card loads only that integration."""
    if integration_id is None:
        known = list(registry().values())
    else:
        found = _find(integration_id)
        known = [found] if found is not None else []
    with _locked():
        data = _load()
    records, installed = data["integrations"], data.get("community") or {}
    out = []
    for integ in known:
        card = _view(integ, records.get(integ.id) or {})
        if integ.id in installed:
            card["community"] = _community_view(integ.id, installed[integ.id])
        out.append(card)
    return out


def active(integration_id: str) -> tuple[dict[str, Any], dict[str, str]] | None:
    """(settings, secrets) of an integration that is on, else None. The runtime read
    a hook point uses (sign-in, an off-site copy)."""
    integ = get(integration_id)
    with _locked():
        rec = _load()["integrations"].get(integration_id) or {}
    if not rec.get("enabled"):
        return None
    return dict(rec.get("settings") or {}), _secrets(integ, rec)


def _provides(integration_id: str, data: dict) -> tuple[str, ...]:
    """The hook points an integration provides, read without loading community code."""
    found = official().get(integration_id)
    if found is not None:
        return found.provides
    rec = (data.get("community") or {}).get(integration_id) or {}
    return tuple(rec.get("provides") or ())


def _holders(data: dict, integration_id: str, hooks: tuple[str, ...]) -> list[tuple[str, str]]:
    """(id, hook) for each OTHER integration that is on and provides one of ``hooks``."""
    return [
        (other, hook)
        for other, rec in data["integrations"].items()
        if other != integration_id and rec.get("enabled")
        for hook in _provides(other, data)
        if hook in hooks
    ]


def provider(hook: str) -> tuple[Integration, dict[str, Any], dict[str, str]] | None:
    """The integration that is on and provides ``hook``, with its settings and
    secrets, or None. How cwcli calls a hook point; only the one serving it loads."""
    with _locked():
        data = _load()
    for integration_id, rec in data["integrations"].items():
        if rec.get("enabled") and hook in _provides(integration_id, data):
            integ = get(integration_id)
            return integ, dict(rec.get("settings") or {}), _secrets(integ, rec)
    return None


def serving(hook: str) -> tuple[str, str] | None:
    """(id, name) of the integration that is on and provides ``hook``, without
    loading community code or unsealing a secret: what a page shows."""
    with _locked():
        data = _load()
    for integration_id, rec in data["integrations"].items():
        if rec.get("enabled") and hook in _provides(integration_id, data):
            return integration_id, _name(integration_id, data)
    return None


def _name(integration_id: str, data: dict) -> str:
    found = official().get(integration_id)
    rec = (data.get("community") or {}).get(integration_id) or {}
    return found.name if found is not None else str(rec.get("name") or integration_id)


def _refuse_hook_taken(integ: Integration, holders: list[tuple[str, str]], data: dict) -> None:
    other, hook = holders[0]
    raise CwcliError(
        ErrorKind.CONFLICT,
        "integration.hook_taken",
        f"{_name(other, data)} already provides {HOOK_LABEL.get(hook, hook)}, and only one "
        f"integration serves it at a time. Turning {integ.name} on switches "
        f"{HOOK_LABEL.get(hook, hook)} to it and turns {_name(other, data)} off.",
        hint=f"Turn {_name(other, data)} off first, or choose to switch.",
        detail={"hook": hook, "current": other, "current_name": _name(other, data)},
    )


# ---------------------------------------------------------------------- changes


def _candidate(
    integ: Integration,
    rec: dict,
    settings: dict | None,
    secrets: dict | None,
    *,
    complete: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The stored values with the request's laid over them, normalized, and (when
    ``complete``, i.e. for a save) with every required field present. An empty secret
    keeps the stored one."""
    if settings is not None and not isinstance(settings, dict):
        raise CwcliError(ErrorKind.USAGE, "integration.settings", "settings must be an object.")
    if secrets is not None and (
        not isinstance(secrets, dict) or any(not isinstance(v, str) for v in secrets.values())
    ):
        raise CwcliError(
            ErrorKind.USAGE, "integration.secrets", "secrets must be an object of strings."
        )
    known = {f.key for f in integ.fields if f.kind != "secret"}
    stored = rec.get("settings") or {}
    merged = {**stored, **{k: v for k, v in (settings or {}).items()}}
    merged = integ.normalize({k: v for k, v in merged.items() if k in known})
    typed = {k: v for k, v in (secrets or {}).items() if k in integ.secret_keys() and v}
    moved = [
        f.label
        for f in integ.fields
        if f.key in integ.secrets_bound_to and merged.get(f.key) != stored.get(f.key)
    ]
    plain = _secrets(integ, rec)
    if moved:
        dropped = [f.label for f in integ.fields if f.key in plain and f.key not in typed]
        if dropped:
            raise CwcliError(
                ErrorKind.USAGE,
                "integration.secret_retype",
                f"A changed {', '.join(moved)} needs {', '.join(dropped)} typed again.",
            )
    plain.update(typed)
    missing = [
        f.label
        for f in integ.fields
        if f.required and not (plain.get(f.key) if f.kind == "secret" else merged.get(f.key))
    ]
    if missing and complete:
        raise CwcliError(
            ErrorKind.USAGE,
            "integration.incomplete",
            f"{integ.name} needs: {', '.join(missing)}.",
        )
    return merged, plain


def _record(integration_id: str) -> dict:
    return _load()["integrations"].get(integration_id) or {}


def test(integration_id: str, settings: dict | None = None, secrets: dict | None = None) -> Check:
    """Test the stored settings with the request's laid over them. Saves nothing."""
    integ = get(integration_id)
    with _locked():
        rec = _record(integration_id)
    merged, plain = _candidate(integ, rec, settings, secrets, complete=False)
    return integ.test(merged, plain)


def run_action(
    integration_id: str,
    action: str,
    args: dict | None = None,
    settings: dict | None = None,
    secrets: dict | None = None,
) -> dict:
    """An integration's named check (``ldap``'s ``lookup_user``) against the stored
    settings with the request's laid over them. Saves nothing."""
    integ = get(integration_id)
    fn = integ.actions.get(action)
    if fn is None:
        raise CwcliError(
            ErrorKind.USAGE,
            "integration.action_unknown",
            f"{integ.name} has no action '{action}'.",
        )
    with _locked():
        rec = _record(integration_id)
    merged, plain = _candidate(integ, rec, settings, secrets, complete=False)
    return fn(merged, plain, args or {})


def configure(
    integration_id: str,
    settings: dict | None = None,
    secrets: dict | None = None,
    *,
    enable: bool | None = None,
    take_over: bool = False,
    by: str = "",
    now: float | None = None,
) -> tuple[Check | None, dict[str, Any]]:
    """Save settings and secrets, and turn the integration on when ``enable``.

    Whenever the result would be on (``enable``, or a change to one already on) the
    test runs first and nothing is applied unless it passes. A failed first setup is
    still saved, off, as "needs setup", so the form is not lost. Turning on one whose
    hook point another integration serves is refused (``integration.hook_taken``)
    unless ``take_over``, which turns the other one off once the test passes. Returns
    the test's answer (None when nothing was tested) and the integration's new view.
    """
    integ = get(integration_id)
    now = time.time() if now is None else now
    with _locked():
        data = _load()
        rec = data["integrations"].get(integration_id) or {}
    merged, plain = _candidate(integ, rec, settings, secrets)
    on = bool(rec.get("enabled")) if enable is None else enable
    holders = _holders(data, integration_id, integ.provides) if on else []
    if holders and not take_over:
        _refuse_hook_taken(integ, holders, data)
    # The test runs outside the lock: a slow server must not hold up sign-in.
    check = integ.test(merged, plain) if on else None
    if check is not None and not check.ok and rec.get("enabled"):
        return check, view(integration_id)[0]
    result = {"ok": check.ok, "message": check.message, "at": now} if check else None
    switched: list[str] = []
    with _locked():
        data = _load()
        if on and (check is None or check.ok):
            holders = _holders(data, integration_id, integ.provides)
            if holders and not take_over:
                _refuse_hook_taken(integ, holders, data)
            for other, _hook in holders:
                data["integrations"][other].update(
                    enabled=False, last_error=None, updated_at=now, updated_by=by
                )
                switched.append(other)
        data["integrations"][integration_id] = {
            "enabled": bool(on and (check is None or check.ok)),
            "settings": merged,
            "secrets": {k: _seal(v, f"{integ.id}/{k}") for k, v in plain.items()},
            "last_test": result
            or (data["integrations"].get(integration_id) or {}).get("last_test"),
            "last_error": None,
            "updated_at": now,
            "updated_by": by,
        }
        _save(data)
    for other in dict.fromkeys(switched):
        changed = get(other)
        if changed.on_change is not None:
            changed.on_change()
    if integ.on_change is not None:
        integ.on_change()
    return check, view(integration_id)[0]


def disable(integration_id: str, *, by: str = "", now: float | None = None) -> dict[str, Any]:
    """Turn an integration off; its settings and secrets stay for the next time."""
    integ = get(integration_id)
    now = time.time() if now is None else now
    with _locked():
        data = _load()
        rec = data["integrations"].get(integration_id)
        if rec is not None and rec.get("enabled"):
            rec.update(enabled=False, last_error=None, updated_at=now, updated_by=by)
            _save(data)
    if integ.on_change is not None:
        integ.on_change()
    return view(integration_id)[0]


def record_outcome(integration_id: str, error: str | None, *, now: float | None = None) -> None:
    """A hook point's runtime outcome: an error turns an "on" integration's state to
    "error" until the next success. Writes only when the state changes."""
    now = time.time() if now is None else now
    with _locked():
        data = _load()
        rec = data["integrations"].get(integration_id)
        if rec is None or not rec.get("enabled"):
            return
        if error is None and rec.get("last_error") is None:
            return
        if error is not None and (rec.get("last_error") or {}).get("message") == error:
            return
        rec["last_error"] = None if error is None else {"message": error, "at": now}
        _save(data)


# --------------------------------------------------------------- S3 backup target


def _s3_normalize(settings: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlsplit

    prefix = settings.get("prefix")
    out = {
        "endpoint": str(settings.get("endpoint") or "").strip().rstrip("/"),
        "bucket": str(settings.get("bucket") or "").strip(),
        "region": str(settings.get("region") or "").strip() or "us-east-1",
        "prefix": "cwcli-backups" if prefix is None else str(prefix).strip().strip("/"),
        "access_key": str(settings.get("access_key") or "").strip(),
    }
    u = urlsplit(out["endpoint"])
    if out["endpoint"] and (u.scheme not in ("http", "https") or not u.netloc):
        raise CwcliError(
            ErrorKind.USAGE,
            "s3.endpoint",
            f"Not an S3 endpoint URL: {out['endpoint']!r} (https://host or http://host:port).",
        )
    return out


def s3_target(settings: dict[str, Any], secrets: dict[str, str]):
    from . import s3 as core_s3

    return core_s3.S3Target(
        endpoint=settings["endpoint"],
        bucket=settings["bucket"],
        region=settings["region"],
        access_key=settings["access_key"],
        secret_key=secrets.get("secret_key", ""),
        prefix=settings["prefix"],
    )


class S3Store:
    """The S3 bucket as the backup store's ``BackupTarget``; the bucket's own settings
    (``endpoint``, ``bucket``, ...) read through from the target."""

    def __init__(self, target) -> None:
        from . import s3 as core_s3

        self.target = target
        self._client = core_s3.S3Client(target)
        self.location = f"s3://{target.bucket}"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._client if hasattr(self._client, name) else self.target, name)

    def key(self, *parts: str) -> str:
        return str(self.target.key(*parts))

    def ensure(self) -> None:
        self._client.ensure_bucket()


def _s3_test(settings: dict[str, Any], secrets: dict[str, str]) -> Check:
    from . import s3 as core_s3

    verdict = core_s3.probe(s3_target(settings, secrets))
    if verdict["ok"]:
        return Check(
            True,
            f"The bucket answers; {verdict['objects']} object(s) under the prefix.",
            {"objects": verdict["objects"]},
        )
    return Check(False, str(verdict["error"]))


def _s3_notices() -> list[str]:
    from . import backup_store

    return [
        f"Off-site copies of {project} are paused: its old target "
        f"{old.get('endpoint') or '-'} (bucket {old.get('bucket') or '-'}) was not carried "
        "over. Set the bucket up here, then turn copies back on in "
        f"{project}'s backup settings."
        for project, old in backup_store.paused_legacy().items()
    ]


S3_INTEGRATION = Integration(
    id="s3",
    name="S3 backup target",
    publisher=OFFICIAL,
    description="Encrypted off-site copies of every instance's backups in an S3 bucket "
    "(AWS, MinIO, Garage, Ceph).",
    provides=("backup_target",),
    fields=(
        Field(
            "endpoint",
            "Endpoint",
            required=True,
            placeholder="https://s3.eu-west-1.amazonaws.com or http://minio:9000",
        ),
        Field("bucket", "Bucket", required=True, help="Created when it does not exist."),
        Field("prefix", "Prefix", default="cwcli-backups", help="Copies land under this path."),
        Field("access_key", "Access key", required=True),
        Field("secret_key", "Secret key", kind="secret", required=True),
        Field("region", "Region", default="us-east-1"),
    ),
    normalize=_s3_normalize,
    test=_s3_test,
    secrets_bound_to=("endpoint",),
    notices=_s3_notices,
    hooks={"backup_target": lambda settings, secrets: S3Store(s3_target(settings, secrets))},
)


def _adopt_legacy_s3() -> tuple[dict, Path] | None:
    """The bucket an instance's backup settings named before the S3 target became an
    integration: the first instance (by name) with copies on and a stored secret is
    adopted, on, and its plaintext secret file is removed; only the instances that were
    copying to that bucket keep copying, and one that copied elsewhere is paused with a
    notice (``backup_store.keep_legacy_copy_choices``). Runs once - an ``s3`` record, on or off, means
    it has run."""
    from . import backup_store

    found = backup_store.legacy_s3_target()
    if found is None:
        return None
    settings, secret, secret_file = found
    try:
        rec = {
            "enabled": True,
            "settings": _s3_normalize(settings),
            "secrets": {"secret_key": _seal(secret, "s3/secret_key")},
            "last_test": None,
            "last_error": None,
            "updated_at": time.time(),
            "updated_by": "adopted from instance backup settings",
        }
    except CwcliError:
        return None
    backup_store.keep_legacy_copy_choices(settings, secret_file)
    return rec, secret_file
