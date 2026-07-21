"""``core.restore`` - the plan/apply slice for cwcli's most destructive path.

`restore` is batch 11 of the logic-core rework, and the command the plan
earmarked for the destructive-preview (plan/apply) boundary. The split is a
**read/destroy safety separation**, not merely an efficiency win:

- :func:`restore_plan` (and its receive-mode sibling :func:`receive_plan`) is the
  PURE, repeatable, secret-free resolve - container/bench/site, the backup scan,
  the selection, the missing-apps check, and the origin comparison - returning a
  :class:`RestorePlan` that DESCRIBES the destructive action without performing
  it. It is the natural home of a future read-only "what would restore do".
- :func:`restore_apply` is the single guarded destructive act: it alone takes the
  MariaDB credentials and the ``consent``, and it runs ``bench restore --force``
  (which drops and recreates the site's database), merges the encryption key, and
  migrates + restarts.

The destructive execs stay BUFFERED ``exec_run`` (the ``core.backup`` shape, not
the exec-stream contract): a buffered exec blocks to completion and returns a real
``int`` code, so it is already honest; the contract's polling exists for the
STREAMING consumers restore never was. Secrets ride ``exec_run(environment=)``
byte-exactly as the pre-migration path (M5); no secret value ever appears on the
argv, in an event, in a warning, or in a DTO field.

Two decisions the frontend keeps: the interactive backup menu and the two
confirms are UI (the core surfaces ``select_backup`` and ``confirm_restore`` as
typed choices), and the sendme send/receive subprocess plus the ticket prompt
stay in ``commands/restore.py`` (interactive host I/O the core does not consume,
the ``commands/logs.py`` ``-it`` precedent). What crosses to the core from the
sendme flows is only the CONTAINER I/O: the streamed ``put_archive`` copy-in and
``get_archive`` copy-out (M4), and the plan/apply itself.

There is deliberately NO ``axi restore`` verb (see the no-verb assertion in the
tests): a ``bench restore --force`` that destroys a user's site data is a product
decision the captain owns on its own evidence (the ``axi apps uninstall``
deferral class - ``axi apps install`` and ``axi init`` were once deferred the
same way and have since shipped on their own separate evidence). It is
DEFERRED, not structurally refused - the plan/apply shape makes the verb thin
whenever it is decided.
"""

from __future__ import annotations

import json
import re
import shlex
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..utils import bench_sites, db_utils
from . import docker as core_docker
from . import resolvers
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind


# --------------------------------------------------------------------------- #
# DTOs (plain, serializable data; no live Docker object, no argv, no secret).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class RestorePlan:
    """The resolved, previewable description of a destructive restore.

    Carries only container-path strings and plain scalars - never a container
    handle - so it crosses a ``core.<verb>`` return boundary and a GUI/axi can
    render the preview and decide.
    """

    project_name: str
    site: str
    bench_path: str
    database_path: str  # full container path to the backup's DB dump
    files_path: str | None
    private_files_path: str | None
    site_config_backup_path: str | None
    backup_filename: str
    backup_timestamp: str  # "%Y-%m-%d %H:%M:%S"
    restore_items: list[str]  # ["Database", "Public files", "Private files"]
    missing_apps: list[str] = field(default_factory=list)
    origin_mismatch: str | None = None  # the backup's origin site when it differs
    is_receive: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreReport:
    """The typed outcome of an applied restore (NO password field)."""

    site: str
    bench_path: str
    restored: bool
    included_files: bool
    encryption_key_updated: bool | None
    migrate_ran: bool
    migrate_ok: bool
    restarted: bool
    restart_log: str | None = None


