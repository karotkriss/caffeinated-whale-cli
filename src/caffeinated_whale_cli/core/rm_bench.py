"""``core.remove_bench`` - remove ONE bench from an instance, never the instance.

The bench-scoped sibling of ``core.rm`` (whole instance) and ``core.rm_site``
(one site): a single frappemate instance accumulates ~23 benches at ~2.6 GB, and
every one boots on ``start``/``scale``, so an OOM-tight box needs a way to thin
them without destroying the instance. Until this module existed the only way to
drop a bench was ``docker exec ... rm -rf`` past cwcli, with no backup and no
guard - the exact escape hatch the agent surface exists to close. Following the
``docker rm`` vs ``docker volume rm`` model ``rm``/``rm-site`` already established,
destruction scope is explicit in the verb (``rm-bench``), never a ``--bench``
modifier on the bigger ``rm`` (forgetting that flag would nuke the instance).

**What a bench physically is, and therefore what "remove" means.** In cwcli's
dev layout a bench is NOT its own container and has NO dedicated named volume:
an instance is ONE frappe container (plus mariadb/redis) whose ``/workspace`` is
a bind mount to ``CWCLI_HOME/projects/<project>/data/``, and each bench is a
directory under it (``/workspace/<bench>``). A bench's persistent data is (1)
that directory on the bind mount and (2) its sites' databases in the SHARED
mariadb. So removing a bench means dropping each of its sites - which backs the
site up AND removes its database - and then deleting the bench directory, while
every sibling bench (and every container) keeps running. There is no per-bench
volume to remove; the whole-instance named volumes belong to ``core.rm``.

**Composition, not a reimplemented gate.** Each site is removed by reusing the
proven, real-instance-validated :func:`core.rm_site.drop_site`: ``bench
drop-site --force`` backs the site up AND drops its database in one command, and
cwcli then copies that credential-bearing archive OUT to the managed host
archive (``cwcli_home()/archive/<project>_dropped_sites/``) and verifies it
landed non-empty on the host. This is not ``core.rm``'s verified-backup-BEFORE-
destroy gate (bench's own backup runs first, but as part of the same drop): the
data-safety property here is that the bench DIRECTORY is deleted ONLY once EVERY
site's archive is confirmed on the host, so no site's backup is destroyed
unverified. If any site's archive could not be copied out, the directory (which
still holds that trapped in-container archive) is kept and the failure is
reported, so the one place the backup survives is never destroyed.

**Refuse while running.** A bench whose dev processes are up (cwcli supervisord
OR a honcho / ``bench start`` manager) is refused with ``CONFLICT`` before
anything is touched, naming ``cwcli stop <project> --bench <selector>``; a live
bench is never deleted out from under its own running code.

Consent is a CORE parameter (the ``apps uninstall``/``rm-site`` lesson): without
it the function returns ``NEEDS_CHOICE``/``confirm_remove_bench`` rather than
proceeding on an implicit yes, so ``axi`` and a future GUI cannot bypass it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..utils import bench_sites
from . import resolvers, supervision
from . import rm_site as core_rm_site
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind

# --------------------------------------------------------------------------- DTO


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRemovalOutcome:
    """The typed outcome of a bench removal (serializable, no live objects).

    ``ok`` is True only when EVERY site was dropped with its backup archive
    confirmed on the host AND the bench directory was removed. A run where a
    site's archive could not be safely copied out leaves the directory in place
    (to preserve that trapped archive), reports the site in ``sites_failed``,
    and sets ``ok=False`` - so a caller reading only the exit code still learns
    the removal did not fully complete.
    """

    project: str
    bench_path: str
    sites_dropped: list[str]
    sites_failed: list[str]
    archived_host_paths: list[str]
    dir_removed: bool
    ok: bool
    failures: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- typed events


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRmStep:
    """A spinner-label update; ``style`` is a semantic colour token the frontend maps."""

    label: str
    style: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRmNotice:
    """A dim progress line."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRmWarning:
    """A yellow warning, with an optional dim follow-up line."""

    text: str
    hint: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRmCommand:
    """A ``$ ...`` command line forwarded from a per-site ``bench drop-site`` run."""

    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchRmOutput:
    """A chunk of a per-site ``bench drop-site``'s own output, tagged with its stream."""

    stream: str  # "stdout" | "stderr"
    text: str


