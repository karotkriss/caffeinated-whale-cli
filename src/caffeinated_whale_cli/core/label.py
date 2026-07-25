"""``core.label`` - list benches, and set or clear a bench's durable user label.

Three functions rather than one, because ``label`` is two operations wearing one
command. Listing is a SEPARATE function, not a mode: ``commands/label.py`` treated
a missing bench selector as a legitimate read request, and a read request is not a
``NEEDS_CHOICE`` - that envelope status is for decisions the core cannot make from
its params, and "list the benches" is one the caller already made.

:func:`list_benches` no longer serves the cache unqualified: every row carries a
``present``/``absent``/``unverified`` state, cross-checked in one exec against the
live container. See :func:`resolvers.present_bench_paths` for the rule and why it
lives at the layer rather than here.

Setting and clearing are likewise separate rather than one ``label=None``-means-clear
function. That sentinel is the shape ``_stop_project``'s ``None`` return was
retired from; ``db_utils.set_bench_label`` keeps the ``None``-clears convention at
the storage layer, where it is a storage detail, and the core does not propagate it
into an API a CLI, an agent surface, and a future GUI all consume.

``set_labels`` is the batched sibling of ``set_label`` - the ``inspect -i`` verb -
owning the same validate/uniqueness/marker/cache rule for several benches at once,
with a container-unavailable degrade (stopped, gone, or the daemon unreachable) and
per-assignment (never batch-aborting) outcomes. It exists so ``inspect -i`` calls
that rule instead of re-implementing it.

Built entirely from existing primitives (``core.docker.get_frappe_container``,
``resolvers.resolve_container_state`` / ``resolve_bench`` / ``cached_benches``,
``utils.bench_labels``, ``db_utils.set_bench_label``, the envelope). The one
existing primitive this batch had to touch is ``resolve_container_state``'s
hardcoded ``NOT_RUNNING`` hint, which named a ``--yes`` flag ``label`` does not
have; it is now a parameter.

The two-store write is the dangerous part of this module - see ``clear_label``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..utils import bench_labels, db_utils
from . import docker as core_docker
from . import resolvers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

# `label` deliberately does not auto-start a stopped project: the marker lives
# inside the bench, and a label change is not a reason to spin an instance up.
# This is the hint for that refusal - the primitive's default names `--yes`,
# which `label` has no such flag for.
_NOT_RUNNING_HINT = "Start the project first - the label marker is stored inside the bench."


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchInfo:
    """One bench's addressing info (serializable, no live objects).

    ``index`` is the bench's durable numeric identity and is what ``--bench``
    accepts. Discovery order may change when another path is added, but this value
    remains attached to the same path. A user label remains the readable handle.

    ``state`` says whether this row is still true: ``present`` | ``absent`` |
    ``unverified`` (:mod:`core.resolvers`' shared vocabulary). Every other field
    here is remembered, not observed - this one says whether to trust them, and it
    is on the ROW rather than only in a warning so an agent parsing the TOON table
    can act on it without parsing prose.
    """

    index: int
    path: str
    label: str | None
    state: str = resolvers.BENCH_UNVERIFIED


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchList:
    """A project's benches. Wrapped in a dataclass (not a bare list) so the axi
    serializer's ``asdict`` has a dataclass at the top, mirroring ``WhereResult``."""

    project: str
    benches: list[BenchInfo]
    #: True only when the live existence check actually ran and answered.
    verified: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class LabelResult:
    """One bench's outcome from the batched :func:`set_labels` apply.

    ``applied`` is False for a rejected assignment (``error`` carries why);
    ``marker_written`` is False when the marker could not be written - either the
    container is unavailable (stopped, gone, or the daemon unreachable; batch-wide,
    see the ``label.marker_skipped`` warning) or the per-bench write failed - and
    the label lives in the cache only.
    """

    bench_path: str
    label: str
    applied: bool
    marker_written: bool
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LabelOutcome:
    """The typed outcome of a set/clear (serializable, no live objects).

    ``previous_label`` is carried because renaming a label is a documented use
    and "what was it before" is the one fact the caller cannot recover afterward.
    """

    project: str
    bench_path: str
    label: str | None  # the label now in effect; None after a clear
    previous_label: str | None
    marker_path: str
    cleared: bool


def _marker_path(bench_path: str) -> str:
    return f"{bench_path.rstrip('/')}/{bench_labels.MARKER_REL_PATH}"


def _require_benches(project_name: str) -> list[dict]:
    """The project's cached benches, or NOT_FOUND naming the remedy.

    An uninspected project is NOT an empty list: "never inspected" and "has zero
    benches" are different facts, and only one of them has a fix the caller can act on.
    """
    benches = resolvers.cached_benches(project_name)
    if not benches:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "benches.none_cached",
            f"No cached benches for project '{project_name}'.",
            hint=f"Run 'cwcli inspect {project_name}' first.",
        )
    return benches