# --------------------------------------------------------------------------- #
# Typed events (progress + verbose traces; user notes ride Result.warnings).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreStep:
    """A phase label the frontend renders as a spinner title."""

    phase: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreTrace:
    """A verbose ``$ cmd`` / diagnostic echo the frontend shows under ``-v``."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreNotice:
    """A note the frontend renders (``info`` dim, ``warning`` yellow)."""

    text: str
    level: str = "info"


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreOutput:
    """Raw restore/migrate output the frontend shows under ``-v`` or on failure."""

    text: str


RestoreEvent = RestoreStep | RestoreTrace | RestoreNotice | RestoreOutput
OnEvent = Callable[[RestoreEvent], None]


def _noop(_event: RestoreEvent) -> None:
    return None


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


# --------------------------------------------------------------------------- #
# Pure helpers - moved verbatim from commands/restore.py (no UI).
# --------------------------------------------------------------------------- #
def parse_backup_filename(filename: str) -> dict | None:
    """Parse a Frappe backup filename into components, or None if invalid."""
    name_without_ext = filename
    extensions: list[str] = []
    while "." in name_without_ext:
        name_without_ext, ext = name_without_ext.rsplit(".", 1)
        extensions.insert(0, ext)

    pattern = r"^(\d{8}_\d{6})-([^-]+)-(.+)$"
    match = re.match(pattern, name_without_ext)
    if not match:
        return None

    timestamp_str, site_name, backup_type = match.groups()
    try:
        timestamp = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return None

    return {
        "filename": filename,
        "timestamp": timestamp,
        "timestamp_str": timestamp_str,
        "site_name": site_name,
        "backup_type": backup_type,
        "extensions": extensions,
        "full_extension": ".".join(extensions),
        "compressed": "gz" in extensions or "tgz" in extensions,
        "is_database": backup_type == "database",
        "is_files": backup_type in ("files", "private-files"),
        "is_config": backup_type == "site_config_backup",
    }


def transform_site_name_to_backup_format(site_name: str) -> str:
    """Transform a site name to backup-filename format (dots -> underscores)."""
    return site_name.replace(".", "_")


def group_backup_sets(backups: list) -> dict:
    """Group backup files by timestamp into backup sets (DB-bearing sets only)."""
    backup_sets: dict = {}
    for backup in backups:
        ts = backup["timestamp_str"]
        if ts not in backup_sets:
            backup_sets[ts] = {
                "timestamp": backup["timestamp"],
                "timestamp_str": ts,
                "site_name": backup["site_name"],
                "site_dir": backup["site_dir"],
                "database": None,
                "files": None,
                "private_files": None,
                "site_config_backup": None,
            }
        if backup["is_database"]:
            backup_sets[ts]["database"] = backup
        elif backup["backup_type"] == "files":
            backup_sets[ts]["files"] = backup
        elif backup["backup_type"] == "private-files":
            backup_sets[ts]["private_files"] = backup
        elif backup["backup_type"] == "site_config_backup":
            backup_sets[ts]["site_config_backup"] = backup

    return {ts: bset for ts, bset in backup_sets.items() if bset["database"] is not None}


def group_and_sort_backups(backups: list, target_site: str) -> tuple:
    """Split backup sets into (target-site, other-site), newest-first each."""
    target_site_backup_name = transform_site_name_to_backup_format(target_site)
    all_backup_sets = group_backup_sets(backups)

    target_backups: list = []
    other_backups: list = []
    for backup_set in all_backup_sets.values():
        if (
            backup_set["site_name"] == target_site_backup_name
            or backup_set["site_dir"] == target_site
        ):
            target_backups.append(backup_set)
        else:
            other_backups.append(backup_set)

    target_backups.sort(key=lambda x: x["timestamp"], reverse=True)
    other_backups.sort(key=lambda x: x["timestamp"], reverse=True)
    return target_backups, other_backups


def select_backup_set(
    target_backups: list,
    other_backups: list,
    *,
    latest: bool,
    backup_file: str | None,
    warnings: list[Message] | None = None,
) -> dict | None:
    """Resolve a backup set non-interactively (pure). See the module docstring.

    ``--latest`` returns the newest TARGET-site backup (never falls through to
    other sites); ``--backup-file`` matches by filename or full path across both
    lists. A multi-match note rides ``warnings`` (the ``console.print`` the CLI
    version used cannot live in the core).
    """
    if latest:
        return target_backups[0] if target_backups else None

    if backup_file is not None:
        matches: list[dict] = []
        for bset in target_backups + other_backups:
            db = bset.get("database")
            if not db:
                continue
            if backup_file in (db.get("filename"), db.get("full_path")):
                matches.append(bset)
        if not matches:
            return None
        if len(matches) > 1 and warnings is not None:
            warnings.append(
                Message(
                    "backup.multi_match",
                    f"multiple backups match '{backup_file}'; using the first match.",
                )
            )
        return matches[0]

    return None


# --------------------------------------------------------------------------- #
# Container-read helpers (buffered execs; diagnostics ride events/warnings).
# --------------------------------------------------------------------------- #
def scan_backups_for_all_sites(
    frappe_container, bench_path: str, *, on_event: OnEvent = _noop
) -> list:
    """Scan every site's backup directory, returning parsed backup dicts."""
    backups: list[dict] = []

    sites_path = f"{bench_path}/sites"
    quoted_sites_path = shlex.quote(sites_path)
    cmd = f'find {quoted_sites_path} -maxdepth 1 -mindepth 1 -type d -not -name "assets" -not -name "common_site_config.json"'
    on_event(RestoreTrace(text=cmd))

    exit_code, output = frappe_container.exec_run(cmd, workdir=bench_path)
    if exit_code != 0:
        on_event(RestoreNotice(text=f"Failed to list sites in {sites_path}", level="warning"))
        return backups

    sites = [s.strip() for s in _decode(output).strip().split("\n") if s.strip()]

    for site_path in sites:
        backup_dir = f"{site_path}/private/backups"
        quoted_backup_dir = shlex.quote(backup_dir)

        test_cmd = f"test -d {quoted_backup_dir}"
        exit_code, _ = frappe_container.exec_run(f"sh -c '{test_cmd}'")
        if exit_code != 0:
            continue

        list_cmd = f'find {quoted_backup_dir} -maxdepth 1 -type f \\( -name "*-database.sql*" -o -name "*-files.tar*" -o -name "*-files.tgz" -o -name "*-private-files.tar*" -o -name "*-private-files.tgz" -o -name "*-site_config_backup.json" \\) | sort -r'
        on_event(RestoreTrace(text=list_cmd))
        exit_code, output = frappe_container.exec_run(f"sh -c '{list_cmd}'")
        if exit_code != 0:
            continue

        files = [f.strip() for f in _decode(output).strip().split("\n") if f.strip()]
        site_name = site_path.split("/")[-1]

        for file_path in files:
            filename = file_path.split("/")[-1]
            parsed = parse_backup_filename(filename)
            if parsed:
                parsed["full_path"] = file_path
                parsed["site_dir"] = site_name
                backups.append(parsed)

    return backups