BenchRmEvent = BenchRmStep | BenchRmNotice | BenchRmWarning | BenchRmCommand | BenchRmOutput
OnEvent = Callable[[BenchRmEvent], None]


def _noop(_event: BenchRmEvent) -> None:
    """The drain-and-discard consumption mode (no renderer)."""


# --------------------------------------------------------------- path-safety guard


def _refuse_unsafe_bench_path(bench_path: str) -> None:
    """Refuse a ``rm -rf`` target that is not a bench nested under a workspace parent.

    ``require_bench_dir`` already proves ``{path}/sites`` exists (so the path IS a
    bench), but a destructive ``rm -rf`` earns its own last-line guard. A bench
    always lives at least two levels deep (``/workspace/<bench>``), so a target
    with fewer than two path segments - ``/``, ``/workspace`` (the shared parent
    that holds every sibling bench), or a bare relative name - is refused. This
    makes "delete every sibling bench at once" unrepresentable regardless of what
    the resolver returned.
    """
    normalized = bench_path.strip().rstrip("/")
    segments = [s for s in normalized.split("/") if s]
    if not normalized or normalized == "/" or len(segments) < 2:
        raise CwcliError(
            ErrorKind.USAGE,
            "bench.unsafe_path",
            f"Refusing to remove {bench_path!r}: it is not a bench directory nested "
            "under a workspace parent.",
        )


# --------------------------------------------------------------- running detection


def _delete_bench_dir(container, bench_path: str) -> None:
    """``rm -rf`` the bench directory inside the container (a bind mount, so the host
    copy goes too). Raises a typed ``CwcliError`` on any failure so a partial or
    failed delete never reads as success. ``container`` is unannotated (the codebase
    pattern for a live docker-py object the core does not re-type)."""
    try:
        exit_code, output = container.exec_run(["rm", "-rf", bench_path])
    except Exception as e:  # noqa: BLE001 - surface as a typed failure, never crash
        raise CwcliError(
            ErrorKind.DOCKER,
            "bench.dir_remove_failed",
            f"Could not remove bench directory {bench_path}.",
            detail={"output": str(e)},
        ) from e
    if exit_code != 0:
        detail = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "bench.dir_remove_failed",
            f"Could not remove bench directory {bench_path} (rm exited {exit_code}).",
            detail={"output": detail},
        )


def _bench_is_running(container, bench_path: str) -> bool:
    """True if the bench has live dev processes (cwcli supervisord OR honcho / bench start).

    Mirrors ``core.status``'s two-path liveness read: ``discover_stack`` finds a
    cwcli supervisord for the bench, and the unsupervised fallback finds a honcho
    / ``bench start`` manager rooted at the bench. Either means the bench is
    serving and must not be deleted out from under itself.
    """
    if supervision.discover_stack(container, bench_path).supervisor_up:
        return True
    return supervision.discover_unsupervised_stack(container, bench_path).manager_up


def _selector_for_path(project_name: str, bench_path: str) -> str:
    """The real ``--bench`` selector (index, else label) for a resolved bench path.

    So the running-bench refusal names the concrete ``cwcli stop <p> --bench 0``
    even when the caller omitted ``--bench`` (a single-bench instance, or a --path
    override) - the axi surface already substitutes the real index, and the human
    hint should too rather than printing a literal ``<index|label>`` placeholder.
    Falls back to that placeholder only when the path is not in the cache.
    """
    for cached in resolvers.cached_benches(project_name):
        if cached.get("path") == bench_path:
            index = cached.get("index")
            if index is not None:
                return str(index)
            if cached.get("label"):
                return str(cached["label"])
            break
    return "<index|label>"


# --------------------------------------------------------------------- remove_bench


