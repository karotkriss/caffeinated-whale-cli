"""The cwcli-owned backup store (captain decisions, 2026-10-27).

Sets live OUTSIDE the instances, under ``cwcli_home()/backups/<project>/<site>/<set>/``,
so they survive ``cwcli rm``; in shared mode the tree carries the shared group modes so
every operator of the box manages every instance's sets. A set is a VERIFIED copy-out
(rm's C1 gate, promoted): ``bench backup --with-files`` runs in the bench, this run's
artifacts stream out to the host, each part is checked (the dump gunzips to its end and
carries mariadb-dump's completion marker, the file tars list, the config parses), the
manifest records the verdict, and only a fully verified set has its in-bench copies
removed - so backups never fill the bench disk again (the staging16 move's lesson), and
a failed verification keeps every byte where it was.

Restoring a set composes the existing restore slice: the parts are copied back into the
target bench (``core.restore.receive_plan`` streams them in and builds the plan) and
``core.restore.restore_apply`` performs the guarded act; the site-config rule the captain
asked for (carry the app settings, keep the target's ``db_*`` keys, a pre-restore copy of
the target's config) is ``core.restore.merge_site_config``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..utils import db_utils, shared_home
from ..utils.config_utils import PROJECTS_DIR, cwcli_home
from . import docker as core_docker
from . import inspect as core_inspect
from . import resolvers
from . import restore as core_restore
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

STORE_DIRNAME = "backups"
MANIFEST = "set.json"
REASONS = (
    "manual",
    "scheduled",
    "before-core-update",
    "before-removal",
    "before-migrate",
    "before-install",
    "before-app-remove",
    "before-overwrite",
    "drill",
    "clone",  # phase 2: the fresh set a clone/move restores from
    "move",
    "received",  # filed from another machine's ticket
)
DB_DUMP_MARKER = b"Dump completed"
# A set id, with the optional suffix adopt_dir adds when two adoptions share a second.
_SET_ID = re.compile(r"^\d{8}_\d{6}-b\d+(-\d+)?$")

# Retention and budget defaults (captain decision 4); every value is a per-instance setting.
DEFAULT_SETTINGS: dict[str, Any] = {
    "local": {"daily": 7, "weekly": 4, "monthly": 0},
    "s3": {"daily": 7, "weekly": 4, "monthly": 12},
    "safety": {"days": 14, "max_per_site": 5},
    "min_free_gb": 20,
    "stale_after_days": 7,
    "with_files": True,
    "s3_target": {
        "enabled": False,
        "endpoint": "",
        "bucket": "",
        "prefix": "cwcli-backups",
        "region": "us-east-1",
        "access_key": "",
        "copy_on_backup": True,
    },
    # Phase 3: one daily backup of every site at this local time, pruned after.
    "schedule": {"enabled": False, "daily_at": "02:00", "last_run": None},
}
SAFETY_REASONS = (
    "before-core-update",
    "before-migrate",
    "before-install",
    "before-app-remove",
    "before-overwrite",
)


# --------------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class StoreStep:
    phase: str
    message: str
    status: str = "start"  # start | ok | failed


@dataclass(frozen=True, slots=True)
class StoreNotice:
    text: str
    level: str = "info"


StoreEvent = StoreStep | StoreNotice
OnEvent = Callable[[Any], None]


def _noop(_event: Any) -> None:
    return None


# --------------------------------------------------------------------------- DTOs


@dataclass(frozen=True, slots=True)
class BackupPart:
    kind: str  # database | public_files | private_files | site_config
    filename: str
    size: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class BackupCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BackupSet:
    id: str
    project: str
    bench_index: int | None
    bench_path: str
    site: str
    created_at: str  # ISO 8601, UTC
    reason: str
    note: str | None
    parts: list[BackupPart]
    size: int
    verified: bool
    checks: list[BackupCheck]
    verified_at: str | None
    frappe_version: str | None
    apps: list[str]
    cwcli_version: str
    path: str  # host directory of the set
    in_bench_removed: bool = False
    # Phase 3: the encrypted off-site copy (bucket/prefix/uploaded_at) and the last
    # restore drill run from this set ({at, ok, detail}); None until each happens.
    offsite: dict[str, Any] | None = None
    drill: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return f"{self.project}/{self.site}/{self.id}"

    def part(self, kind: str) -> BackupPart | None:
        return next((p for p in self.parts if p.kind == kind), None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> BackupSet:
        return BackupSet(
            id=str(d["id"]),
            project=str(d["project"]),
            bench_index=d.get("bench_index"),
            bench_path=str(d.get("bench_path") or ""),
            site=str(d["site"]),
            created_at=str(d.get("created_at") or ""),
            reason=str(d.get("reason") or "manual"),
            note=d.get("note"),
            parts=[BackupPart(**p) for p in d.get("parts") or []],
            size=int(d.get("size") or 0),
            verified=bool(d.get("verified")),
            checks=[BackupCheck(**c) for c in d.get("checks") or []],
            verified_at=d.get("verified_at"),
            frappe_version=d.get("frappe_version"),
            apps=[str(a) for a in d.get("apps") or []],
            cwcli_version=str(d.get("cwcli_version") or ""),
            path=str(d.get("path") or ""),
            in_bench_removed=bool(d.get("in_bench_removed")),
            offsite=d.get("offsite") if isinstance(d.get("offsite"), dict) else None,
            drill=d.get("drill") if isinstance(d.get("drill"), dict) else None,
        )


# --------------------------------------------------------------------------- layout


def store_root() -> Path:
    root = cwcli_home() / STORE_DIRNAME
    shared_home.ensure_private_dir(root)
    return root


def _check_project(project: str) -> None:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", project):
        raise CwcliError(ErrorKind.USAGE, "project.invalid", f"Invalid project name {project!r}")


def _site_dir(project: str, site: str) -> Path:
    resolvers.validate_site_name(site)
    _check_project(project)
    d = store_root() / project / site
    return d


def _manifest_path(set_dir: Path) -> Path:
    return set_dir / MANIFEST


def _write_manifest(set_dir: Path, bset: BackupSet) -> None:
    tmp = _manifest_path(set_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bset.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, _manifest_path(set_dir))
    shared_home.secure_file_shared_only(_manifest_path(set_dir))


def _read_manifest(set_dir: Path) -> BackupSet | None:
    try:
        d = json.loads(_manifest_path(set_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        bset = BackupSet.from_dict(d)
    except (KeyError, TypeError, ValueError):
        return None
    if bset.path != str(set_dir):
        bset = BackupSet(**{**asdict(bset), "path": str(set_dir)})
    return bset


# --------------------------------------------------------------------------- reads


def list_sets(project: str | None = None, site: str | None = None) -> list[BackupSet]:
    """Every set in the store (newest first), narrowed by project and site when given."""
    root = store_root()
    out: list[BackupSet] = []
    projects = [root / project] if project else [p for p in root.iterdir() if p.is_dir()]
    for pdir in projects:
        if not pdir.is_dir():
            continue
        sites = [pdir / site] if site else [s for s in pdir.iterdir() if s.is_dir()]
        for sdir in sites:
            if not sdir.is_dir():
                continue
            for set_dir in sdir.iterdir():
                if not set_dir.is_dir() or not _manifest_path(set_dir).exists():
                    continue
                bset = _read_manifest(set_dir)
                if bset is not None:
                    out.append(bset)
    out.sort(key=lambda b: b.created_at, reverse=True)
    return out


def find_set(key: str) -> BackupSet | None:
    """A set by its ``project/site/id`` key (the shape every surface passes around)."""
    parts = key.split("/")
    if len(parts) != 3 or not _SET_ID.match(parts[2]):
        return None
    try:
        set_dir = _site_dir(parts[0], parts[1]) / parts[2]
    except CwcliError:
        return None
    return _read_manifest(set_dir) if _manifest_path(set_dir).exists() else None


def last_good(project: str, site: str) -> BackupSet | None:
    return next((b for b in list_sets(project, site) if b.verified), None)


def disk_budget(min_free_gb: float | None = None) -> dict[str, Any]:
    """Free space on the store's filesystem against the configured floor."""
    usage = shutil.disk_usage(store_root())
    floor = float(DEFAULT_SETTINGS["min_free_gb"] if min_free_gb is None else min_free_gb)
    free_gb = usage.free / 1e9
    store_bytes = sum(b.size for b in list_sets())
    return {
        "free_gb": round(free_gb, 1),
        "total_gb": round(usage.total / 1e9, 1),
        "store_gb": round(store_bytes / 1e9, 2),
        "min_free_gb": floor,
        "warn": free_gb < floor,
        "root": str(store_root()),
    }