def list_benches(project_name: str, *, verify: bool = True) -> Result[BenchList]:
    """The project's benches with their indices, labels, and existence state.

    The rows come from the cache; ``verify`` (default on) cross-checks each path
    against the live container in ONE exec, so a bench whose directory is gone is
    reported ``absent`` instead of vouched for. It used to touch no container at
    all, which is exactly how it kept confidently reporting a removed bench across
    repeated probes while the host and the container both agreed it was gone - the
    third read surface caught doing that, and the reason the rule now lives at the
    layer (see :func:`resolvers.present_bench_paths`).

    Verification is best-effort by design, because this verb must keep answering
    when there is nothing to ask: it is what tells a caller which ``--bench`` to
    pass to ``cwcli start``, on a project that is by definition stopped. A stopped
    project, a missing container, or an unreachable daemon leaves every row
    ``unverified`` with a warning saying so - never ``present``, and never an
    error. ``verify=False`` is the explicit opt-out for a caller that has already
    established liveness; it reports ``unverified`` rather than buying speed by
    pretending.

    Nothing is pruned here: an ``absent`` row is reported with the remedy named.
    """
    benches = _require_benches(project_name)
    paths = [b["path"] for b in benches]

    present = _present_paths(project_name, paths) if verify else None
    rows = [
        BenchInfo(
            index=b.get("index", position),
            path=b["path"],
            label=b.get("label"),
            state=(
                resolvers.BENCH_UNVERIFIED
                if present is None
                else (resolvers.BENCH_PRESENT if b["path"] in present else resolvers.BENCH_ABSENT)
            ),
        )
        for position, b in enumerate(benches)
    ]

    warnings: list[Message] = []
    if present is None:
        if verify:
            warnings.append(
                Message(
                    "benches.unverified",
                    "Could not reach the project's container, so these paths were not "
                    "checked; they are cached and a bench may no longer exist.",
                )
            )
    else:
        stale = [row.path for row in rows if row.state == resolvers.BENCH_ABSENT]
        if stale:
            warnings.append(
                Message(
                    "benches.stale",
                    f"{len(stale)} cached bench(es) no longer exist: {', '.join(stale)}. "
                    f"Run 'cwcli inspect {project_name} --update' to refresh the cache.",
                    detail={"benches": stale},
                )
            )

    return Result(
        status=Status.WARNING if warnings else Status.OK,
        data=BenchList(project=project_name, benches=rows, verified=present is not None),
        warnings=warnings,
    )


def _resolve_target(
    project_name: str, bench: str | None
) -> tuple[Result[LabelOutcome] | None, list[dict], str, list[Message]]:
    """Resolve which bench to act on, shared by set_label and clear_label.

    Returns ``(early_result, benches, bench_path, warnings)``. ``early_result`` is
    a ``NEEDS_CHOICE`` to hand straight back to the caller, or None to proceed.
    """
    benches = _require_benches(project_name)
    bench_result = resolvers.resolve_bench(project_name, bench, None)

    # `_require_benches` guarantees a non-empty cache, which is the only case
    # `resolve_bench` returns None for.
    assert bench_result is not None
    if bench_result.status is Status.NEEDS_CHOICE:
        return (
            Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice),
            benches,
            "",
            [],
        )

    assert bench_result.data is not None
    return None, benches, bench_result.data, list(bench_result.warnings)


def _running_container(project_name: str):
    """The frappe container, or NOT_RUNNING. Never starts anything."""
    frappe_container = core_docker.get_frappe_container(project_name)
    resolvers.resolve_container_state(
        project_name,
        frappe_container,
        auto_start=False,
        offer_choice=False,
        not_running_hint=_NOT_RUNNING_HINT,
    )
    return frappe_container