def remove_bench(
    project_name: str,
    *,
    bench: str | None = None,
    bench_path: str | None = None,
    auto_start: bool = False,
    consent: bool = False,
    db_root_password: str = "123",
    on_event: OnEvent | None = None,
) -> Result[BenchRemovalOutcome]:
    """Remove ONE bench: back up + drop its sites, then delete its directory.

    See the module docstring. Resolves the container and the ``--bench`` selector,
    refuses a running bench (``CONFLICT``/``bench.running``, naming ``cwcli stop``),
    then - once consent is granted - drops each site through
    :func:`core.rm_site.drop_site` (which backs it up and relocates its archive to
    the host) and deletes the bench directory only if every archive is confirmed
    on the host.

    Returns ``NEEDS_CHOICE`` (``confirm_start`` for a stopped container, then
    ``select_bench`` for an unselected multi-bench project, then
    ``confirm_remove_bench`` for missing consent) the same way every other
    bench-scoped verb does; each frontend resolves it its own way. Raises
    ``CwcliError`` for hard failures (Docker unreachable, no such bench, a running
    bench, a ``bench drop-site`` that itself failed).
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

    resolvers.validate_bench_path(path)
    resolvers.require_bench_dir(container, path)
    _refuse_unsafe_bench_path(path)

    # Refuse a live bench BEFORE consent: never confirm deleting something that is
    # actively serving, and name the exact stop command so the refusal is actionable.
    if _bench_is_running(container, path):
        selector = bench if bench is not None else _selector_for_path(project_name, path)
        raise CwcliError(
            ErrorKind.CONFLICT,
            "bench.running",
            f"Bench '{path}' in project '{project_name}' is still running.",
            hint=f"Stop it first with 'cwcli stop {project_name} --bench {selector}', then re-run.",
        )

    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_remove_bench",
                param="consent",
                prompt=(
                    f"Permanently remove bench '{path}' from project '{project_name}'? "
                    "This drops every site on it (deleting their databases and files) "
                    "and deletes the bench directory."
                ),
            ),
            warnings=warnings,
        )

    sites = bench_sites.list_sites(container, path)
    if sites is None:
        # Cannot enumerate the sites, so cannot guarantee a backup of each - fail
        # closed rather than delete a directory whose sites were never backed up.
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "bench.sites_unknown",
            f"Could not list sites under {path}/sites; the bench was not removed.",
        )

    sites_dropped: list[str] = []
    sites_failed: list[str] = []
    archived_host_paths: list[str] = []
    failures: list[str] = []

    def _forward(event) -> None:
        if isinstance(event, core_rm_site.DropSiteCommand):
            emit(BenchRmCommand(command=event.command))
        elif isinstance(event, core_rm_site.DropSiteOutput):
            emit(BenchRmOutput(stream=event.stream, text=event.text))
        elif isinstance(event, core_rm_site.DropSiteNotice):
            emit(BenchRmNotice(text=event.text))

    for site in sites:
        emit(BenchRmStep(label=f"Backing up and dropping site '{site}'...", style="cyan"))
        result = core_rm_site.drop_site(
            project_name,
            site,
            bench_path=path,
            consent=True,
            db_root_password=db_root_password,
            on_event=_forward,
        )
        # drop_site with consent=True and an explicit bench_path against a running
        # container never returns NEEDS_CHOICE; a hard failure raised CwcliError
        # above and propagates (the bench directory, and any partial archive, is
        # left intact for recovery).
        assert result.data is not None
        outcome = result.data
        # Skip drop_site's "could not prune the in-container copy" warning: we are
        # about to delete the whole bench directory, so that copy goes with it.
        for warning in result.warnings:
            if warning.code != "rm_site.archive_not_pruned":
                warnings.append(warning)
        if outcome.archived_host_path is not None:
            sites_dropped.append(site)
            archived_host_paths.append(outcome.archived_host_path)
        else:
            # The site's database was dropped but its backup archive could not be
            # copied out of the container. Keep the bench directory so that trapped
            # archive is not destroyed.
            sites_failed.append(site)
            failures.append(f"site '{site}' was dropped but its backup could not be copied out")

    dir_removed = False
    if sites_failed:
        emit(
            BenchRmWarning(
                text=(
                    f"Not deleting bench directory {path}: "
                    f"{len(sites_failed)} site(s) have a backup archive still inside the "
                    "container that could not be copied out."
                ),
                hint="Copy those archives out manually, then remove the bench directory.",
            )
        )
    else:
        emit(BenchRmStep(label=f"Removing bench directory {path}...", style="red"))
        _delete_bench_dir(container, path)
        dir_removed = True
        emit(BenchRmNotice(text=f"Removed bench directory {path}"))

    ok = dir_removed and not sites_failed
    return Result(
        status=Status.OK if ok else Status.WARNING,
        data=BenchRemovalOutcome(
            project=project_name,
            bench_path=path,
            sites_dropped=sites_dropped,
            sites_failed=sites_failed,
            archived_host_paths=archived_host_paths,
            dir_removed=dir_removed,
            ok=ok,
            failures=failures,
        ),
        warnings=warnings,
    )