# --------------------------------------------------------------------------- settings


def _settings_path(project: str) -> Path:
    return PROJECTS_DIR / project / "backups.json"


def settings(project: str) -> dict[str, Any]:
    """The instance's backup settings over the defaults (decision 4: per-instance values)."""
    merged: dict[str, Any] = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        stored = json.loads(_settings_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if isinstance(stored, dict):
        for k, v in stored.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
    return merged


def save_settings(project: str, values: dict[str, Any]) -> dict[str, Any]:
    current = settings(project)
    for k, v in values.items():
        if k not in DEFAULT_SETTINGS:
            raise CwcliError(ErrorKind.USAGE, "backups.setting_unknown", f"Unknown setting {k!r}")
        if isinstance(v, dict) and isinstance(current.get(k), dict):
            current[k].update(v)
        else:
            current[k] = v
    path = _settings_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    shared_home.secure_file_shared_only(path)
    return current


# --------------------------------------------------------------------------- verification


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_database(path: Path) -> BackupCheck:
    """The dump gunzips to its end and carries mariadb-dump's completion marker."""
    try:
        if path.stat().st_size == 0:
            return BackupCheck("database", False, "empty dump")
        tail = b""
        with gzip.open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                tail = (tail + chunk)[-4096:]
        if DB_DUMP_MARKER not in tail:
            return BackupCheck("database", False, "dump ends without 'Dump completed' (truncated?)")
        return BackupCheck("database", True, "gzip intact, dump complete")
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        return BackupCheck("database", False, f"unreadable: {exc}")


def _check_tar(kind: str, path: Path) -> BackupCheck:
    try:
        n = 0
        with tarfile.open(path, mode="r:*") as tar:
            for _m in tar:
                n += 1
        return BackupCheck(kind, True, f"{n} entries")
    except (OSError, tarfile.TarError, EOFError) as exc:
        return BackupCheck(kind, False, f"archive unreadable: {exc}")


def _check_config(path: Path) -> BackupCheck:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return BackupCheck("site_config", False, "not a JSON object")
        return BackupCheck("site_config", True, f"{len(data)} keys")
    except (OSError, ValueError) as exc:
        return BackupCheck("site_config", False, f"unreadable: {exc}")


def _run_checks(set_dir: Path, parts: list[BackupPart]) -> list[BackupCheck]:
    checks: list[BackupCheck] = []
    for p in parts:
        path = set_dir / p.filename
        if p.kind == "database":
            checks.append(_check_database(path))
        elif p.kind in ("public_files", "private_files"):
            checks.append(_check_tar(p.kind, path))
        elif p.kind == "site_config":
            checks.append(_check_config(path))
    if not any(p.kind == "database" for p in parts):
        checks.append(BackupCheck("database", False, "no database dump in the set"))
    return checks


def verify_set(bset: BackupSet) -> BackupSet:
    """Re-run every check on a stored set and record the verdict in its manifest."""
    set_dir = Path(bset.path)
    checks = _run_checks(set_dir, bset.parts)
    for p in bset.parts:
        if (
            p.sha256
            and (set_dir / p.filename).is_file()
            and _sha256(set_dir / p.filename) != p.sha256
        ):
            checks.append(
                BackupCheck("sha256", False, f"{p.filename} does not match its recorded sha256")
            )
    updated = BackupSet(
        **{
            **asdict(bset),
            "checks": checks,
            "verified": all(c.ok for c in checks),
            "verified_at": _now(),
        }
    )
    updated = BackupSet(**{**asdict(updated), "parts": bset.parts, "checks": checks})
    _write_manifest(set_dir, updated)
    return updated


# --------------------------------------------------------------------------- capture


def _replace(bset: BackupSet, **changes: Any) -> BackupSet:
    from dataclasses import replace

    return replace(bset, **changes)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind_of(filename: str) -> str | None:
    parsed = core_restore.parse_backup_filename(filename)
    if not parsed:
        return None
    bt = parsed["backup_type"]
    return {
        "database": "database",
        "files": "public_files",
        "private-files": "private_files",
        "site_config_backup": "site_config",
    }.get(bt)


def _site_apps(project: str, bench_path: str, site: str) -> tuple[str | None, list[str]]:
    """Frappe's version and the app names from the cache (install-time registry data)."""
    try:
        data = db_utils.get_cached_project_data(project) or {}
    except Exception:
        return None, []
    for bench in data.get("bench_instances") or []:
        if bench.get("path") != bench_path:
            continue
        for s in bench.get("sites") or []:
            if s.get("name") != site:
                continue
            apps = [str(a).split()[0] for a in s.get("installed_apps") or [] if str(a).strip()]
            frappe_version = next(
                (
                    str(a).split()[1]
                    for a in s.get("installed_apps") or []
                    if str(a).split()[:1] == ["frappe"] and len(str(a).split()) > 1
                ),
                None,
            )
            return frappe_version, apps
    return None, []


def capture(
    project: str,
    *,
    site: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    with_files: bool = True,
    reason: str = "manual",
    note: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[BackupSet]:
    """Back one site up INTO the store: ``bench backup``, verified copy-out, manifest.

    A set that fails verification is kept (marked unverified, its reason in ``checks``)
    and the in-bench copies are left in place; only a verified set has them removed.
    """
    emit = on_event or _noop
    warnings: list[Message] = []
    if reason not in REASONS:
        raise CwcliError(
            ErrorKind.USAGE, "backups.reason", f"reason must be one of {', '.join(REASONS)}"
        )

    emit(StoreStep("resolve", "Resolving the site and bench"))
    container = core_docker.get_frappe_container(project)
    state = resolvers.resolve_container_state(project, container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)
    bench_result = core_inspect.resolve_bench_with_fallback(project, bench, bench_path)
    if bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    bench_path = bench_result.data
    warnings.extend(bench_result.warnings)
    assert bench_path is not None
    if not site:
        site = resolvers.resolve_sole_or_require_site(project, bench_path)
        warnings.append(Message("default_site.resolved", f"Using site '{site}'"))
    resolvers.validate_site_name(site)
    resolvers.validate_bench_path(bench_path)
    resolvers.require_bench_dir(container, bench_path)
    site_path = resolvers.require_site_dir(container, bench_path, site)
    bench_index = _bench_index(project, bench_path)
    emit(StoreStep("resolve", "Resolved", "ok"))

    backup_dir = f"{site_path}/private/backups"
    container.exec_run(["mkdir", "-p", backup_dir])

    emit(
        StoreStep(
            "backup", f"Running bench backup for {site}" + (" with files" if with_files else "")
        )
    )
    cmd = ["bench", "--site", site, "backup"] + (["--with-files"] if with_files else [])
    code, out = container.exec_run(cmd, workdir=bench_path)
    if code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "backup.failed",
            f"bench backup failed for site '{site}'",
            detail={"output": _decode(out)},
        )
    emit(StoreStep("backup", "bench backup finished", "ok"))

    # This run's artifacts share the newest leading timestamp token (rm's rule).
    code, ls_out = container.exec_run(["ls", "-1t", backup_dir])
    names = [n.strip() for n in _decode(ls_out).splitlines() if n.strip()] if code == 0 else []
    if not names:
        raise CwcliError(
            ErrorKind.PRECONDITION, "backup.no_artifacts", "bench backup wrote nothing"
        )
    run_ts = names[0].split("-", 1)[0]
    current = [n for n in names if n.split("-", 1)[0] == run_ts and _kind_of(n)]

    set_id = f"{run_ts}-b{bench_index if bench_index is not None else 0}"
    set_dir = _site_dir(project, site) / set_id
    set_dir.mkdir(parents=True, exist_ok=True)
    for d in (set_dir.parent.parent, set_dir.parent, set_dir):
        shared_home.secure_dir_shared_only(d)

    emit(StoreStep("copy", f"Copying {len(current)} artifacts into the store"))
    parts: list[BackupPart] = []
    copy_failed: list[str] = []
    for name in current:
        dest = set_dir / name
        if not core_docker.stream_container_file(container, f"{backup_dir}/{name}", dest):
            copy_failed.append(name)
            continue
        shared_home.secure_file_shared_only(dest)
        kind = _kind_of(name) or "other"
        parts.append(
            BackupPart(kind=kind, filename=name, size=dest.stat().st_size, sha256=_sha256(dest))
        )
    emit(
        StoreStep(
            "copy",
            "Copied" if not copy_failed else f"{len(copy_failed)} artifacts failed to copy",
            "ok" if not copy_failed else "failed",
        )
    )

    emit(StoreStep("verify", "Verifying the set"))
    checks = _run_checks(set_dir, parts)
    for name in copy_failed:
        checks.append(BackupCheck("copy", False, f"{name} did not copy out whole"))
    verified = bool(parts) and all(c.ok for c in checks)
    emit(
        StoreStep(
            "verify",
            "Verified" if verified else "Verification failed",
            "ok" if verified else "failed",
        )
    )

    removed = False
    if verified:
        emit(
            StoreStep("cleanup", "Removing the in-bench copies (the store holds the verified set)")
        )
        code, _ = container.exec_run(["rm", "-f", *[f"{backup_dir}/{n}" for n in current]])
        removed = code == 0
        emit(
            StoreStep(
                "cleanup",
                "Removed" if removed else "Could not remove; left in the bench",
                "ok" if removed else "failed",
            )
        )
    else:
        emit(
            StoreNotice(
                "The in-bench copies were left in place because the set did not verify.", "warning"
            )
        )

    frappe_version, apps = _site_apps(project, bench_path, site)
    bset = BackupSet(
        id=set_id,
        project=project,
        bench_index=bench_index,
        bench_path=bench_path,
        site=site,
        created_at=_now(),
        reason=reason,
        note=note,
        parts=parts,
        size=sum(p.size for p in parts),
        verified=verified,
        checks=checks,
        verified_at=_now(),
        frappe_version=frappe_version,
        apps=apps,
        cwcli_version=__version__,
        path=str(set_dir),
        in_bench_removed=removed,
    )
    _write_manifest(set_dir, bset)
    budget = disk_budget(settings(project).get("min_free_gb"))
    if budget["warn"]:
        warnings.append(
            Message(
                "backups.low_disk",
                f"The backup store's disk has {budget['free_gb']} GB free, under the {budget['min_free_gb']} GB floor.",
            )
        )
    return Result(status=Status.OK if verified else Status.WARNING, data=bset, warnings=warnings)


def _bench_index(project: str, bench_path: str) -> int | None:
    try:
        data = db_utils.get_cached_project_data(project) or {}
    except Exception:
        return None
    for bench in data.get("bench_instances") or []:
        if bench.get("path") == bench_path:
            idx = bench.get("index")
            return int(idx) if idx is not None else None
    return None


def _decode(output: Any) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


# --------------------------------------------------------------------------- adopt


def adopt_dir(
    project: str,
    site: str,
    source_dir: Path,
    *,
    bench_path: str = "",
    reason: str = "before-removal",
    note: str | None = None,
    move: bool = True,
) -> BackupSet | None:
    """Turn a directory of bench-backup artifacts already on the host (rm's and
    rm-site's verified archives) into a store set, so a removal's backup lives where
    every other backup does. Fail-SOFT by design: the removal gates have already proven
    the copy; a problem here returns None and never undoes or blocks the removal."""
    try:
        files = [f for f in sorted(source_dir.iterdir()) if f.is_file() and _kind_of(f.name)]
    except OSError:
        return None
    if not files:
        return None
    try:
        run_ts = files[0].name.split("-", 1)[0]
        bench_index = _bench_index(project, bench_path) if bench_path else None
        set_id = f"{run_ts}-b{bench_index if bench_index is not None else 0}"
        set_dir = _site_dir(project, site) / set_id
        if set_dir.exists():
            set_id = f"{run_ts}-b{bench_index if bench_index is not None else 0}-{int(time.time()) % 100000}"
            set_dir = _site_dir(project, site) / set_id
        set_dir.mkdir(parents=True)
        for d in (set_dir.parent.parent, set_dir.parent, set_dir):
            shared_home.secure_dir_shared_only(d)
        parts: list[BackupPart] = []
        for f in files:
            dest = set_dir / f.name
            if move:
                shutil.move(str(f), str(dest))
            else:
                shutil.copy2(str(f), str(dest))
            shared_home.secure_file_shared_only(dest)
            parts.append(
                BackupPart(
                    kind=_kind_of(f.name) or "other",
                    filename=f.name,
                    size=dest.stat().st_size,
                    sha256=_sha256(dest),
                )
            )
        checks = _run_checks(set_dir, parts)
        frappe_version, apps = _site_apps(project, bench_path, site) if bench_path else (None, [])
        bset = BackupSet(
            id=set_id,
            project=project,
            bench_index=bench_index,
            bench_path=bench_path,
            site=site,
            created_at=_now(),
            reason=reason,
            note=note,
            parts=parts,
            size=sum(p.size for p in parts),
            verified=all(c.ok for c in checks),
            checks=checks,
            verified_at=_now(),
            frappe_version=frappe_version,
            apps=apps,
            cwcli_version=__version__,
            path=str(set_dir),
            in_bench_removed=False,
        )
        _write_manifest(set_dir, bset)
        return bset
    except Exception:  # noqa: BLE001 - adopting is a courtesy after a proven gate
        return None


# --------------------------------------------------------------------------- delete


def delete_set(bset: BackupSet, *, consent: bool = False) -> Result[dict[str, Any]]:
    """Remove a set from the store. Never the site's last good one (decision 4)."""
    if not consent:
        from .envelope import Choice

        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_delete_backup",
                param="consent",
                prompt=f"Delete backup {bset.id} of {bset.site} on {bset.project}?",
            ),
        )
    good = [b for b in list_sets(bset.project, bset.site) if b.verified]
    if bset.verified and len(good) == 1 and good[0].id == bset.id:
        raise CwcliError(
            ErrorKind.CONFLICT,
            "backups.last_good",
            f"{bset.id} is the last good backup of {bset.site}; it is never deleted.",
            hint="Take a new verified backup first.",
        )
    shutil.rmtree(bset.path, ignore_errors=False)
    return Result(status=Status.OK, data={"deleted": bset.key, "bytes": bset.size})


