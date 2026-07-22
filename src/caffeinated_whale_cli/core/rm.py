"""``core.remove`` - delete a project's containers, named volumes, network, and directory.

The migrated ``commands/rm.py:_remove_project`` and its data-destruction
helpers: the verified copy-out backup gate (C1), the container-removal loop, the
named-volume + project-directory deletion (issue #19), the project's own compose
network removal (by exact compose-project label, never a prune - see
``_remove_project_network``), the multi-bench per-bench backup fan-out, the
path-traversal name guard (H5), the honest-exit accounting (M11), and the
cache-clear-on-clean-removal rule. It prints, prompts, and exits nothing: it
emits progress and warnings through the typed-event ``on_event`` callback,
raises :class:`~.errors.CwcliError` for hard failures (invalid name, Docker
unreachable), and otherwise returns ``Result(status, RemovalOutcome(...))``
carrying the tri-state result as plain data.

This is ONE plain function, the ``core.backup``/``core.update`` shape, not a
plan/apply split: rm's confirmation is a frontend concern computed before any
core call, so there is no destructive preview for the core to compute (see
``openspec/changes/migrate-rm-core/design.md`` Decision 1). It deliberately does
NOT route through ``resolvers.resolve_container_state`` or ``get_frappe_container``
- rm is project-wide, must handle an orphan with no frappe service (the
``core.stop`` precedent), and a stopped project is NORMAL for rm (the frontend
transiently starts it before calling here; the not-running branch below is the
fail-closed backstop). ``cwcli axi rm`` now exists: the deferral was the
captain's own product decision, and he overturned it on 2026-07-21. The verb is
a thin renderer over this unchanged ``core.remove``, so this module's own
safety properties (the early fail-closed backup gate, the verified copy-out,
the not-running backstop) are unchanged and are shared by both frontends.

rm's ``_backup_sites`` is a copy-out-and-VERIFY gate, distinct from
``core.backup``'s in-container dump: it streams each artifact out of the
container and confirms the database dump landed non-empty on the host archive,
because the volume the in-container dump lives in is about to be deleted. Reusing
``core.backup`` would drop that verification, which IS the C1 gate.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..utils import bench_sites, db_utils
from ..utils.config_utils import PROJECTS_DIR, cwcli_home
from .docker import get_project_containers, get_project_networks, get_project_volumes
from .envelope import Result, Status
from .errors import CwcliError, ErrorKind
from .resolvers import DEFAULT_BENCH_PATH

# Marker in a Frappe backup filename that identifies the database dump - the one
# artifact a "backup" cannot be trusted without (Frappe names it
# ``<timestamp>-<site>-database.sql.gz``). Matched case-insensitively so an
# uncompressed ``.sql`` variant is still recognised.
_DB_DUMP_MARKER = "database.sql"


# --------------------------------------------------------------------------- DTO


@dataclass(frozen=True, slots=True, kw_only=True)
class RemovalOutcome:
    """The typed outcome of a removal (serializable, no live objects).

    ``failures`` non-empty means a requested step did not complete and the
    caller must exit non-zero; ``found=False`` means the project was genuinely
    absent (an exit-0 no-op for the human CLI). ``backup_ok`` is True unless a
    backup was attempted on the ``--volumes`` path and did not fully succeed.
    ``network_removed`` is True only if the project's own compose network was
    actually deleted this run - False covers BOTH "there was none to remove"
    and "it could not be removed"; the latter always adds an entry to
    ``failures`` too, so the two are only distinguishable together, mirroring
    how ``dir_removed`` already reports "was something deleted" rather than
    "is the project now clean".
    """

    project: str
    found: bool
    orphan: bool  # no containers, but volumes/dir remained
    containers_removed: int
    volumes_removed: int
    dir_removed: bool
    network_removed: bool
    backup_ok: bool
    failures: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- typed events


@dataclass(frozen=True, slots=True, kw_only=True)
class RmStep:
    """A spinner-label update (was ``status.update(...)``).

    ``label`` is plain text; ``style`` is a semantic colour token
    (``"cyan"`` archive/backup, ``"yellow"`` stop, ``"red"`` destroy) the
    frontend maps to its own styling - the core carries no rich markup.
    """

    label: str
    style: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RmNotice:
    """A dim stdout progress line (was ``console.print("  [dim]...[/dim]")``)."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RmWarning:
    """A yellow stderr warning, with an optional dim follow-up line."""

    text: str
    hint: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RmError:
    """A red stderr error line (a per-container removal failure)."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RmTrace:
    """A verbose-only dim stderr diagnostic (rendered only under ``-v``)."""

    text: str


RmEvent = RmStep | RmNotice | RmWarning | RmError | RmTrace

OnEvent = Callable[[RmEvent], None]


def _noop(_event: RmEvent) -> None:
    """The drain-and-discard consumption mode (no renderer)."""


# ---------------------------------------------------- public H5 path-safety guards
#
# Public so the CLI keeps its up-front pre-filter loop byte-for-byte; ``remove``
# re-checks its own param (a core function must not trust its caller's discipline).


def is_safe_project_dir(project_dir: Path) -> bool:
    """Return True only if ``project_dir`` resolves strictly inside ``PROJECTS_DIR``.

    The last-line guard against a ``project_name`` that escapes the projects root
    once joined - ``PROJECTS_DIR / ".."`` resolves to the parent cwcli-state
    directory (``~/.cwcli`` by default, or ``$CWCLI_HOME`` when set) that holds
    every project plus the cache and config, and an absolute component resets the
    join entirely. Any ``rmtree``/archive of an escaped path could wipe unrelated
    data, so callers must gate on this before touching the filesystem.
    """
    try:
        root = PROJECTS_DIR.resolve()
        target = project_dir.resolve()
    except OSError:
        return False
    return target != root and root in target.parents


def is_valid_project_name(name: str) -> bool:
    """Reject names that are not a single, safe directory entry under ``PROJECTS_DIR``.

    A project name is only ever one directory beneath the projects root. Empty,
    ``.``, ``..``, absolute, or separator-bearing names let a ``pathlib`` join
    escape that root (see :func:`is_safe_project_dir`), so ``cwcli rm ..`` could
    otherwise archive-and-``rmtree`` the entire cwcli-state tree.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if Path(name).is_absolute():
        return False
    return is_safe_project_dir(PROJECTS_DIR / name)


