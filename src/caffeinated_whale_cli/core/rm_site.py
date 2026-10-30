"""``core.drop_site`` - permanently drop ONE site, scoped to that site alone.

The inverse of ``core.init.init_bench``'s site provisioning: the warm-bench
delivery model creates a new site per task on a shared, already-running bench
and must drop it at teardown, and until this module existed that had no verb -
only ``cwcli rm``, which destroys the WHOLE instance (containers, volumes, the
project directory), never touching just one site on a bench that keeps running.
Hiding that blast-radius difference behind a ``--site`` flag on ``rm`` would be a
footgun (forgetting the flag nukes the instance instead of one site, worst of
all on the agent surface), so this is a dedicated noun-scoped verb, following the
same ``docker rm`` vs ``docker volume rm`` model ``rm``/``apps uninstall`` already
established: destruction scope is explicit in the verb, not a modifier on a
bigger one.

Built entirely from the shared bench-op primitives ``unlock``/``bench_ops``
already use (``resolvers.resolve_container_and_bench``, the site/path validation
and probe helpers, ``exec_stream`` for the narrated ``bench drop-site`` call, the
``consent``-as-core-parameter pattern ``apps.uninstall_apps``/``restore_apply``
established), plus ``core.rm``'s own backup gate. Its own logic is the order
(verified backup first, drop second) and what happens to the archive
``bench drop-site`` creates.

**The backup gate (the hard requirement this module exists to satisfy).**
cwcli takes the site's backup ITSELF, before anything is destroyed, with the
SAME gate ``cwcli rm`` uses (:func:`core.rm.backup_site`): ``bench --site <site>
backup --with-files``, this run's database dump and file archives streamed out
to the managed host archive (``cwcli_home()/archive/<project>_dropped_sites/
<site>_<timestamp>/backups/``), and the dump verified non-empty there. Only then
does it run ``bench drop-site --no-backup``. It must never lean on bench's own
drop-time backup: ``bench drop-site --force`` wraps that backup in a ``try`` and,
under ``--force``, ignores a failed backup and drops the database anyway, which
once dropped a real site's database while cwcli reported ``ok: true``. A backup or
verification failure raises ``rm_site.backup_failed`` with bench's own output,
and nothing is dropped.

**The archive decision.** ``bench drop-site`` MOVES the site's whole directory -
``site_config.json`` (the database credentials) included - under
``{bench_path}/archived/sites/`` inside the container, normally named after the
site and given a numeric suffix when that name already exists. Left there, that
folder grows by one credential-bearing entry every drop, forever, unpruned,
inside a container that keeps running for the rest of the bench's life. That is
the same unbounded-residue shape the network-leak fix in ``core.rm`` closed for
the whole-instance path, so it gets the same answer here: immediately after a
successful drop, this module copies that one archived path out beside the
verified backup (``<site>.tar`` in the same host directory) and - ONLY once that
copy is verified non-empty - deletes the in-container copy. Anything that cannot be verified is left in place
rather than deleted unbacked (fail closed, the ``core.rm`` C1 gate's own logic),
and every one of these outcomes rides ``DropSiteOutcome.ok`` / warnings rather
than a silent success.
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..utils import shared_home
from ..utils.config_utils import cwcli_home
from . import db_password, failure_causes, resolvers
from . import rm as core_rm
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_stream

# --------------------------------------------------------------------------- DTO


@dataclass(frozen=True, slots=True, kw_only=True)
class DropSiteOutcome:
    """The typed outcome of a site drop (serializable, no live objects).

    ``archived_host_path`` is the host directory holding the site's verified
    backup (``backups/``: the database dump, the file archives, and the site
    config) and bench's archived site directory (``<site>.tar``); it is None
    when that archive could not be confirmed there. ``ok`` is True only when the
    site was dropped and the in-container archive was both copied out and pruned - the fully
    clean case. A drop that succeeded but left any of that unconfirmed still
    reports the site as gone (it is), but ``ok=False`` and a warning names exactly
    what remains, so a caller reading only the exit code still learns it.
    """

    project: str
    site: str
    bench_path: str
    archived_host_path: str | None
    archive_pruned_in_container: bool
    ok: bool


# ----------------------------------------------------------------- typed events


@dataclass(frozen=True, slots=True, kw_only=True)
class DropSiteCommand:
    """The ``bench drop-site`` command about to run, to stderr (secret as a $-ref)."""

    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DropSiteOutput:
    """A chunk of ``bench drop-site``'s own output, tagged with its stream.

    Forwarded verbatim and unparsed - the same reasoning ``bench_ops``'s
    ``BenchOpOutput`` documents: a drop is not a supervised process, so its own
    bytes are the only place a mid-drop failure's reason exists.
    """

    stream: str  # "stdout" | "stderr"
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DropSiteNotice:
    """A dim progress line (e.g. "archived dropped site to ...")."""

    text: str


DropSiteEvent = DropSiteCommand | DropSiteOutput | DropSiteNotice
OnEvent = Callable[[DropSiteEvent], None]


def _noop(_event: DropSiteEvent) -> None:
    """The drain-and-discard consumption mode (no renderer)."""


# ---------------------------------------------------------------- archive helpers


def _validate_plain_component(value: str, *, kind: str) -> None:
    if (
        not value
        or not value.strip()
        or value in {".", ".."}
        or PurePosixPath(value).name != value
        or "\0" in value
    ):
        raise CwcliError(
            ErrorKind.USAGE,
            f"{kind}.invalid_component",
            f"Invalid {kind} name '{value}'. It must be one plain directory name.",
        )


def _archived_sites_root(bench_path: str) -> str:
    return f"{bench_path}/archived/sites"


def _archived_site_names(container, root: str) -> set[str] | None:
    try:
        exists, _ = container.exec_run(["test", "-d", root])
        if exists != 0:
            return set()
        exit_code, output = container.exec_run(
            [
                "find",
                root,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-type",
                "d",
                "-printf",
                "%f\n",
            ]
        )
    except Exception:
        return None
    if exit_code != 0:
        return None
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return {name for name in str(output).splitlines() if name}


def _new_archived_site_path(root: str, site: str, before: set[str], after: set[str]) -> str | None:
    candidates = [
        name
        for name in after - before
        if name == site or (name.startswith(site) and name[len(site) :].isdigit())
    ]
    if len(candidates) != 1:
        return None
    return f"{root}/{candidates[0]}"


def _archived_site_exists(container, path: str) -> bool | None:
    try:
        exit_code, _ = container.exec_run(["test", "-d", path])
    except Exception:
        return None
    return bool(exit_code == 0)


def _prune_archived_site(container, source_path: str) -> bool:
    """Delete ``source_path`` inside the container. True only if the exit code was 0."""
    try:
        exit_code, _ = container.exec_run(["rm", "-rf", source_path])
    except Exception:
        return False
    return bool(exit_code == 0)


def _copy_out_archived_site(container, source_path: str, dest_file: Path) -> bool:
    """Copy ``source_path`` (a directory) out of the container as a raw host tar.

    Uses ``get_archive`` exactly as ``core.rm`` does, but writes the tar stream
    to the host UNPARSED - the archive is an opaque credential-bearing backup
    blob (like ``core.rm``'s ``conf/`` copy), not something cwcli needs to
    inspect, so there is no reason to pay for extraction. Returns True only if
    the resulting host file is non-empty.
    """
    created = False
    try:
        stream, _stat = container.get_archive(source_path)
        fd = os.open(dest_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as out:
            for chunk in stream:
                out.write(chunk)
        # 0600 per-user; 0660 + cwcli group in shared mode so group members can
        # read backups at rest (the shared install treats the cwcli group as able
        # to read all instance data - see the README security note).
        dest_file.chmod(shared_home.file_mode(0o600))
        shared_home.apply_group(dest_file)
    except Exception:
        if created:
            try:
                dest_file.unlink()
            except OSError:
                pass
        return False
    try:
        return dest_file.stat().st_size > 0
    except OSError:
        return False


# ------------------------------------------------------------------- backup gate


def _has_verified_dump(backups_dir: Path) -> bool:
    """True if ``backups_dir`` holds a non-empty database dump (the host-side check)."""
    try:
        return any(
            f.is_file() and core_rm.DB_DUMP_MARKER in f.name.lower() and f.stat().st_size > 0
            for f in backups_dir.iterdir()
        )
    except OSError:
        return False


def backup_before_drop(
    container,
    project_name: str,
    bench_path: str,
    site: str,
    *,
    on_event: OnEvent | None = None,
) -> Path:
    """Take ``site``'s verified backup on the host BEFORE anything is dropped.

    Runs ``core.rm``'s own C1 gate (:func:`core.rm.backup_site`) into a fresh
    ``cwcli_home()/archive/<project>_dropped_sites/<site>_<timestamp>/backups/``
    and returns that ``<site>_<timestamp>`` directory. On ANY failure the
    half-written directory is removed and ``CwcliError(PRECONDITION,
    "rm_site.backup_failed")`` carries bench's own output: nothing was dropped.
    """
    emit: OnEvent = on_event or _noop
    _validate_plain_component(project_name, kind="project")
    resolvers.validate_site_name(site)
    _validate_plain_component(site, kind="site")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    sites_dir = cwcli_home() / "archive" / f"{project_name}_dropped_sites"
    drop_dir = sites_dir / f"{site}_{timestamp}"
    try:
        sites_dir.mkdir(parents=True, exist_ok=True, mode=shared_home.dir_mode(0o700))
        # Never exist_ok: the failure cleanup below may only ever remove this run's own dir.
        drop_dir.mkdir(mode=shared_home.dir_mode(0o700))
        for d in (sites_dir, drop_dir):
            d.chmod(shared_home.dir_mode(0o700))
            shared_home.apply_group(d)
    except OSError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "rm_site.backup_failed",
            f"Could not create the host archive directory {drop_dir}; nothing was dropped.",
            detail={"output": str(e)},
        ) from e

    events: list[core_rm.RmEvent] = []
    try:
        ok = core_rm.backup_site(container, bench_path, site, drop_dir / "backups", events.append)
    except Exception as e:  # noqa: BLE001 - a Docker error mid-backup is a failed backup
        ok = False
        events.append(core_rm.RmTrace(text=str(e)))
    if not ok:
        shutil.rmtree(drop_dir, ignore_errors=True)
        reason = next(
            (e.text for e in events if isinstance(e, core_rm.RmWarning)),
            f"Could not backup site '{site}'",
        )
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "rm_site.backup_failed",
            f"{reason}; nothing was dropped.",
            hint=(
                "Fix the backup failure and re-run, or drop the site deliberately without a "
                f"backup with 'cwcli run {project_name} --path {bench_path} -i drop-site "
                f"{site} --no-backup'."
            ),
            detail={"output": "\n".join(e.text for e in events if isinstance(e, core_rm.RmTrace))},
        )

    emit(DropSiteNotice(text=f"Backed up site '{site}' to {drop_dir / 'backups'}"))
    return drop_dir


def drop_backed_up_site(
    container,
    project_name: str,
    bench_path: str,
    site: str,
    drop_dir: Path,
    *,
    db_root_password: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[DropSiteOutcome]:
    """Drop a site whose verified backup :func:`backup_before_drop` put in ``drop_dir``.

    Refuses (``rm_site.backup_missing``) unless ``drop_dir/backups`` holds a
    non-empty database dump, so this can never be a drop without a backup. Then
    ``bench drop-site --no-backup`` (never ``--force``: it exists to ignore a failed
    backup), then the archive relocation.
    """
    emit: OnEvent = on_event or _noop
    warnings: list[Message] = []
    backups_dir = drop_dir / "backups"

    if not _has_verified_dump(backups_dir):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "rm_site.backup_missing",
            f"No verified database dump for site '{site}' in {backups_dir}; the site was not "
            "dropped.",
        )

    archive_root = _archived_sites_root(bench_path)
    archives_before = _archived_site_names(container, archive_root)
    if archives_before is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "rm_site.archive_state_unknown",
            f"Could not inspect existing site archives at {archive_root}; the site was not dropped.",
        )

    # Secret rides the environment, never the argv (the restore.py M5 pattern):
    # the traced command below is the $-ref, never the value.
    env = {"CWCLI_DB_ROOT_PASSWORD": db_password.resolve(project_name, db_root_password)}
    cmd_str = (
        f"bench drop-site {shlex.quote(site)} "
        '--db-root-password "$CWCLI_DB_ROOT_PASSWORD" --no-backup'
    )
    emit(DropSiteCommand(command=cmd_str))
    exit_code = 1
    tail = ""
    for event in exec_stream(container, ["sh", "-c", cmd_str], workdir=bench_path, environment=env):
        if isinstance(event, ExecChunk):
            emit(DropSiteOutput(stream=event.stream, text=event.text))
            tail = (tail + event.text)[-8192:]
        else:
            exit_code = event.exit_code

    if exit_code != 0:
        cause = failure_causes.explain(tail)
        failed = f"Failed to drop site '{site}' (bench drop-site exited {exit_code})."
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "rm_site.failed",
            f"{cause.text} {failed}" if cause else failed,
            detail={"output": tail} if tail else None,
        )

    archives_after = _archived_site_names(container, archive_root)
    source = (
        _new_archived_site_path(archive_root, site, archives_before, archives_after)
        if archives_after is not None
        else None
    )
    archived_host_path: str | None = None
    archive_pruned = False

    source_exists = _archived_site_exists(container, source) if source is not None else False

    if source is not None and source_exists:
        site_tar = drop_dir / f"{site}.tar"
        if _copy_out_archived_site(container, source, site_tar):
            archived_host_path = str(drop_dir)
            emit(DropSiteNotice(text=f"Archived dropped site to {drop_dir}"))
            # The site is gone and its backup proven twice; keep that backup where every
            # other set lives (captain decision 1, 2026-10-27). Fail-soft, never a refusal.
            from . import backup_store as _store

            adopted = _store.adopt_dir(
                project_name,
                site,
                drop_dir / "backups",
                bench_path=bench_path,
                reason="before-removal",
            )
            if adopted is not None:
                emit(DropSiteNotice(text=f"Backup kept in the store as {adopted.key}"))
            archive_pruned = _prune_archived_site(container, source)
            if not archive_pruned:
                warnings.append(
                    Message(
                        "rm_site.archive_not_pruned",
                        f"Copied the dropped site's archive to {site_tar}, but could not "
                        f"remove the in-container copy at {source}; it still holds "
                        "site_config.json (database credentials).",
                    )
                )
        else:
            warnings.append(
                Message(
                    "rm_site.archive_not_copied",
                    "bench archived the dropped site (including site_config.json and its "
                    f"database credentials) at {source} inside the container, but cwcli could "
                    "not copy it out; it was left in place rather than deleted unbacked. The "
                    f"verified backup is in {backups_dir}. Copy the archive out manually, then "
                    "remove it.",
                )
            )
    elif source is not None and source_exists is None:
        warnings.append(
            Message(
                "rm_site.archive_probe_failed",
                f"bench created the dropped site's archive at {source}, but cwcli could not "
                "verify it after the drop; nothing was copied out or pruned, and the "
                "credential-bearing archive may remain there.",
            )
        )
    else:
        warnings.append(
            Message(
                "rm_site.archive_not_found",
                f"cwcli could not uniquely identify the archive bench created under "
                f"{archive_root}; the credential-bearing archive may remain there, and "
                "nothing was copied out or pruned.",
            )
        )

    ok = archived_host_path is not None and archive_pruned

    return Result(
        status=Status.OK if ok else Status.WARNING,
        data=DropSiteOutcome(
            project=project_name,
            site=site,
            bench_path=bench_path,
            archived_host_path=archived_host_path,
            archive_pruned_in_container=archive_pruned,
            ok=ok,
        ),
        warnings=warnings,
    )


# --------------------------------------------------------------------- drop_site


def drop_site(
    project_name: str,
    site: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    consent: bool = False,
    db_root_password: str | None = None,
    on_event: OnEvent | None = None,
) -> Result[DropSiteOutcome]:
    """Permanently drop ONE site: verified backup first, then ``bench drop-site --no-backup``.

    ``site`` is a required, explicit target - unlike ``backup``/``unlock``, there
    is no default-site fallback, matching ``bench_ops.run_tests``' captain ruling
    S1: an unbounded destructive effect must never default its target.

    Returns ``NEEDS_CHOICE``/``confirm_drop_site`` when ``consent`` is not
    granted (the ``apps.uninstall_apps``/``restore_apply`` pattern: a destructive
    core call never proceeds on an implicit yes), after resolving the container,
    bench, and site so the prompt names the real target and a missing site is a
    ``NOT_FOUND`` refusal rather than a confirmation for something that does not
    exist. ``db_root_password`` defaults to the one stored for the instance at
    init, else the compose default ``123`` (:func:`core.db_password.resolve`),
    shielded off the argv/echo via the exec environment, not randomized.

    See the module docstring for the backup gate and the archive-relocation
    contract this owns.
    """
    emit: OnEvent = on_event or _noop
    warnings: list[Message] = []

    _validate_plain_component(project_name, kind="project")
    resolvers.validate_site_name(site)
    _validate_plain_component(site, kind="site")

    resolved = resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )
    if isinstance(resolved, Result):
        return resolved
    container, path, resolve_warnings = resolved
    warnings.extend(resolve_warnings)

    resolvers.validate_bench_path(path)
    resolvers.require_bench_dir(container, path)
    resolvers.require_site_dir(container, path, site)

    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_drop_site",
                param="consent",
                prompt=(
                    f"Permanently drop site '{site}' on bench '{path}' in project "
                    f"'{project_name}'? This deletes its database and files."
                ),
            ),
        )

    drop_dir = backup_before_drop(container, project_name, path, site, on_event=emit)
    dropped = drop_backed_up_site(
        container,
        project_name,
        path,
        site,
        drop_dir,
        db_root_password=db_root_password,
        on_event=emit,
    )
    return Result(status=dropped.status, data=dropped.data, warnings=[*warnings, *dropped.warnings])