# ---------------------------------------------------------------------------- off-site


def _s3_secret_path(project: str) -> Path:
    _check_project(project)
    return cwcli_home() / "backups" / "keys" / f"s3-{project}.secret"


def set_s3_secret(secret: str, *, project: str) -> None:
    """Store an instance's S3 secret key on this machine only (mode 0600); never in settings."""
    p = _s3_secret_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    shared_home.secure_dir_shared_only(p.parent)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secret)
    shared_home.secure_file_shared_only(p)


def offsite_target(project: str, *, require: bool = True):
    """The instance's S3 target with its secret, or None when off-site copies are off."""
    from . import s3 as core_s3

    cfg = settings(project).get("s3_target") or {}
    if not cfg.get("enabled"):
        if require:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "offsite.disabled",
                f"Off-site copies are not configured for {project}.",
                hint="cwcli backups s3 configure <project> --endpoint ... --bucket ... --access-key ...",
            )
        return None
    try:
        secret = _s3_secret_path(project).read_text(encoding="utf-8").strip()
    except OSError:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "offsite.secret_missing",
            "The S3 secret key is not stored on this machine.",
            hint="cwcli backups s3 configure <project> (reads CWCLI_S3_SECRET_KEY)",
        ) from None
    return core_s3.S3Target(
        endpoint=str(cfg.get("endpoint") or ""),
        bucket=str(cfg.get("bucket") or ""),
        region=str(cfg.get("region") or "us-east-1"),
        access_key=str(cfg.get("access_key") or ""),
        secret_key=secret,
        prefix=str(cfg.get("prefix") or ""),
    )