def _read_backup_installed_apps(
    frappe_container, backup_db_path: str, *, on_event: OnEvent = _noop
) -> set[str] | None:
    """Read the app names a backup's site had installed, from its DB dump.

    Returns None (fail-safe) when the list cannot be read, so the caller never
    warns on a false negative.
    """
    quoted = shlex.quote(backup_db_path)
    extract = (
        f"zcat -f {quoted} 2>/dev/null | " "grep -aoE \"\\[[^]]*\\]','installed_apps'\" | head -1"
    )
    on_event(RestoreTrace(text=extract))
    exit_code, output = frappe_container.exec_run(["sh", "-c", extract])
    if exit_code != 0:
        return None
    try:
        text = _decode(output).strip()
    except Exception:
        return None
    if not text:
        return None
    array_body = text.split("]", 1)[0]
    apps = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", array_body))
    return apps or None


def check_missing_apps(
    frappe_container,
    project_name: str,
    bench_path: str,
    backup_db_path: str,
    *,
    on_event: OnEvent = _noop,
    no_recache: bool = False,
) -> list[str]:
    """Apps the BACKUP needs that this bench does not physically have.

    Both sides read LIVE (the backup's dump and ``ls apps``), so this never
    touches cwcli's cache; ``no_recache`` is a DEPRECATED no-op kept for callers.
    Fail-safe: an unreadable dump / absent marker yields no warning.
    """
    quoted_apps_dir = shlex.quote(f"{bench_path}/apps")
    exit_code, output = frappe_container.exec_run(["sh", "-c", f"ls -1 {quoted_apps_dir}"])
    if exit_code != 0:
        on_event(RestoreNotice(text=f"Could not list apps in {bench_path}/apps.", level="warning"))
        return []
    available_apps = {a.strip() for a in _decode(output).split("\n") if a.strip()}

    backup_apps = _read_backup_installed_apps(frappe_container, backup_db_path, on_event=on_event)
    if backup_apps is None:
        on_event(
            RestoreNotice(
                text="Could not read the backup's installed apps; skipping the missing-apps check.",
                level="warning",
            )
        )
        return []

    return sorted(backup_apps - available_apps)