# ---------------------------------------------------------- site / archive helpers


def _list_sites(container, bench_path: str) -> list[str] | None:
    """Return the real Frappe sites under ``{bench_path}/sites`` (fail-safe).

    Thin wrapper over :func:`bench_sites.list_sites` so ``rm`` and ``inspect``
    share one site-detection implementation. Detection is fail-safe (anything
    ambiguous is treated as a real site) so ``rm`` never deletes a site's volume
    without first backing it up. Returns the list of site names (possibly empty),
    or ``None`` if the sites directory itself could not be listed.
    """
    return bench_sites.list_sites(container, bench_path)


def _bench_archive_slug(bench_path: str) -> str:
    """Return a short safe slug for a bench path, for archive-directory namespacing.

    Two benches in the same instance may share a site name (e.g. both use the
    frappe-docker default ``development.localhost``), so their backup artifacts
    must land in distinct archive subdirectories. Hashes the full bench path to a
    short deterministic slug that is safe across platforms.
    """
    safe_basename = re.sub(
        r"[^a-zA-Z0-9_-]", "_", bench_path.rstrip("/").rsplit("/", 1)[-1] or "bench"
    )
    short_hash = hashlib.sha256(bench_path.encode()).hexdigest()[:8]
    return f"{safe_basename}_{short_hash}"