def _crypto_context(bset: BackupSet, filename: str) -> bytes:
    return f"{bset.project}/{bset.site}/{bset.id}/{filename}".encode()


def _offsite_key(target: Any, bset: BackupSet, filename: str) -> str:
    return str(target.key(bset.project, bset.site, bset.id, filename))


def offsite_copy(bset: BackupSet, *, on_event: OnEvent | None = None) -> BackupSet:
    """Encrypt every part under this machine's key and upload it with the manifest.
    The manifest in the bucket is the local one plus ``offsite`` (no secrets ride in it)."""
    from . import backup_crypto
    from . import s3 as core_s3

    emit = on_event or _noop
    target = offsite_target(bset.project)
    key = backup_crypto.load_key()
    client = core_s3.S3Client(target)
    client.ensure_bucket()
    set_dir = Path(bset.path)
    tmp = set_dir / ".offsite.tmp"
    uploaded = []
    try:
        for part in bset.parts:
            src = set_dir / part.filename
            emit(StoreStep("offsite", f"Encrypting and uploading {part.filename}"))
            backup_crypto.encrypt_file(src, tmp, key, context=_crypto_context(bset, part.filename))
            remote = _offsite_key(target, bset, part.filename + ".cwenc")
            client.put_file(remote, tmp)
            uploaded.append(
                {
                    "kind": part.kind,
                    "key": remote,
                    "size": tmp.stat().st_size,
                    "sha256": part.sha256,
                }
            )
            emit(StoreStep("offsite", f"Uploaded {part.filename}", "ok"))
    finally:
        tmp.unlink(missing_ok=True)
    remote_prefix = str(target.key(bset.project, bset.site, bset.id))
    updated = _replace(
        bset,
        offsite={
            "endpoint": target.endpoint,
            "bucket": target.bucket,
            "prefix": remote_prefix,
            "uploaded_at": _now(),
            "parts": uploaded,
            "encrypted": True,
        },
    )
    _write_manifest(set_dir, updated)
    client.put_bytes(_offsite_key(target, bset, "set.json"), json.dumps(updated.to_dict()).encode())
    emit(StoreNotice(f"{bset.key} copied off-site to s3://{target.bucket}/{remote_prefix}"))
    return updated