def _resolve_default_site(
    project_name: str, bench_path: str | None, frappe_container
) -> str | None:
    """The bench's default site, from EITHER source, with a LIVE fallback.

    Stricter than :func:`resolvers.resolve_default_site` (which reads only the
    cache and raises): the destructive restore path adds a LIVE
    ``sites/currentsite.txt`` read so a cold/stale cache still resolves the
    default. Kept private to this module rather than widening the shared resolver
    (which would change ``backup``/``unlock``) - the batch's reported flat spot.
    """
    site = db_utils.get_default_site(project_name, bench_path)
    if site:
        return site
    if frappe_container is not None and bench_path:
        site = bench_sites.read_current_site(frappe_container, bench_path)
    return site


def _resolve_bench(
    project_name: str, bench: str | None, bench_path: str | None, warnings: list[Message]
) -> Result[str] | None:
    """Belt-and-suspenders bench resolution (the ``core.backup`` shape).

    The frontend resolves the bench in its no-spinner PROLOGUE (container + bench +
    site, before the scan/download - byte-exact ordering, and the multi-bench
    error / no-cache inspect fallback stay in the CLI's ``resolve_bench_path`` +
    prologue), so this normally receives a concrete ``bench_path`` and passes it
    straight through. A ``NEEDS_CHOICE`` (multi-bench, no selector) is returned for
    the frontend; ``None`` means the resolved path is in ``warnings``' caller.
    """
    resolved = resolvers.resolve_bench(project_name, bench, bench_path)
    if resolved is None:
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
        return None
    if resolved.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=resolved.choice)
    assert resolved.data is not None
    warnings.extend(resolved.warnings)
    return Result(status=Status.OK, data=resolved.data)


# --------------------------------------------------------------------------- #
# The plan phase.
# --------------------------------------------------------------------------- #
def _build_plan(
    frappe_container,
    project_name: str,
    site: str,
    bench_path: str,
    backup_set: dict,
    *,
    is_receive: bool,
    on_event: OnEvent,
) -> RestorePlan:
    """Shared tail of both plan entry points: missing-apps + origin + DTO."""
    on_event(RestoreStep(phase="check_apps", message="Checking for missing apps"))
    db = backup_set["database"]
    db_path = db["full_path"]
    missing_apps = check_missing_apps(
        frappe_container, project_name, bench_path, db_path, on_event=on_event
    )

    origin_parsed = parse_backup_filename(db["filename"])
    origin_site = origin_parsed["site_name"] if origin_parsed else None
    origin_mismatch = (
        origin_site
        if (origin_site and origin_site != transform_site_name_to_backup_format(site))
        else None
    )

    restore_items = ["Database"]
    files = backup_set.get("files")
    private_files = backup_set.get("private_files")
    config_backup = backup_set.get("site_config_backup")
    if files:
        restore_items.append("Public files")
    if private_files:
        restore_items.append("Private files")

    timestamp = backup_set.get("timestamp")
    return RestorePlan(
        project_name=project_name,
        site=site,
        bench_path=bench_path,
        database_path=db_path,
        files_path=files["full_path"] if files else None,
        private_files_path=private_files["full_path"] if private_files else None,
        site_config_backup_path=config_backup["full_path"] if config_backup else None,
        backup_filename=db["filename"],
        backup_timestamp=timestamp.strftime("%Y-%m-%d %H:%M:%S") if timestamp else "",
        restore_items=restore_items,
        missing_apps=missing_apps,
        origin_mismatch=origin_mismatch,
        is_receive=is_receive,
    )


