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
established). It adds exactly one new piece of logic: what happens to the
archive ``bench drop-site`` creates.

**The archive decision (the hard requirement this module exists to satisfy).**
``bench drop-site`` (without ``--no-backup``) takes its own backup, then MOVES
the site's whole directory - ``site_config.json`` (the database credentials)
included - into ``{bench_path}/archived/sites/{site}`` inside the container
(verified against a real bench: the path is deterministic, named after the
site, not timestamped). Left there, that folder grows by one credential-bearing
entry every drop, forever, unpruned, inside a container that keeps running for
the rest of the bench's life. That is the same unbounded-residue shape the
network-leak fix in ``core.rm`` closed for the whole-instance path, so it gets
the same answer here, not a different one: cwcli does NOT rely on bench's own
archive as the durable copy. Immediately after a successful drop, this module
copies that one archived path out to the SAME managed host archive location
``core.rm`` already uses (``cwcli_home()/archive/``, disclosed and never
silently grown), and - ONLY once that copy is verified non-empty on the host -
deletes the in-container copy. A copy that cannot be verified is left in place
rather than deleted unbacked (fail closed, the ``core.rm`` C1 gate's own logic
applied here), and every one of these outcomes rides ``DropSiteOutcome.ok`` /
warnings rather than a silent success: a caller that only checks the exit code
still finds out its site's credentials did not make it safely out of the
container.

This is NOT a verified-backup-BEFORE-destroy gate the way ``core.rm``'s is:
bench's own backup-then-archive-then-drop runs as one external command before
cwcli ever learns the archive's path, so there is no point at which cwcli could
gate the destruction itself on the archive's fate. What it can, and does, own
is the archive's fate immediately AFTER, honestly reported either way.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..utils.config_utils import cwcli_home
from . import resolvers
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind
from .exec_stream import ExecChunk, exec_stream

# --------------------------------------------------------------------------- DTO