def offsite_list(project: str) -> list[dict[str, Any]]:
    """Every set the bucket holds for ``project`` (its manifests), newest first, each
    marked ``local`` when this machine's store still has it."""
    from . import s3 as core_s3

    target = offsite_target(project)
    client = core_s3.S3Client(target)
    local = {b.key for b in list_sets(project)}
    out = []
    for obj in client.list(target.key(project) + "/"):
        if not obj["key"].endswith("/set.json"):
            continue
        try:
            d = json.loads(client.get_bytes(obj["key"]).decode("utf-8"))
        except (ValueError, CwcliError):
            continue
        d["key"] = f"{d.get('project')}/{d.get('site')}/{d.get('id')}"
        d["local"] = d["key"] in local
        d["remote_manifest"] = obj["key"]
        out.append(d)
    out.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return out


def offsite_fetch(project: str, key: str, *, on_event: OnEvent | None = None) -> BackupSet:
    """Download and decrypt a set from the bucket into this machine's store (the set
    keeps its id and reason; it is verified again on arrival)."""
    from . import backup_crypto
    from . import s3 as core_s3

    emit = on_event or _noop
    target = offsite_target(project)
    client = core_s3.S3Client(target)
    crypto_key = backup_crypto.load_key(create=False)
    _p, site, set_id = (key.split("/", 2) + ["", ""])[:3]
    if _p != project or not _SET_ID.match(set_id):
        raise CwcliError(ErrorKind.USAGE, "offsite.bad_key", f"{key!r} is not a set of {project}")
    set_dir = _site_dir(project, site) / set_id
    manifest_key = target.key(_p, site, set_id, "set.json")
    d = json.loads(client.get_bytes(manifest_key).decode("utf-8"))
    remote = BackupSet.from_dict(d)
    for part in remote.parts:
        if part.filename != os.path.basename(part.filename) or _kind_of(part.filename) is None:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "offsite.bad_manifest",
                f"The bucket's manifest for {key} names an unsafe part {part.filename!r}.",
            )
    if set_dir.exists():
        raise CwcliError(
            ErrorKind.CONFLICT, "offsite.local_exists", f"{key} is already in the local store."
        )
    set_dir.mkdir(parents=True)
    for dd in (set_dir.parent.parent, set_dir.parent, set_dir):
        shared_home.secure_dir_shared_only(dd)
    tmp = set_dir / ".download.tmp"
    try:
        for part in remote.parts:
            emit(StoreStep("offsite", f"Downloading and decrypting {part.filename}"))
            client.get_to_file(target.key(_p, site, set_id, part.filename + ".cwenc"), tmp)
            backup_crypto.decrypt_file(
                tmp,
                set_dir / part.filename,
                crypto_key,
                context=f"{project}/{site}/{set_id}/{part.filename}".encode(),
            )
            shared_home.secure_file_shared_only(set_dir / part.filename)
            emit(StoreStep("offsite", f"Fetched {part.filename}", "ok"))
    except Exception:
        shutil.rmtree(set_dir, ignore_errors=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)
    local = _replace(
        remote, project=project, site=site, id=set_id, path=str(set_dir), in_bench_removed=True
    )
    _write_manifest(set_dir, local)
    verified = verify_set(local)
    emit(
        StoreNotice(
            f"{key} fetched from s3://{target.bucket}; {'verified' if verified.verified else 'did NOT verify'}"
        )
    )
    return verified