def _menu_options(target_backups: list, other_backups: list) -> list[dict]:
    """Serializable option rows for the ``select_backup`` choice (the frontend
    rebuilds today's rich menu from these; a minimal frontend uses value/label)."""
    options: list[dict] = []
    for group, sets in (("target", target_backups), ("other", other_backups)):
        for bset in sets:
            db = bset.get("database") or {}
            badges = []
            if bset.get("files"):
                badges.append("FILES")
            if bset.get("private_files"):
                badges.append("PRIVATE")
            options.append(
                {
                    "value": db.get("full_path", ""),
                    "label": bset["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                    "group": group,
                    "site_dir": bset.get("site_dir", ""),
                    "badges": badges,
                }
            )
    return options


def _match_selected(
    target_backups: list[dict], other_backups: list[dict], selected_backup: str
) -> dict | None:
    for bset in target_backups + other_backups:
        db = bset.get("database")
        if db and selected_backup in (db.get("filename"), db.get("full_path")):
            return bset
    return None


def restore_plan(
    project_name: str,
    *,
    site: str | None = None,
    latest: bool = False,
    backup_file: str | None = None,
    selected_backup: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[RestorePlan]:
    """The read-only resolve for the normal restore path. See the module docstring."""
    emit = on_event or _noop
    warnings: list[Message] = []

    frappe_container = core_docker.get_frappe_container(project_name)

    state = resolvers.resolve_container_state(project_name, frappe_container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    bench_result = _resolve_bench(project_name, bench, bench_path, warnings)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        bench_path = bench_result.data
    assert bench_path is not None

    if not site:
        site = _resolve_default_site(project_name, bench_path, frappe_container)
        if not site:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "site.no_default",
                "No site specified and no default site found in config.",
            )
        warnings.append(Message("default_site.resolved", f"Using default site: {site}"))

    resolvers.validate_site_name(site)
    resolvers.validate_bench_path(bench_path)
    resolvers.require_bench_dir(frappe_container, bench_path)
    resolvers.require_site_dir(frappe_container, bench_path, site)

    emit(RestoreStep(phase="scan", message="Scanning backups for all sites"))
    backups = scan_backups_for_all_sites(frappe_container, bench_path, on_event=emit)
    target_backups, other_backups = group_and_sort_backups(backups, site)

    if selected_backup is not None:
        selected = _match_selected(target_backups, other_backups, selected_backup)
    elif latest or backup_file is not None:
        selected = select_backup_set(
            target_backups, other_backups, latest=latest, backup_file=backup_file, warnings=warnings
        )
    else:
        # No selector: the interactive-menu decision the core cannot make.
        return Result(
            status=Status.NEEDS_CHOICE,
            warnings=warnings,
            choice=Choice(
                kind="select_backup",
                param="selected_backup",
                prompt="Select a backup to restore:",
                options=_menu_options(target_backups, other_backups),
            ),
        )

    if selected is None:
        selector_desc = (
            "--latest"
            if latest
            else (f"--backup-file {backup_file!r}" if backup_file is not None else "the selection")
        )
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "backup.no_match",
            f"no backup matched {selector_desc} for site '{site}'.",
        )

    plan = _build_plan(
        frappe_container, project_name, site, bench_path, selected, is_receive=False, on_event=emit
    )
    return Result(status=Status.OK, data=plan, warnings=warnings)


def receive_plan(
    project_name: str,
    *,
    site: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
    downloaded_files: list,
    on_event: OnEvent | None = None,
) -> Result[RestorePlan]:
    """The read-only resolve for ``--receive``: copy the downloaded files INTO the
    container (streamed), identify them, and build a :class:`RestorePlan`.

    ``downloaded_files`` are host paths the frontend obtained from ``sendme
    receive``; the sendme subprocess and the ticket prompt stay in the frontend.
    """
    emit = on_event or _noop
    warnings: list[Message] = []

    frappe_container = core_docker.get_frappe_container(project_name)

    state = resolvers.resolve_container_state(project_name, frappe_container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    bench_result = _resolve_bench(project_name, bench, bench_path, warnings)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        bench_path = bench_result.data
    assert bench_path is not None

    if not site:
        site = _resolve_default_site(project_name, bench_path, frappe_container)
        if not site:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "site.no_default",
                "No site specified and no default site found in config.",
            )
        warnings.append(Message("default_site.resolved", f"Using default site: {site}"))

    resolvers.validate_site_name(site)
    resolvers.validate_bench_path(bench_path)

    # Identify the downloaded components.
    files = [Path(f) for f in downloaded_files]
    database_file = files_archive = private_files_archive = config_backup = None
    for f in files:
        parsed = parse_backup_filename(f.name)
        if not parsed:
            continue
        bt = parsed["backup_type"]
        if bt == "database":
            database_file = f
        elif bt == "files":
            files_archive = f
        elif bt == "private-files":
            private_files_archive = f
        elif bt == "site_config_backup":
            config_backup = f

    if database_file is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "receive.no_database",
            "No database backup found in downloaded files.",
        )

    # Copy every downloaded file INTO the container's backup dir (streamed, M4).
    backup_dir = f"{bench_path}/sites/{site}/private/backups"
    _ensure_backup_dir(frappe_container, backup_dir, on_event=emit)
    for local_file in files:
        _put_archive_streamed(frappe_container, backup_dir, local_file)

    def _in_container(f: Path | None) -> dict | None:
        return {"full_path": f"{backup_dir}/{f.name}", "filename": f.name} if f else None

    ts_parsed = parse_backup_filename(database_file.name)
    backup_set = {
        "timestamp": ts_parsed["timestamp"] if ts_parsed else None,
        "database": _in_container(database_file),
        "files": _in_container(files_archive),
        "private_files": _in_container(private_files_archive),
        "site_config_backup": _in_container(config_backup),
    }
    plan = _build_plan(
        frappe_container, project_name, site, bench_path, backup_set, is_receive=True, on_event=emit
    )
    return Result(status=Status.OK, data=plan, warnings=warnings)