def _container_or_none(project_name: str):
    """The running frappe container, or None for the states a cached read degrades on.

    "Degrades on" is one set - a stopped project, a gone container, an unreachable
    daemon - shared by :func:`container_available`, :func:`set_labels`' cache-only
    fallback, and :func:`list_benches`' verification. Anything else propagates.
    """
    try:
        return _running_container(project_name)
    except CwcliError as exc:
        if exc.kind in (ErrorKind.NOT_RUNNING, ErrorKind.NOT_FOUND, ErrorKind.DOCKER):
            return None
        raise


def _present_paths(project_name: str, paths: list[str]) -> set[str] | None:
    """The subset of ``paths`` that still exist, or None if it could not be asked."""
    container = _container_or_none(project_name)
    if container is None:
        return None
    return resolvers.present_bench_paths(container, paths)


def container_available(project_name: str) -> bool:
    """Whether the project's frappe container is resolvable and running.

    A serializable pre-check ``inspect -i`` calls BEFORE prompting, so it can warn
    the user up front that labels will be cache-only - rather than only finding
    out from :func:`set_labels`'s own degrade after the whole prompt loop has run.
    """
    return _container_or_none(project_name) is not None


def _label_of(benches: list[dict], bench_path: str) -> str | None:
    for bench in benches:
        if bench["path"] == bench_path:
            return bench.get("label")
    return None


def set_label(project_name: str, *, bench: str | None = None, label: str) -> Result[LabelOutcome]:
    """Assign a durable user label to a bench, writing the marker then the cache."""
    early, benches, bench_path, warnings = _resolve_target(project_name, bench)
    if early is not None:
        return early

    label = label.strip()
    error = bench_labels.validate_user_label(label)
    if error is None:
        # Uniqueness is the caller's job (bench_labels has no sibling context).
        # Keyed on path, not dict identity: a bench IS its path - `set_bench_label`
        # keys on it and the marker lives at it.
        duplicate = any(
            other["path"] != bench_path and other.get("label") == label for other in benches
        )
        if duplicate:
            error = f"Label '{label}' is already used by another bench in this project."
    if error:
        raise CwcliError(ErrorKind.USAGE, "label.invalid", error)

    previous = _label_of(benches, bench_path)
    frappe_container = _running_container(project_name)

    if not bench_labels.write_label_marker(frappe_container, bench_path, label):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "label.marker_write_failed",
            "Failed to write the marker file inside the container; label not saved.",
        )
    # The DB write's return value is deliberately NOT checked here, unlike the
    # clear path below - and this asymmetry is CORRECT, not an oversight.
    #
    # The marker (written and verified just above) is the source of truth for
    # labels: `core.inspect` re-reads it and writes the recovered label back into
    # the cache (`core/inspect.py`, "Recover the user label from the per-bench
    # marker file"). So if this cache write silently misses (the bench row is not
    # cached, `set_bench_label` returns False), the next inspect converges the
    # cache to EXACTLY the label the user just asked for. A missed set self-heals
    # toward the user's intent, so failing loud here would only turn a
    # self-correcting case into a hard error.
    #
    # `clear_label` cannot do this: there, a missed cache clear leaves a label the
    # user DELETED lingering as a live `--bench` handle - a silent resurrection of
    # removed data, in the WRONG direction - so it must fail closed. The two paths
    # are asymmetric because their recovery directions are.
    db_utils.set_bench_label(project_name, bench_path, label)

    return Result(
        status=Status.OK,
        data=LabelOutcome(
            project=project_name,
            bench_path=bench_path,
            label=label,
            previous_label=previous,
            marker_path=_marker_path(bench_path),
            cleared=False,
        ),
        warnings=warnings,
    )