def offsite_prune(
    project: str, *, consent: bool = False, dry_run: bool = False, on_event: OnEvent | None = None
) -> Result[dict[str, Any]]:
    """Apply the S3 tier's retention (7 daily / 4 weekly / 12 monthly by default) to the
    bucket's copies of ``project``; the newest verified copy per site is never removed."""
    from . import s3 as core_s3

    emit = on_event or _noop
    remote = offsite_list(project)
    sets = []
    for d in remote:
        try:
            sets.append(
                BackupSet.from_dict(
                    {k: v for k, v in d.items() if k not in ("key", "local", "remote_manifest")}
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    plan = _plan_for(project, sets, tier="s3", low_disk=False)
    doomed = [d for d in plan if not d.keep]
    if dry_run or not doomed:
        return Result(
            status=Status.OK,
            data={
                "project": project,
                "removed": [],
                "would_remove": [asdict(d) for d in doomed],
                "kept": sum(d.keep for d in plan),
            },
        )
    if not consent:
        from .envelope import Choice

        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_prune",
                param="consent",
                prompt=f"Remove {len(doomed)} off-site copies of {project} outside the S3 retention?",
            ),
        )
    target = offsite_target(project)
    client = core_s3.S3Client(target)
    removed = []
    for dec in doomed:
        _p, site, set_id = dec.key.split("/", 2)
        emit(StoreStep("offsite_prune", f"Removing s3 copy {dec.key} ({dec.why})"))
        for obj in client.list(target.key(_p, site, set_id) + "/"):
            client.delete(obj["key"])
        removed.append(asdict(dec))
        emit(StoreStep("offsite_prune", f"Removed {dec.key}", "ok"))
        local = find_set(dec.key)
        if local is not None and local.offsite:
            _write_manifest(Path(local.path), _replace(local, offsite=None))
    return Result(
        status=Status.OK,
        data={
            "project": project,
            "removed": removed,
            "would_remove": [],
            "kept": sum(d.keep for d in plan),
        },
    )


# -------------------------------------------------------------------------- schedule


def schedule_due(project: str, now: datetime | None = None) -> bool:
    """Is the instance's daily backup due: the schedule is on, today's local ``daily_at``
    has passed, and no run was recorded today. A machine that was off at the hour runs
    on its next tick (run once on wake)."""
    cfg = settings(project).get("schedule") or {}
    if not cfg.get("enabled"):
        return False
    now = now or datetime.now().astimezone()
    try:
        hour, minute = (int(x) for x in str(cfg.get("daily_at") or "02:00").split(":", 1))
    except ValueError:
        hour, minute = 2, 0
    if (now.hour, now.minute) < (hour, minute):
        return False
    last = str(cfg.get("last_run") or "")
    return last[:10] != now.strftime("%Y-%m-%d")