# --------------------------------------------------------------------------- #
# The apply phase (the guarded destructive act).
# --------------------------------------------------------------------------- #
def restore_apply(
    plan: RestorePlan,
    *,
    mariadb_root_username: str,
    mariadb_root_password: str,
    admin_password: str | None = None,
    consent: bool = False,
    no_migrate: bool = False,
    on_event: OnEvent | None = None,
) -> Result[RestoreReport]:
    """Perform the destructive restore described by ``plan``. See the module docstring.

    Returns ``NEEDS_CHOICE``/``confirm_restore`` when ``consent`` is not granted,
    so no frontend can bypass the destructive gate. A failed post-restore migrate
    is a ``WARNING`` carrying ``migrate_ok=False`` (the restore succeeded).
    """
    emit = on_event or _noop
    warnings: list[Message] = []

    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_restore",
                param="consent",
                prompt=f"This will replace all data in site '{plan.site}'. "
                "Are you sure you want to restore?",
            ),
        )

    resolvers.validate_site_name(plan.site)
    if any(char in mariadb_root_username for char in resolvers._INVALID_CHARS):
        raise CwcliError(
            ErrorKind.USAGE,
            "mariadb_username.invalid_chars",
            "Invalid MariaDB username. Username cannot contain special shell characters.",
        )

    frappe_container = core_docker.get_frappe_container(plan.project_name)

    exit_code, _ = frappe_container.exec_run(["test", "-f", plan.database_path])
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "backup.not_found",
            f"Backup file not found: {plan.database_path}",
        )

    # Secrets ride the environment, never the argv (M5).
    restore_env: dict[str, str] = {}
    cmd = f"bench --site {shlex.quote(plan.site)} restore {shlex.quote(plan.database_path)}"
    cmd += f" --mariadb-root-username {shlex.quote(mariadb_root_username)}"
    restore_env["CWCLI_MARIADB_ROOT_PASSWORD"] = mariadb_root_password
    cmd += ' --mariadb-root-password "$CWCLI_MARIADB_ROOT_PASSWORD"'
    cmd += " --force"
    if admin_password:
        restore_env["CWCLI_ADMIN_PASSWORD"] = admin_password
        cmd += ' --admin-password "$CWCLI_ADMIN_PASSWORD"'
    if plan.files_path:
        cmd += f" --with-public-files {shlex.quote(plan.files_path)}"
    if plan.private_files_path:
        cmd += f" --with-private-files {shlex.quote(plan.private_files_path)}"

    emit(RestoreTrace(text=cmd))  # the secret is a $-ref, never the value
    emit(RestoreStep(phase="restore", message=f"Restoring site '{plan.site}'"))
    exit_code, output = frappe_container.exec_run(
        ["sh", "-c", cmd], workdir=plan.bench_path, environment=restore_env
    )
    emit(RestoreOutput(text=_decode(output)))
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "restore.failed",
            f"Failed to restore site '{plan.site}'",
            detail={"output": _decode(output)},
        )

    encryption_key_updated: bool | None = None
    if plan.site_config_backup_path:
        encryption_key_updated = _restore_encryption_key(frappe_container, plan, emit, warnings)

    migrate_ran = not no_migrate
    migrate_ok = True
    restarted = False
    restart_log: str | None = None
    if not no_migrate:
        migrate_ok = _run_migrate(frappe_container, plan, emit, warnings)
        restarted, restart_log = _restart(plan, emit, warnings)

    report = RestoreReport(
        site=plan.site,
        bench_path=plan.bench_path,
        restored=True,
        included_files=bool(plan.files_path or plan.private_files_path),
        encryption_key_updated=encryption_key_updated,
        migrate_ran=migrate_ran,
        migrate_ok=migrate_ok,
        restarted=restarted,
        restart_log=restart_log,
    )
    status = Status.OK if (migrate_ok or not migrate_ran) else Status.WARNING
    return Result(status=status, data=report, warnings=warnings)