@dataclass(frozen=True, slots=True, kw_only=True)
class DropSiteOutcome:
    """The typed outcome of a site drop (serializable, no live objects).

    ``ok`` is True only when the site was dropped AND its archive was both
    copied out to ``archived_host_path`` and pruned from the container - the
    fully clean case. A drop that succeeded but left its archive un-relocated
    or un-pruned still reports the site as gone (it is), but ``ok=False`` and a
    warning names exactly what remains, so a caller reading only the exit code
    still learns its site's credentials may not be safely out of the container.
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


def _archived_site_path(bench_path: str, site: str) -> str:
    """Where ``bench drop-site`` moves a dropped site's directory.

    Deterministic and named after the site, NOT timestamped - verified against
    a real bench (see the module docstring), so no before/after diff is needed
    to find it: the path is known before the drop even runs.
    """
    return f"{bench_path}/archived/sites/{site}"


def _archived_site_exists(container, path: str) -> bool:
    exit_code, _ = container.exec_run(["test", "-d", path])
    return bool(exit_code == 0)


def _prune_archived_site(container, source_path: str) -> bool:
    """Delete ``source_path`` inside the container. True only if the exit code was 0."""
    exit_code, _ = container.exec_run(["rm", "-rf", source_path])
    return bool(exit_code == 0)


def _copy_out_archived_site(container, source_path: str, dest_file: Path) -> bool:
    """Copy ``source_path`` (a directory) out of the container as a raw host tar.

    Uses ``get_archive`` exactly as ``core.rm`` does, but writes the tar stream
    to the host UNPARSED - the archive is an opaque credential-bearing backup
    blob (like ``core.rm``'s ``conf/`` copy), not something cwcli needs to
    inspect, so there is no reason to pay for extraction. Returns True only if
    the resulting host file is non-empty.
    """
    try:
        stream, _stat = container.get_archive(source_path)
        with open(dest_file, "wb") as out:
            for chunk in stream:
                out.write(chunk)
    except Exception:
        return False
    try:
        return dest_file.stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------------- drop_site


def drop_site(
    project_name: str,
    site: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    consent: bool = False,
    db_root_password: str = "123",
    on_event: OnEvent | None = None,
) -> Result[DropSiteOutcome]:
    """Permanently drop ONE site (``bench drop-site --force``), then relocate its archive.

    ``site`` is a required, explicit target - unlike ``backup``/``unlock``, there
    is no default-site fallback, matching ``bench_ops.run_tests``' captain ruling
    S1: an unbounded destructive effect must never default its target.

    Returns ``NEEDS_CHOICE``/``confirm_drop_site`` when ``consent`` is not
    granted (the ``apps.uninstall_apps``/``restore_apply`` pattern: a destructive
    core call never proceeds on an implicit yes), after resolving the container,
    bench, and site so the prompt names the real target and a missing site is a
    ``NOT_FOUND`` refusal rather than a confirmation for something that does not
    exist. ``db_root_password`` defaults to ``"123"`` for the same reason
    ``core.init``'s ``init_bench`` does: it mirrors the downloaded compose's
    hardcoded ``MYSQL_ROOT_PASSWORD: 123``, shielded off the argv/echo via the
    exec environment, not randomized.

    See the module docstring for the archive-relocation contract this owns.
    """
    emit: OnEvent = on_event or _noop
    warnings: list[Message] = []

    resolved = resolvers.resolve_container_and_bench(
        project_name, bench, bench_path, auto_start=auto_start
    )
    if isinstance(resolved, Result):
        return resolved
    container, path, resolve_warnings = resolved
    warnings.extend(resolve_warnings)

    resolvers.validate_site_name(site)
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

    # Secret rides the environment, never the argv (the restore.py M5 pattern):
    # the traced command below is the $-ref, never the value.
    env = {"CWCLI_DB_ROOT_PASSWORD": db_root_password}
    cmd_str = (
        f"bench drop-site {shlex.quote(site)} "
        '--db-root-password "$CWCLI_DB_ROOT_PASSWORD" --force'
    )
    emit(DropSiteCommand(command=cmd_str))
    exit_code = 1
    for event in exec_stream(container, ["sh", "-c", cmd_str], workdir=path, environment=env):
        if isinstance(event, ExecChunk):
            emit(DropSiteOutput(stream=event.stream, text=event.text))
        else:
            exit_code = event.exit_code

    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "rm_site.failed",
            f"Failed to drop site '{site}' (bench drop-site exited {exit_code}).",
        )

    source = _archived_site_path(path, site)
    archived_host_path: str | None = None
    archive_pruned = False

    if _archived_site_exists(container, source):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_dir = cwcli_home() / "archive" / f"{project_name}_dropped_sites"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f"{site}_{timestamp}.tar"

        if _copy_out_archived_site(container, source, dest_file):
            archived_host_path = str(dest_file)
            emit(DropSiteNotice(text=f"Archived dropped site to {dest_file}"))
            archive_pruned = _prune_archived_site(container, source)
            if not archive_pruned:
                warnings.append(
                    Message(
                        "rm_site.archive_not_pruned",
                        f"Copied the dropped site's archive to {dest_file}, but could not "
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
                    "not copy it out; it was left in place rather than deleted unbacked. Copy "
                    "it out manually, then remove it.",
                )
            )
    else:
        warnings.append(
            Message(
                "rm_site.archive_not_found",
                f"bench did not archive the dropped site at the expected path ({source}); "
                "nothing was copied out or pruned.",
            )
        )

    ok = archived_host_path is not None and archive_pruned

    return Result(
        status=Status.OK if ok else Status.WARNING,
        data=DropSiteOutcome(
            project=project_name,
            site=site,
            bench_path=path,
            archived_host_path=archived_host_path,
            archive_pruned_in_container=archive_pruned,
            ok=ok,
        ),
        warnings=warnings,
    )