def mark_schedule_run(project: str, when: datetime | None = None) -> None:
    when = when or datetime.now().astimezone()
    save_settings(project, {"schedule": {"last_run": when.strftime("%Y-%m-%dT%H:%M:%S%z")}})


def record_drill(bset: BackupSet, drill: dict[str, Any]) -> BackupSet:
    """Write a restore drill's verdict onto the set's manifest."""
    updated = _replace(bset, drill=drill)
    _write_manifest(Path(bset.path), updated)
    return updated


def last_drill(project: str, site: str) -> dict[str, Any] | None:
    """The newest drill verdict recorded on any of the site's sets."""
    drills = [b.drill for b in list_sets(project, site) if b.drill]
    drills.sort(key=lambda d: str(d.get("at") or ""), reverse=True)
    return drills[0] if drills else None


# -------------------------------------------------------------------------- retention


@dataclass(frozen=True, slots=True)
class PruneDecision:
    key: str
    site: str
    keep: bool
    why: str
    size: int
    offsite: bool


def retention_plan(project: str, *, tier: str = "local") -> list[PruneDecision]:
    """Which sets the instance's retention policy keeps and which it removes, per site
    (captain decision 4). ``tier`` is ``local`` (7 daily / 4 weekly / 0 monthly by default)
    or ``s3`` (7 / 4 / 12). Rules, in order:

    - a site's newest verified set is ALWAYS kept (never delete the last good backup);
    - safety sets (``before-*``) are kept for ``safety.days`` days, at most
      ``safety.max_per_site`` of them, newest first;
    - every other set competes for the daily, weekly and monthly slots: the newest set
      of each of the newest N days, M ISO weeks and L months is kept (a GFS rotation);
    - under the disk floor (``min_free_gb``), a set that has an off-site copy keeps only
      its site's newest local copy - the rest lean on S3; a set without one is never
      pruned harder than the policy says.
    """
    cfg = settings(project)
    low_disk = bool(disk_budget(cfg.get("min_free_gb")).get("warn")) if tier == "local" else False
    return _plan_for(project, list_sets(project), tier=tier, low_disk=low_disk)


def _plan_for(
    project: str, sets_in: list[BackupSet], *, tier: str, low_disk: bool
) -> list[PruneDecision]:
    cfg = settings(project)
    policy = cfg[tier] if tier in ("local", "s3") else cfg["local"]
    safety_days = float(cfg["safety"]["days"])
    safety_max = int(cfg["safety"]["max_per_site"])
    now = time.time()
    out: list[PruneDecision] = []
    by_site: dict[str, list[BackupSet]] = {}
    for b in sets_in:
        by_site.setdefault(b.site, []).append(b)
    for site, sets in by_site.items():
        sets.sort(key=lambda b: b.created_at, reverse=True)
        last_good = next((b for b in sets if b.verified), None)
        keep: dict[str, str] = {}
        if last_good is not None:
            keep[last_good.id] = "the site's last good backup"
        for b in sets:
            if not b.verified:
                keep[b.id] = "it did not verify; kept until you delete it by hand"
        safety = [b for b in sets if b.reason in SAFETY_REASONS and b.verified]
        for n, b in enumerate(safety):
            age_days = (now - _epoch(b.created_at)) / 86400
            if n < safety_max and age_days <= safety_days:
                keep.setdefault(
                    b.id,
                    f"safety backup, {age_days:.0f}d old (kept {safety_days:.0f}d, max {safety_max})",
                )
        regular = [b for b in sets if b.reason not in SAFETY_REASONS and b.verified]
        for label, count, bucket in (
            ("daily", int(policy["daily"]), lambda d: d.strftime("%Y-%m-%d")),
            ("weekly", int(policy["weekly"]), lambda d: "{}-W{:02d}".format(*d.isocalendar()[:2])),
            ("monthly", int(policy["monthly"]), lambda d: d.strftime("%Y-%m")),
        ):
            seen: list[str] = []
            for b in regular:  # newest first: the first set seen in a bucket is its newest
                key = bucket(_dt(b.created_at))
                if key in seen:
                    continue
                if len(seen) >= count:
                    break
                seen.append(key)
                keep.setdefault(b.id, f"{label} slot {key}")
        for b in sets:
            why = keep.get(b.id)
            if (
                why is not None
                and low_disk
                and b.verified
                and b.offsite
                and last_good is not None
                and b.id != last_good.id
            ):
                why = None
                reason = (
                    "disk under the floor; it has an off-site copy, only the newest stays local"
                )
            elif why is None:
                reason = "outside every retention slot"
            else:
                reason = why
            out.append(PruneDecision(b.key, site, why is not None, reason, b.size, bool(b.offsite)))
    return out


def _epoch(iso: str) -> float:
    return _dt(iso).timestamp()