def _restore_encryption_key(
    frappe_container, plan: RestorePlan, emit: OnEvent, warnings: list[Message]
) -> bool | None:
    """Best-effort: merge the backup's ``encryption_key`` into the live site_config.

    Returns True on a successful merge, None otherwise (never raises; failures
    ride ``warnings``, exactly as the pre-migration best-effort step).
    """
    try:
        config_path = shlex.quote(plan.site_config_backup_path or "")
        exit_code, output = frappe_container.exec_run(
            ["sh", "-c", f"cat {config_path}"], workdir=plan.bench_path
        )
        if exit_code != 0:
            return None
        backup_config = json.loads(_decode(output))
        encryption_key = backup_config.get("encryption_key")
        if not encryption_key:
            return None

        site_config_path = f"{plan.bench_path}/sites/{plan.site}/site_config.json"
        quoted_site_config = shlex.quote(site_config_path)
        exit_code, output = frappe_container.exec_run(
            ["sh", "-c", f"cat {quoted_site_config}"], workdir=plan.bench_path
        )
        if exit_code != 0:
            return None
        current_config = json.loads(_decode(output))
        current_config["encryption_key"] = encryption_key

        updated_json = json.dumps(current_config, indent=1)
        write_cmd = f"cat > {quoted_site_config} << 'EOF'\n{updated_json}\nEOF"
        exit_code, _ = frappe_container.exec_run(["sh", "-c", write_cmd], workdir=plan.bench_path)
        if exit_code == 0:
            return True
        warnings.append(
            Message(
                "encryption_key.write_failed", "Failed to update encryption key in site_config."
            )
        )
        return None
    except Exception as e:  # noqa: BLE001 - best-effort, never fatal
        warnings.append(Message("encryption_key.error", f"Could not update encryption key: {e}"))
        return None


