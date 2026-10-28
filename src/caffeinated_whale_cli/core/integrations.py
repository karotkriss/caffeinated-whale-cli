"""Integrations: optional connections to systems outside cwcli, off until turned on.

The contract (``Integration``) is what every integration implements: an id, a name,
a publisher, a settings schema whose ``secret`` fields are write-only, a connection
``test``, optional named ``actions`` (a check the setup page can run, such as looking
up a directory user), and the hook points it ``provides``. Two official
integrations use it now:

* ``ldap`` (``core.directory``) provides ``sign_in``: LDAP / Active Directory users
  sign in to the web UI with a role mapped from their directory groups.
* ``s3`` provides ``backup_target``: the bucket that receives the backup store's
  encrypted off-site copies (``core.backup_store``).

Community integrations (``publisher="community"``, shown as "Community, not
reviewed", off until an Admin installs one) are a later phase.

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
from typing import Any

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

    def secret_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.kind == "secret")


def registry() -> dict[str, Integration]:
    """Every integration this install knows, in display order."""
    from . import directory

    return {i.id: i for i in (directory.INTEGRATION, S3_INTEGRATION)}


def get(integration_id: str) -> Integration:
    found = registry().get(integration_id)
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
    }


def view(integration_id: str | None = None) -> list[dict[str, Any]]:
    """What the Integrations page shows: never a secret, only whether one is set."""
    with _locked():
        records = _load()["integrations"]
    out = [_view(i, records.get(i.id) or {}) for i in registry().values()]
    return [v for v in out if integration_id is None or v["id"] == integration_id]


def active(integration_id: str) -> tuple[dict[str, Any], dict[str, str]] | None:
    """(settings, secrets) of an integration that is on, else None. The runtime read
    a hook point uses (sign-in, an off-site copy)."""
    integ = get(integration_id)
    with _locked():
        rec = _load()["integrations"].get(integration_id) or {}
    if not rec.get("enabled"):
        return None
    return dict(rec.get("settings") or {}), _secrets(integ, rec)


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
    by: str = "",
    now: float | None = None,
) -> tuple[Check | None, dict[str, Any]]:
    """Save settings and secrets, and turn the integration on when ``enable``.

    Whenever the result would be on (``enable``, or a change to one already on) the
    test runs first and nothing is applied unless it passes. A failed first setup is
    still saved, off, as "needs setup", so the form is not lost. Returns the test's
    answer (None when nothing was tested) and the integration's new view.
    """
    integ = get(integration_id)
    now = time.time() if now is None else now
    with _locked():
        rec = _record(integration_id)
    merged, plain = _candidate(integ, rec, settings, secrets)
    on = bool(rec.get("enabled")) if enable is None else enable
    # The test runs outside the lock: a slow server must not hold up sign-in.
    check = integ.test(merged, plain) if on else None
    if check is not None and not check.ok and rec.get("enabled"):
        return check, view(integration_id)[0]
    result = {"ok": check.ok, "message": check.message, "at": now} if check else None
    with _locked():
        data = _load()
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