def _dt(iso: str) -> datetime:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def prune(
    project: str, *, consent: bool = False, dry_run: bool = False, on_event: OnEvent | None = None
) -> Result[dict[str, Any]]:
    """Apply the local retention policy: a visible step that lists what it removed
    (decision 4). ``dry_run`` only reports; otherwise ``consent`` is required."""
    emit = on_event or _noop
    plan = retention_plan(project)
    doomed = [d for d in plan if not d.keep]
    if dry_run or not doomed:
        emit(
            StoreNotice(
                f"{len(doomed)} set{'s' if len(doomed) != 1 else ''} outside the policy"
                if doomed
                else "every set is within the retention policy"
            )
        )
        return Result(
            status=Status.OK,
            data={
                "project": project,
                "removed": [],
                "would_remove": [asdict(d) for d in doomed],
                "kept": sum(d.keep for d in plan),
                "bytes": 0,
            },
        )
    if not consent:
        from .envelope import Choice

        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_prune",
                param="consent",
                prompt=f"Remove {len(doomed)} backup set{'s' if len(doomed) != 1 else ''} of {project} outside its retention policy?",
            ),
        )
    removed: list[dict[str, Any]] = []
    freed = 0
    for d in doomed:
        bset = find_set(d.key)
        if bset is None:
            emit(
                StoreStep(
                    "prune", f"Kept {d.key}: it could not be read back from the store", "failed"
                )
            )
            continue
        emit(StoreStep("prune", f"Removing {d.key} ({d.why})"))
        try:
            result = delete_set(bset, consent=True)
        except CwcliError as exc:  # the last-good guard, belt and braces
            emit(StoreStep("prune", f"Kept {d.key}: {exc.message}", "failed"))
            continue
        freed += int((result.data or {}).get("bytes") or 0)
        removed.append({**asdict(d), "removed_at": _now()})
        emit(StoreStep("prune", f"Removed {d.key}", "ok"))
    emit(
        StoreNotice(
            f"Pruned {len(removed)} set{'s' if len(removed) != 1 else ''}, {freed / 1048576:.1f} MB freed"
        )
    )
    return Result(
        status=Status.OK,
        data={
            "project": project,
            "removed": removed,
            "would_remove": [],
            "kept": sum(d.keep for d in plan),
            "bytes": freed,
        },
    )


# --------------------------------------------------------------------------- restore


@dataclass(frozen=True, slots=True)
class RestoreFromSetReport:
    set_key: str
    project: str
    bench_path: str
    site: str
    restored: bool
    migrate_ran: bool
    migrate_ok: bool
    config_merged: bool
    config_backup_path: str | None
    post_checks: list[BackupCheck]
    report: dict[str, Any]


def restore_set(
    key: str,
    *,
    project: str,
    site: str,
    bench: str | None = None,
    bench_path: str | None = None,
    mariadb_root_username: str = "root",
    mariadb_root_password: str,
    admin_password: str | None = None,
    consent: bool = False,
    no_migrate: bool = False,
    config_mode: str = "carry",  # carry (app settings from the set, target db_* kept) | keep
    on_event: OnEvent | None = None,
) -> Result[RestoreFromSetReport]:
    """Restore a stored set into ``site`` on ``project`` (same site, a new site, another
    bench or another instance - the target is whatever the caller resolved)."""
    emit = on_event or _noop
    bset = find_set(key)
    if bset is None:
        raise CwcliError(ErrorKind.NOT_FOUND, "backups.set_not_found", f"No backup set {key}")
    if not bset.verified:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "backups.set_unverified",
            f"{bset.id} did not verify; it is not restored.",
            hint="Run verify on the set, or pick another.",
        )
    files = [str(Path(bset.path) / p.filename) for p in bset.parts if p.kind != "other"]
    emit(StoreStep("stage", f"Copying {len(files)} parts into {project}:{site}"))
    plan_result = core_restore.receive_plan(
        project,
        site=site,
        bench=bench,
        bench_path=bench_path,
        downloaded_files=files,
        on_event=emit,
    )
    if plan_result.status is Status.NEEDS_CHOICE or plan_result.data is None:
        return Result(
            status=plan_result.status, choice=plan_result.choice, warnings=plan_result.warnings
        )
    plan = plan_result.data
    emit(StoreStep("stage", "Parts in place, plan built", "ok"))
    pre_copy = None
    if config_mode == "carry" and consent:
        pre_copy = core_restore.snapshot_site_config(plan, on_event=emit)
    apply = core_restore.restore_apply(
        plan,
        mariadb_root_username=mariadb_root_username,
        mariadb_root_password=mariadb_root_password,
        admin_password=admin_password,
        consent=consent,
        no_migrate=True,  # the config merge must land BEFORE migrate reads the site
        on_event=emit,
    )
    if apply.status is Status.NEEDS_CHOICE or apply.data is None:
        return Result(status=apply.status, choice=apply.choice, warnings=apply.warnings)
    merged = False
    if config_mode == "carry":
        merged = core_restore.merge_site_config(plan, on_event=emit)
    migrate_ran = not no_migrate
    migrate_ok = True
    if migrate_ran:
        migrate_ok = core_restore.run_migrate_and_restart(plan, on_event=emit)
    checks = [BackupCheck(**c) for c in core_restore.post_checks(plan, on_event=emit)]
    report = RestoreFromSetReport(
        set_key=key,
        project=project,
        bench_path=plan.bench_path,
        site=plan.site,
        restored=True,
        migrate_ran=migrate_ran,
        migrate_ok=migrate_ok,
        config_merged=merged,
        config_backup_path=pre_copy,
        post_checks=checks,
        report=asdict(apply.data),
    )
    ok = migrate_ok and all(c.ok for c in checks)
    return Result(status=Status.OK if ok else Status.WARNING, data=report, warnings=apply.warnings)