def _run_migrate(
    frappe_container, plan: RestorePlan, emit: OnEvent, warnings: list[Message]
) -> bool:
    """Run ``bench migrate`` (buffered). A failure/exception does NOT undo the
    restore; it is surfaced as a warning and returns False (the caller still
    restarts and the frontend exits non-zero)."""
    migrate_cmd = f"bench --site {shlex.quote(plan.site)} migrate"
    emit(RestoreTrace(text=migrate_cmd))
    emit(RestoreStep(phase="migrate", message=f"Migrating site '{plan.site}'"))
    try:
        exit_code, output = frappe_container.exec_run(
            ["sh", "-c", migrate_cmd], workdir=plan.bench_path
        )
    except Exception as exc:  # noqa: BLE001 - a Docker/API error is a reported failure
        exit_code = 1
        output = f"Failed to run migrate: {exc}".encode("utf-8", errors="replace")

    emit(RestoreOutput(text=_decode(output)))
    if exit_code == 0:
        return True
    warnings.append(
        Message(
            "migrate.failed",
            f"'bench migrate' failed for site '{plan.site}'. "
            "The database was restored, but the schema may be out of date.",
            detail={"output": _decode(output)},
        )
    )
    return False


def _restart(plan: RestorePlan, emit: OnEvent, warnings: list[Message]) -> tuple[bool, str | None]:
    """Restart the SAME bench that was restored, over ``core.start(restart=True)``."""
    from . import start as core_start

    try:
        result = core_start.start(plan.project_name, bench_path=plan.bench_path, restart=True)
    except CwcliError as e:
        warnings.append(Message("restart.failed", f"Instance restart failed: {e.message}"))
        return False, None
    # core.start's web-readiness timeout must not be silently dropped here: a
    # restored site that never begins serving is exactly the kind of thing this
    # safety-critical path must not hide.
    warnings.extend(w for w in result.warnings if w.code == "start.web_not_ready")
    if result.data is None:  # a resolved path never yields a choice
        return False, None
    return True, result.data.log_path


# --------------------------------------------------------------------------- #
# Send-path container I/O (the sendme subprocess stays frontend).
# --------------------------------------------------------------------------- #
def _ensure_backup_dir(frappe_container, backup_dir: str, *, on_event: OnEvent = _noop) -> None:
    quoted = shlex.quote(backup_dir)
    exit_code, _ = frappe_container.exec_run(f"sh -c 'test -d {quoted}'")
    if exit_code == 0:
        return
    exit_code, _ = frappe_container.exec_run(f"sh -c 'mkdir -p {quoted}'")
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "backup_dir.failed",
            f"Failed to create backup directory at {backup_dir}",
        )


def _put_archive_streamed(frappe_container, backup_dir: str, local_file: Path) -> None:
    """Stream a single host file INTO the container's backup dir (M4).

    The tar is written to a temp file and the OPEN HANDLE is passed to
    ``put_archive`` (docker-py streams it) rather than read whole into RAM; an
    unsuccessful copy fails closed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / f"{local_file.name}.tar"
        with tarfile.open(tar_path, "w") as tar:
            tar.add(local_file, arcname=local_file.name)
        with open(tar_path, "rb") as tar_file:
            copied = frappe_container.put_archive(backup_dir, tar_file)
    if not copied:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "copy.failed",
            f"Failed to copy '{local_file.name}' into the container.",
        )


def copy_backup_files_out(frappe_container, file_paths: list[str], dest_dir: Path) -> None:
    """Stream backup files OUT of the container into ``dest_dir`` (send path).

    Uses ``get_archive`` (docker-py streams the tar) and extracts each file to
    ``dest_dir`` under its bare name; raises ``PRECONDITION`` on a copy failure.
    """
    for file_path in file_paths:
        filename = file_path.split("/")[-1]
        try:
            bits, _ = frappe_container.get_archive(file_path)
            tar_path = dest_dir / f"{filename}.tar"
            with open(tar_path, "wb") as f:
                for chunk in bits:
                    f.write(chunk)
            with tarfile.open(tar_path, "r") as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        member.name = filename
                        tar.extract(member, dest_dir)
                        break
            tar_path.unlink()
        except CwcliError:
            raise
        except Exception as e:  # noqa: BLE001 - surface as a typed copy failure
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "copy.failed",
                f"Failed to copy {filename}: {e}",
            ) from e