class _ChunkStreamReader(io.RawIOBase):
    """Adapt docker-py's ``get_archive`` byte-chunk iterator to a readable stream.

    ``get_archive`` yields the artifact's tar wrapper in chunks; wrapping those
    chunks in this reader lets :mod:`tarfile` consume the tar incrementally
    (streaming mode) so a multi-GB backup artifact is never buffered whole in
    host RAM.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        super().__init__()
        self._chunks = iter(chunks)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _stream_container_file(
    container,
    source_path: str,
    dest_file: Path,
    chunk_size: int = 1024 * 1024,
) -> bool:
    """Copy a single file out of ``container`` to the host, streaming in chunks.

    Uses ``get_archive`` (a tar stream) and extracts the single member
    incrementally, so a multi-GB ``bench backup --with-files`` artifact is never
    materialised in host RAM as one blob. Returns True only on a fully-written
    copy. Any failure - a missing/unreadable file (``get_archive`` raises), a
    tar/read error, or a truncated stream (bytes written do not match the member
    size) - returns False, so the caller keeps the "a backup missing any part is
    not trusted" fail-closed semantics.
    """
    try:
        stream, _stat = container.get_archive(source_path)
        reader = io.BufferedReader(_ChunkStreamReader(stream))
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    return False
                written = 0
                with open(dest_file, "wb") as out:
                    while True:
                        chunk = extracted.read(chunk_size)
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
                # A truncated stream yields fewer bytes than the header promised:
                # fail closed rather than trust a short copy.
                return written == member.size
    except Exception:
        return False
    # No regular-file member in the archive - nothing was copied.
    return False


def _backup_sites(
    project_name: str,
    container,
    bench_path: str,
    archive_dir: Path,
    emit: OnEvent,
) -> bool:
    """Back up all sites in the bench before removal (verified copy-out).

    Returns True only if EVERY site was fully backed up with its database dump
    confirmed present and non-empty on the host archive directory; False if any
    site's backup failed or its dump did not land. A partial success is reported
    as False so the caller never destroys data for a site whose backup is missing.
    """
    try:
        sites = _list_sites(container, bench_path)

        if sites is None:
            emit(RmTrace(text=f"Could not list sites directory: {bench_path}/sites"))
            return False

        if not sites:
            emit(RmTrace(text="No sites found to backup"))
            return True

        backups_dir = archive_dir / "backups"
        backups_dir.mkdir(exist_ok=True)

        backed_up_count = 0
        failed_sites: list[str] = []
        for site in sites:
            emit(RmTrace(text=f"Backing up site '{site}'..."))

            backup_cmd = f"cd {bench_path} && bench --site {site} backup --with-files"
            exit_code, output = container.exec_run(f"sh -c '{backup_cmd}'", workdir=bench_path)

            if exit_code != 0:
                emit(RmWarning(text=f"Could not backup site '{site}'"))
                emit(RmTrace(text=f"Backup command output: {output.decode('utf-8')}"))
                failed_sites.append(site)
                continue

            emit(RmTrace(text=f"Backup created for '{site}'"))

            # A zero exit from `bench backup` only means the dump was written
            # INSIDE the container (i.e. inside the volume we are about to
            # delete). It is not a backup until the bytes are copied out to the
            # host archive, so verify each expected artifact actually lands here
            # and, critically, that the database dump is present and non-empty.
            site_backup_dir = f"{bench_path}/sites/{site}/private/backups"
            exit_code, ls_output = container.exec_run(f"ls -1t {site_backup_dir}")

            if exit_code != 0:
                emit(RmWarning(text=f"Could not list backups for site '{site}'"))
                failed_sites.append(site)
                continue

            backup_files = [f.strip() for f in ls_output.decode("utf-8").split("\n") if f.strip()]

            # Select only THIS run's artifacts. `ls -1t` is newest-first and
            # Frappe names every artifact of one run with a shared leading
            # timestamp token (`<YYYYMMDD_HHMMSS>-<site>-<part>`), so the newest
            # file's token identifies the current run. Verifying a fixed top-N
            # window instead would bleed into prior runs, letting a stale, broken
            # artifact falsely fail an otherwise complete fresh backup.
            run_ts = backup_files[0].split("-", 1)[0] if backup_files else ""
            current_files = [f for f in backup_files if f.split("-", 1)[0] == run_ts]

            site_archive_backups = backups_dir / site
            site_archive_backups.mkdir(exist_ok=True)

            db_dump_saved = False
            copy_failed = False
            for backup_file in current_files:
                source_path = f"{site_backup_dir}/{backup_file}"
                dest_file = site_archive_backups / backup_file

                # Stream the artifact out in fixed-size chunks so a multi-GB
                # `--with-files` backup is never held in host RAM as one blob.
                if not _stream_container_file(container, source_path, dest_file):
                    # An artifact we could not copy out (missing, read error, or
                    # a truncated stream). Fail closed: a backup missing any of
                    # its parts is not a trustworthy backup.
                    copy_failed = True
                    emit(RmTrace(text=f"Could not copy {backup_file} to archive"))
                    continue

                # Confirm the artifact actually landed on the host. The database
                # dump additionally must be non-empty - an empty dump is no backup
                # at all (other artifacts, e.g. a files tar, may legitimately be
                # small, so only the dump is size-checked).
                try:
                    size = dest_file.stat().st_size
                except OSError:
                    size = 0

                if _DB_DUMP_MARKER in backup_file.lower() and size > 0:
                    db_dump_saved = True

                emit(RmTrace(text=f"Copied {backup_file} to archive"))

            if db_dump_saved and not copy_failed:
                backed_up_count += 1
            else:
                emit(
                    RmWarning(
                        text=(
                            f"Backup for site '{site}' was not fully copied to the host archive "
                            "(missing or empty database dump)"
                        )
                    )
                )
                failed_sites.append(site)

        if backed_up_count > 0:
            emit(RmNotice(text=f"Backed up {backed_up_count} site(s) to {backups_dir}"))

        # Success requires EVERY site to have a confirmed dump. Any failure means
        # the caller must not delete data that was not safely captured.
        return not failed_sites

    except Exception as e:
        emit(RmTrace(text=f"Backup failed: {e}"))
        emit(RmWarning(text=f"Could not backup sites for '{project_name}'"))
        return False


def _archive_project_config(
    project_name: str,
    container,
    bench_path: str,
    emit: OnEvent,
    archive_dir_override: Path | None = None,
) -> bool:
    """Archive project configuration (docker-compose.yml + site_config.json files).

    ``archive_dir_override`` lets the multi-bench backup loop land the config
    archive in the same per-bench namespace as the DB backups. Returns True if
    the archive succeeded, False otherwise.
    """
    try:
        if archive_dir_override is not None:
            archive_dir = archive_dir_override
        else:
            archive_base = cwcli_home() / "archive"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_dir = archive_base / f"{project_name}_{timestamp}"
        archive_dir.mkdir(parents=True, exist_ok=True)

        emit(RmTrace(text=f"Archiving to {archive_dir}"))

        # Archive docker-compose.yml from the container's working directory.
        compose_locations = [
            "/workspace/frappe-bench/docker-compose.yml",
            f"{bench_path}/docker-compose.yml",
            "/docker-compose.yml",
        ]

        compose_archived = False
        for compose_path in compose_locations:
            exit_code, output = container.exec_run(f"cat {compose_path}")
            if exit_code == 0:
                compose_file = archive_dir / "docker-compose.yml"
                compose_file.write_bytes(output)
                emit(RmTrace(text=f"Archived {compose_path} to {compose_file}"))
                compose_archived = True
                break

        if not compose_archived:
            emit(RmTrace(text="Could not find docker-compose.yml to archive"))

        # Archive site_config.json files from all real sites.
        sites_dir = f"{bench_path}/sites"
        sites = _list_sites(container, bench_path)

        if sites:
            configs_archived = 0
            for site in sites:
                site_config_path = f"{sites_dir}/{site}/site_config.json"
                exit_code, output = container.exec_run(f"cat {site_config_path}")

                if exit_code == 0:
                    site_archive_dir = archive_dir / site
                    site_archive_dir.mkdir(exist_ok=True)

                    config_file = site_archive_dir / "site_config.json"
                    config_file.write_bytes(output)
                    configs_archived += 1

                    emit(RmTrace(text=f"Archived {site_config_path} to {config_file}"))

            if configs_archived > 0:
                emit(RmTrace(text=f"Archived {configs_archived} site config(s)"))

        metadata = {
            "project_name": project_name,
            "archived_at": datetime.now().isoformat(),
            "bench_path": bench_path,
        }

        metadata_file = archive_dir / "archive_metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2))

        emit(RmNotice(text=f"Configuration archived to {archive_dir}"))
        return True

    except Exception as e:
        emit(RmTrace(text=f"Archive failed: {e}"))
        return False


def _remove_named_volumes(
    project_name: str,
    emit: OnEvent,
    failures: list[str] | None = None,
) -> int:
    """Remove the named Docker Compose volumes for a project.

    ``Container.remove(v=True)`` only removes a container's *anonymous* volumes.
    The named volumes that frappe-docker creates (e.g. ``sites``, ``db-data``)
    are labeled with the compose project and must be removed explicitly, or the
    databases and sites survive removal despite what the user was told. Returns
    the number of named volumes removed; ``failures`` collects step failures.
    """
    volumes = get_project_volumes(project_name)

    if volumes is None:
        emit(
            RmWarning(
                text=(
                    f"Could not enumerate volumes for '{project_name}'; "
                    "some named volumes may remain."
                )
            )
        )
        if failures is not None:
            failures.append(f"could not enumerate named volumes for '{project_name}'")
        return 0

    if not volumes:
        emit(RmTrace(text=f"No named volumes found for '{project_name}'"))
        return 0

    removed = 0
    for volume in volumes:
        try:
            emit(RmStep(label=f"Removing volume '{volume.name}'...", style="red"))
            emit(RmTrace(text=f"Removing volume '{volume.name}'"))
            volume.remove(force=True)
            removed += 1
        except Exception as e:
            emit(RmWarning(text=f"Could not remove volume '{volume.name}': {e}"))
            if failures is not None:
                failures.append(f"could not remove volume '{volume.name}'")
            emit(RmTrace(text=f"Exception: {e}"))

    if removed > 0:
        emit(RmNotice(text=f"Removed {removed} named volume(s) for '{project_name}'"))

    return removed


def _remove_project_network(
    project_name: str,
    emit: OnEvent,
    failures: list[str] | None = None,
) -> bool:
    """Remove the project's own Docker Compose network, by exact compose-project label.

    Compose creates and labels one network per project (typically
    ``<project>_default``); neither ``Container.remove(v=True)`` nor
    :func:`_remove_named_volumes` above ever touches it, so - before this - it
    outlived every other removal step and accumulated forever, each leaked
    network consuming a slice of Docker's finite address pool until an
    unrelated command failed with "could not find an available, non-overlapping
    IPv4 address pool", naming nothing about this being the cause. Scoped to
    THIS project's network only, via the same ``com.docker.compose.project``
    label the container/volume lookups use (an exact-value match, never a name
    prefix or a broad ``network prune`` - the fleet forbids prune outright).

    A network that still has an endpoint attached from OUTSIDE this project (a
    container manually joined to it, or one of this project's own containers
    that failed to be removed above) cannot be deleted without disconnecting
    that endpoint first, and this function never does that: disconnecting
    something it does not know it owns is a bigger blast radius than the leak
    it exists to close. Docker's own API refuses the delete in that case
    (``network has active endpoints``); the refusal is reported as a failure
    with a self-explaining hint, never silently skipped and never forced
    through.

    Returns True only if a network was found AND removed; False if none
    existed (nothing to do) or a removal failed - the caller distinguishes the
    two via ``failures``, mirroring how ``dir_removed`` already reports "was
    something actually deleted", not "is the leak now clean".
    """
    networks = get_project_networks(project_name)

    if networks is None:
        emit(
            RmWarning(
                text=(f"Could not enumerate the network for '{project_name}'; it may remain.")
            )
        )
        if failures is not None:
            failures.append(f"could not enumerate the network for '{project_name}'")
        return False

    if not networks:
        emit(RmTrace(text=f"No network found for '{project_name}'"))
        return False

    removed = False
    for network in networks:
        try:
            emit(RmStep(label=f"Removing network '{network.name}'...", style="red"))
            emit(RmTrace(text=f"Removing network '{network.name}'"))
            network.remove()
            emit(RmNotice(text=f"Removed network '{network.name}' for '{project_name}'"))
            removed = True
        except Exception as e:
            emit(
                RmWarning(
                    text=(f"Could not remove network '{network.name}' for '{project_name}': {e}"),
                    hint=(
                        "A container outside this project may still be attached to it. "
                        "Disconnect it, then remove the network manually with "
                        f"'docker network rm {network.name}'."
                    ),
                )
            )
            if failures is not None:
                failures.append(f"could not remove network '{network.name}'")
            emit(RmTrace(text=f"Exception: {e}"))

    return removed


def _archive_project_directory(
    project_name: str,
    emit: OnEvent,
    archive_dir: Path | None = None,
) -> bool:
    """Archive the project's cwcli-managed ``conf/`` before the local dir is deleted.

    Only the small, reliable ``conf/`` subdirectory of ``PROJECTS_DIR/{name}/`` is
    archived - it holds the generated ``docker-compose.yml``. The rest of the
    directory is the frappe-docker devcontainer bind mount (a multi-hundred-MB
    ``frappe-bench`` whose venv/node_modules hold dangling symlinks); copying the
    whole tree wastes space and makes ``copytree`` raise on the dangling symlinks,
    which previously aborted removal (reintroducing issue #19). The database/files
    safety net is the ``bench backup`` output; ``conf/`` is the config safety net.

    Returns True if the copy succeeded or there was nothing to archive, False only
    if the copy itself failed (so the caller can refuse to delete unarchived data).
    """
    project_dir = PROJECTS_DIR / project_name

    # Last-line guard: never read/copy from a path that escapes the projects root.
    if not is_safe_project_dir(project_dir):
        emit(
            RmError(
                text=(
                    f"Refusing to archive '{project_dir}': path is outside the projects "
                    f"directory ({PROJECTS_DIR})."
                )
            )
        )
        return False

    if not project_dir.exists() or archive_dir is None:
        if not project_dir.exists():
            emit(RmTrace(text=f"No project directory to archive at {project_dir}"))
        return True

    conf_dir = project_dir / "conf"
    if not conf_dir.is_dir():
        # No cwcli-managed config to preserve (older or partial layout).
        emit(RmTrace(text=f"No conf/ directory to archive at {conf_dir}"))
        return True

    try:
        dest = archive_dir / "project_files" / "conf"
        # symlinks=True + ignore_dangling_symlinks keeps the copy robust even if a
        # config dir ever contains a (possibly dangling) symlink.
        shutil.copytree(
            conf_dir,
            dest,
            dirs_exist_ok=True,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
        emit(RmTrace(text=f"Archived project config to {dest}"))
        return True
    except Exception as e:
        emit(RmWarning(text=f"Could not archive project configuration for '{project_name}': {e}"))
        emit(RmTrace(text=f"Exception: {e}"))
        return False


def _delete_project_directory(
    project_name: str,
    emit: OnEvent,
    failures: list[str] | None = None,
) -> bool:
    """Delete the project's local directory at ``PROJECTS_DIR/{project_name}/``.

    Must only be called after :func:`_archive_project_directory` has succeeded.
    Without this step ``cwcli rm`` leaves the project lingering on disk (issue
    #19). Returns True if a directory was removed, False if there was nothing to
    remove or removal failed; ``failures`` collects a real ``rmtree`` failure.
    """
    project_dir = PROJECTS_DIR / project_name

    # Last-line guard before ``rmtree``: never delete a path outside the projects
    # root. ``PROJECTS_DIR / ".."`` would otherwise wipe the entire cwcli-state
    # tree (every project, the cache, config).
    if not is_safe_project_dir(project_dir):
        emit(
            RmError(
                text=(
                    f"Refusing to delete '{project_dir}': path is outside the projects "
                    f"directory ({PROJECTS_DIR})."
                )
            )
        )
        if failures is not None:
            failures.append(
                f"refused to delete path outside projects directory for '{project_name}'"
            )
        return False

    if not project_dir.exists():
        emit(RmTrace(text=f"No project directory to remove at {project_dir}"))
        return False

    try:
        shutil.rmtree(project_dir)
        emit(RmNotice(text=f"Removed project directory {project_dir}"))
        return True
    except Exception as e:
        emit(RmWarning(text=f"Could not remove project directory '{project_dir}': {e}"))
        if failures is not None:
            failures.append(f"could not remove project directory for '{project_name}'")
        emit(RmTrace(text=f"Exception: {e}"))
        return False


# --------------------------------------------------------------------- remove()


def _resolve_bench_paths(project_name: str, frappe_container, emit: OnEvent) -> list[str]:
    """The bench paths to back up: cache, else live discovery, else the default.

    A cache-read failure escalates to ``core.inspect.discover_benches`` (the same
    ``find``-based discovery the full T3 inspect uses) so a genuinely multi-bench
    instance still gets every bench backed up even with a corrupt/missing cache;
    only when live discovery also fails does it fall back to the single default
    path. Exactly one consolidated warning is emitted either way.
    """
    bench_paths = [DEFAULT_BENCH_PATH]  # default fallback
    try:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data and cached_data.get("bench_instances"):
            return [b["path"] for b in cached_data["bench_instances"]]
        return bench_paths
    except Exception as e:
        emit(RmTrace(text=f"Could not read cache for '{project_name}': {e}"))
        try:
            from . import inspect as core_inspect

            discovered = core_inspect.discover_benches(frappe_container)
            if discovered:
                bench_paths = discovered
        except Exception as discover_err:
            emit(RmTrace(text=f"Live bench discovery also failed: {discover_err}"))
        if bench_paths == [DEFAULT_BENCH_PATH]:
            fallback_detail = f"falling back to default bench path '{bench_paths[0]}'"
        else:
            fallback_detail = f"discovered {len(bench_paths)} bench(es) live"
        emit(
            RmWarning(
                text=(
                    f"Could not read bench paths from cache for '{project_name}'; "
                    f"{fallback_detail}. Multi-bench instances may not be fully backed up."
                )
            )
        )
        return bench_paths


def remove(
    project_name: str,
    *,
    remove_volumes: bool = True,
    no_backup: bool = False,
    on_event: OnEvent | None = None,
) -> Result[RemovalOutcome]:
    """Remove a single project's containers, named volumes, network, and directory.

    See the module docstring. Raises :class:`~.errors.CwcliError` (``USAGE`` for
    an invalid name, ``DOCKER`` when the daemon is unreachable); otherwise returns
    ``Result(status, RemovalOutcome(...))`` - ``OK`` for a clean removal,
    ``WARNING`` for a partial failure, a gate-blocked removal, or a genuinely
    not-found project. Prints nothing; narration rides ``on_event``.
    """
    emit = on_event or _noop

    found = False
    orphan = False
    containers_removed = 0
    volumes_removed = 0
    dir_removed = False
    network_removed = False
    backup_ok = True
    failures: list[str] = []

    def _result() -> Result[RemovalOutcome]:
        status = Status.OK if (found and not failures) else Status.WARNING
        return Result(
            status=status,
            data=RemovalOutcome(
                project=project_name,
                found=found,
                orphan=orphan,
                containers_removed=containers_removed,
                volumes_removed=volumes_removed,
                dir_removed=dir_removed,
                network_removed=network_removed,
                backup_ok=backup_ok,
                failures=failures,
            ),
        )

    # Defense-in-depth: never operate on a name that escapes the projects root.
    # The CLI already filters these, but a core function must not trust its
    # caller's discipline for its own contract.
    if not is_valid_project_name(project_name):
        raise CwcliError(
            ErrorKind.USAGE,
            "project.invalid_name",
            f"Refusing to remove invalid project name {project_name!r}.",
        )

    containers = get_project_containers(project_name)

    # None means a Docker connection error (distinct from an empty list). Do not
    # attempt destructive cleanup when we cannot even see the project.
    if containers is None:
        raise CwcliError(
            ErrorKind.DOCKER,
            "docker.unreachable",
            f"Could not connect to Docker to inspect '{project_name}'.",
        )

    project_dir = PROJECTS_DIR / project_name
    dir_existed = project_dir.exists()

    is_orphan = not containers
    if is_orphan:
        # No containers left. The project may still have orphaned named volumes,
        # an orphaned network, or a lingering local directory. Clean those up
        # rather than refusing. If there is genuinely nothing left, treat it as a
        # typo / not found.
        orphan_volumes = get_project_volumes(project_name)
        orphan_networks = get_project_networks(project_name)
        if not dir_existed and not orphan_volumes and not orphan_networks:
            return _result()  # found stays False -> the frontend renders "not found"
        orphan = True

    found = True

    emit(RmTrace(text=f"Found {len(containers)} container(s) for '{project_name}'"))

    # Create a single archive directory for this removal. Database backups, config
    # archives, and a copy of the local project directory all land here so nothing
    # is deleted without a safety copy first.
    archive_base = cwcli_home() / "archive"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = archive_base / f"{project_name}_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    frappe_container = next(
        (c for c in containers if c.labels.get("com.docker.compose.service") == "frappe"),
        None,
    )

    # A live `bench backup` and the container-side config archive both shell into
    # the frappe container via exec_run, which only works while it is running. A
    # stopped frappe container is a normal rm case: do NOT attempt those
    # exec-based steps against it. The conf/ safety net is copied host-side below.
    frappe_running = frappe_container is not None and frappe_container.status == "running"

    if frappe_running:
        bench_paths = _resolve_bench_paths(project_name, frappe_container, emit)

        # Pre-compute per-bench archive directories. For a single-bench instance
        # the archive dir IS ``archive_dir`` (flat layout, easier to discover);
        # multi-bench gets a hash-suffixed namespace so same-named sites do not
        # collide.
        multi_bench = len(bench_paths) > 1
        bench_archive_dirs: dict[str, Path] = {}
        for bp in bench_paths:
            bd = archive_dir
            if multi_bench:
                bd = bd / _bench_archive_slug(bp)
            bench_archive_dirs[bp] = bd

        # Backup ALL benches (not just the first). Each bench's sites are backed
        # up and verified independently; the result is True only if EVERY bench
        # fully backed up. Any single bench failure blocks volume deletion.
        if not no_backup:
            all_backups_ok = True
            for bp in bench_paths:
                emit(RmStep(label=f"Backing up databases for bench '{bp}'...", style="cyan"))
                bench_archive_dir = bench_archive_dirs[bp]
                bench_archive_dir.mkdir(parents=True, exist_ok=True)
                bench_ok = _backup_sites(
                    project_name, frappe_container, bp, bench_archive_dir, emit
                )
                if not bench_ok:
                    all_backups_ok = False
            backup_ok = all_backups_ok

        # Archive configuration for each bench. A failed config archive is a
        # warning only -- it does not block cache clearing (unlike backup, volume,
        # or container failures which mean something was NOT deleted). The backup
        # gate above already protects the data.
        for bp in bench_paths:
            emit(RmStep(label=f"Archiving configuration for bench '{bp}'...", style="cyan"))
            bench_archive_dir = bench_archive_dirs[bp]
            bench_archive_dir.mkdir(parents=True, exist_ok=True)
            config_ok = _archive_project_config(
                project_name, frappe_container, bp, emit, archive_dir_override=bench_archive_dir
            )
            if not config_ok:
                emit(
                    RmWarning(
                        text=(
                            f"Could not archive configuration for bench '{bp}' -- the bench "
                            "config may not be recoverable from backup artifacts alone."
                        )
                    )
                )
    elif remove_volumes and not no_backup:
        # No running frappe container, so a live `bench backup` is impossible -
        # and this is the data-destroying path. Mark the backup not-OK so the
        # EARLY gate below aborts before anything is removed. The frontend
        # transiently starts a stopped project BEFORE calling this, so reaching
        # here on the backup path means the project is an orphan (nothing to
        # start) or the transient start did not take - either way, fail closed.
        backup_ok = False
        if is_orphan:
            emit(
                RmWarning(
                    text=(
                        f"'{project_name}' has no containers to start, so a fresh database "
                        "backup could not be taken."
                    )
                )
            )
        else:
            emit(
                RmWarning(
                    text=(
                        f"'{project_name}' is not running, so a fresh database backup could "
                        "not be taken."
                    )
                )
            )
    else:
        # --no-backup or --no-volumes: nothing data-bearing is destroyed without a
        # backup, so it is safe to proceed. Only the host-side conf/ archive can
        # be preserved (a live DB dump is impossible while stopped).
        emit(
            RmWarning(
                text=(
                    f"No container was running for '{project_name}', so a fresh database "
                    "backup could not be taken before cleanup."
                )
            )
        )

    # A verified live backup is the gate on destroying the named volumes. Evaluate
    # it BEFORE removing any container so a failed backup aborts while the frappe
    # container is still alive and a retry can still produce a backup. Removing
    # containers first and only THEN blocking would leave an orphan: on the naive
    # retry there is no running container, so no backup is attempted, backup_ok
    # defaults True, the gate passes, and the database volumes are deleted with NO
    # backup - defeating C1. The gate is scoped to volume deletion: under
    # --no-volumes no volume data is destroyed, so a failed backup does not abort;
    # --no-backup opts out of the gate entirely.
    backup_failed = remove_volumes and not no_backup and not backup_ok
    if backup_failed:
        emit(
            RmWarning(
                text=(
                    f"Refusing to remove '{project_name}': a verified database backup could "
                    "not be created."
                ),
                hint=(
                    "Resolve the backup failure and retry, or use --no-backup to remove "
                    "without a backup (the databases will be lost)."
                ),
            )
        )
        failures.append(f"a verified database backup could not be created for '{project_name}'")
        return _result()

    # Stop and remove each container.
    container_removal_failed = False
    for container in containers:
        # Bind a fallback name BEFORE reading ``container.name`` so the except
        # handler can never hit an unbound ``container_name`` (a NameError):
        # ``container.name`` is a docker-py property that can itself raise, and
        # that raise must surface as a recorded container-removal failure.
        container_name = "<unknown>"
        try:
            container_name = container.name
            container_status = container.status

            emit(
                RmTrace(
                    text=f"Processing container '{container_name}' (status: {container_status})"
                )
            )

            if container_status == "running":
                emit(RmStep(label=f"Stopping '{container_name}'...", style="yellow"))
                emit(RmTrace(text=f"Stopping container '{container_name}'"))
                container.stop()

            emit(RmStep(label=f"Removing '{container_name}'...", style="red"))
            emit(RmTrace(text=f"Removing container '{container_name}'"))
            container.remove(v=remove_volumes, force=True)
            containers_removed += 1

        except Exception as e:
            container_removal_failed = True
            failures.append(f"failed to remove container '{container_name}'")
            emit(RmError(text=f"Failed to remove container '{container_name}': {e}"))
            emit(RmTrace(text=f"Exception: {e}"))

    # Archive the local project directory BEFORE deleting anything. If the copy
    # fails we refuse to delete the named volumes or the directory.
    emit(RmStep(label=f"Archiving project directory for '{project_name}'...", style="cyan"))
    archived_ok = _archive_project_directory(project_name, emit, archive_dir=archive_dir)

    # Destroying data (the named volumes and the local directory) is gated on two
    # remaining safety conditions: every container was removed cleanly, and the
    # conf/ config archive succeeded. (The verified live backup is enforced
    # EARLIER, as an abort before any container is removed.)
    archive_failed = dir_existed and not archived_ok
    gate_blocked = container_removal_failed or archive_failed

    if gate_blocked:
        reasons = []
        if container_removal_failed:
            reasons.append("one or more containers could not be removed")
        if archive_failed:
            reasons.append("its configuration could not be archived")
        message = (
            f"Skipping volume, network, and directory removal for '{project_name}' because "
            + " and ".join(reasons)
            + "."
        )
        emit(RmWarning(text=message))
        failures.append(message)
    else:
        # Remove named compose volumes. Anonymous volumes were already handled by
        # container.remove(v=remove_volumes), but the named volumes that hold the
        # databases and sites must be removed explicitly or the data survives.
        if remove_volumes:
            emit(RmStep(label=f"Removing volumes for '{project_name}'...", style="red"))
            volumes_removed = _remove_named_volumes(project_name, emit, failures=failures)

        # Remove the project's own compose network. Attempted regardless of
        # --no-volumes: unlike the named volumes, the network holds none of the
        # user's data, so keeping it is never a safety choice - see
        # _remove_project_network's docstring for why a failure here refuses
        # rather than forces.
        emit(RmStep(label=f"Removing network for '{project_name}'...", style="red"))
        network_removed = _remove_project_network(project_name, emit, failures=failures)

        # Remove the local project directory (deleted regardless of --no-volumes:
        # it is config, not data, and leaving it behind is the issue #19 bug).
        emit(RmStep(label=f"Removing project directory for '{project_name}'...", style="red"))
        dir_removed = _delete_project_directory(project_name, emit, failures=failures)

    # Clear the cache only when NO step failed. This covers the gate-blocked case
    # AND a post-gate failure - any of those may leave a data-bearing volume or
    # directory, so keep the cache entry (the half-removed project stays visible
    # in `ls`/`inspect` and can be retried). On a clean full/orphan/--no-volumes
    # removal, `failures` is empty and the cache is cleared.
    if not failures and (
        containers_removed > 0 or volumes_removed > 0 or dir_removed or network_removed
    ):
        emit(RmTrace(text=f"Clearing cache for '{project_name}'"))
        db_utils.clear_cache_for_project(project_name)

    return _result()