def set_labels(project_name: str, assignments: list[tuple[str, str]]) -> Result[list[LabelResult]]:
    """Apply several bench labels in one pass - the interactive-``inspect`` verb.

    Owns the SAME validate + cross-bench-uniqueness rule as :func:`set_label` and
    the SAME marker-then-cache persistence, so that rule has one owner rather than
    a copy re-implemented in ``inspect -i``. Three things differ, each a real need
    of the interactive path:

    - a running, reachable container is OPTIONAL. ``inspect -i`` against a
      STOPPED project, a project whose containers are gone, or a Docker daemon
      that is momentarily unreachable still records labels to the cache
      (markers skipped, one batch-wide ``label.marker_skipped`` warning),
      rather than the hard error :func:`set_label` raises for the same cases;
    - each assignment reports its own outcome (a rejected label does not abort the
      batch - the loop keeps the previous label for that bench and moves on);
    - uniqueness is resolved first-wins WITHIN the batch (an accepted label is
      reflected before the next assignment's duplicate check), matching the old
      in-memory cross-bench check.

    Persistence reads the cached benches and writes them back (one
    ``cache_project_data``), so it never re-persists a T2 read-only refresh the way
    the old ``inspect -i`` inadvertently could.
    """
    benches = _require_benches(project_name)
    by_path = {b["path"]: b for b in benches}

    # A running, reachable container lets us write markers; any container
    # unavailability (stopped, gone, or the daemon unreachable) degrades to
    # cache-only rather than refusing (the interactive-inspect affordance).
    warnings: list[Message] = []
    frappe_container = _container_or_none(project_name)
    if frappe_container is None:
        warnings.append(
            Message(
                code="label.marker_skipped",
                text=(
                    "Containers are not available; labels saved to the cache only "
                    "(marker files not written)."
                ),
            )
        )

    results: list[LabelResult] = []
    for bench_path, raw_label in assignments:
        label = raw_label.strip()
        target = by_path.get(bench_path)
        if target is None:
            results.append(
                LabelResult(
                    bench_path=bench_path,
                    label=label,
                    applied=False,
                    marker_written=False,
                    error=f"No cached bench at path '{bench_path}'.",
                )
            )
            continue

        error = bench_labels.validate_user_label(label)
        if error is None:
            # First-wins across the batch: an accepted label is already on its
            # bench dict by the time a later assignment checks for a duplicate.
            duplicate = any(
                other["path"] != bench_path and other.get("label") == label for other in benches
            )
            if duplicate:
                error = f"Label '{label}' is already used by another bench in this project."
        if error:
            results.append(
                LabelResult(
                    bench_path=bench_path,
                    label=label,
                    applied=False,
                    marker_written=False,
                    error=error,
                )
            )
            continue

        marker_written = False
        if frappe_container is not None:
            marker_written = bench_labels.write_label_marker(frappe_container, bench_path, label)
        target["label"] = label
        results.append(
            LabelResult(
                bench_path=bench_path,
                label=label,
                applied=True,
                marker_written=marker_written,
                error=None,
            )
        )

    db_utils.cache_project_data(project_name, benches)
    return Result(status=Status.OK, data=results, warnings=warnings)


def clear_label(project_name: str, *, bench: str | None = None) -> Result[LabelOutcome]:
    """Remove a bench's user label, clearing the marker BEFORE the cache.

    The ordering is load-bearing and must not be "tidied": the marker is the
    source of truth for label recovery, so clearing the cache while the marker
    survives would let a later full ``inspect`` resurrect the label the user just
    deleted. Both failure modes therefore fail closed.
    """
    early, benches, bench_path, warnings = _resolve_target(project_name, bench)
    if early is not None:
        return early

    previous = _label_of(benches, bench_path)
    frappe_container = _running_container(project_name)

    # Marker FIRST: a failure here must leave the cache label intact, or the
    # marker would resurrect it on the next full inspect.
    if not bench_labels.clear_label_marker(frappe_container, bench_path):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "label.marker_clear_failed",
            "Could not remove the marker file inside the container; label left "
            "unchanged to avoid the marker resurrecting it later.",
        )
    if not db_utils.set_bench_label(project_name, bench_path, None):
        raise CwcliError(
            ErrorKind.INTERNAL,
            "label.db_clear_failed",
            "Removed the marker file but could not clear the label in the cache database.",
            hint="Run 'cwcli inspect --update' to reconcile.",
        )

    return Result(
        status=Status.OK,
        data=LabelOutcome(
            project=project_name,
            bench_path=bench_path,
            label=None,
            previous_label=previous,
            marker_path=_marker_path(bench_path),
            cleared=True,
        ),
        warnings=warnings,
    )
