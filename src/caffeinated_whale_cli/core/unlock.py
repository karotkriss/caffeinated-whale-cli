"""``core.unlock`` - remove a site's locks directory.

`backup`'s near-twin: the eight steps between "resolve the container" and "do the
thing" are the SAME steps in the SAME order, so this is built entirely from the
primitives ``core.backup`` already uses (``core.docker.get_frappe_container``,
``resolvers.resolve_container_state`` / ``resolve_bench`` / ``resolve_default_site``
/ the validation + probe helpers, the envelope). It adds none of its own: that is
the point of the exercise - if a bench-op verb needed a primitive bent to fit, the
core foundation would have been overfitted to its first caller.

The removal is BUFFERED, not streamed. The old CLI streamed ``rm -rfv`` chunk by
chunk so verbose mode could echo the removed paths, but the payload is a locks
directory - a handful of small files, gone in milliseconds. Buffering it into
``UnlockOutcome.removed`` keeps streaming machinery out of this verb (it belongs
to ``logs``/``update``, which are genuinely long-lived) and hands the agent
surface a structured list rather than an opaque byte stream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import docker as core_docker
from . import resolvers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

# `rm -rfv` reports one line per removal: "removed '<path>'" for a file and
# "removed directory '<path>'" for a directory.
_REMOVED_RE = re.compile(r"^removed (?:directory )?'(.*)'$")


@dataclass(frozen=True, slots=True, kw_only=True)
class UnlockOutcome:
    """The typed outcome of an unlock (serializable, no live objects)."""

    site: str
    bench_path: str
    locks_path: str
    removed: list[str]
    already_unlocked: bool


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


def _parse_removed(output: str) -> list[str]:
    """The paths ``rm -rfv`` reported removing, in the order it reported them."""
    paths = []
    for line in output.splitlines():
        match = _REMOVED_RE.match(line.strip())
        if match:
            paths.append(match.group(1))
    return paths


def unlock(
    project_name: str,
    *,
    site: str | None = None,
    bench: str | None = None,
    bench_path: str | None = None,
) -> Result[UnlockOutcome]:
    """Remove ``{bench_path}/sites/{site}/locks``. See the module docstring."""
    warnings: list[Message] = []

    # 1. Resolve the frappe container (raises NOT_FOUND / DOCKER).
    frappe_container = core_docker.get_frappe_container(project_name)

    # 2. Container must be running; a stopped container is a confirm_start fork.
    state = resolvers.resolve_container_state(project_name, frappe_container, offer_choice=True)
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    # 3. Resolve which bench to unlock (--bench/--path, else single, else default).
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        bench_path = resolvers.DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        bench_path = bench_result.data
        warnings.extend(bench_result.warnings)

    assert bench_path is not None  # narrowed by the branches above

    # 4. Resolve the default site when --site was not given.
    if not site:
        site = resolvers.resolve_default_site(project_name, bench_path)
        warnings.append(Message("default_site.resolved", f"Using default site: {site}"))

    # 5-6. Validate the site name and bench path (empty / shell metacharacters).
    resolvers.validate_site_name(site)
    resolvers.validate_bench_path(bench_path)

    # 7-8. Verify the bench directory and the site exist. (argv lists: no shell,
    # no quoting to reason about.)
    resolvers.require_bench_dir(frappe_container, bench_path)
    site_path = resolvers.require_site_dir(frappe_container, bench_path, site)

    # 9. An absent locks directory is a CLEAN SUCCESS, not a NOT_FOUND: `rm -rf`
    # has always exited 0 on a missing target, and a site that is simply not
    # locked is exactly what the caller wanted. Probing explicitly (rather than
    # inferring "nothing was removed" from rm's output) keeps `already_unlocked`
    # independent of rm's human-readable -v format.
    locks_path = f"{site_path}/locks"
    exit_code, _ = frappe_container.exec_run(["test", "-d", locks_path])
    if exit_code != 0:
        return Result(
            status=Status.OK,
            data=UnlockOutcome(
                site=site,
                bench_path=bench_path,
                locks_path=locks_path,
                removed=[],
                already_unlocked=True,
            ),
            warnings=warnings,
        )

    # 10. Remove the locks folder. argv list (no shell); -v so the removed paths
    # can be reported, buffered in one exec rather than streamed.
    exit_code, output = frappe_container.exec_run(["rm", "-rfv", locks_path], workdir=bench_path)
    decoded = _decode(output)
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "unlock.failed",
            f"Failed to unlock site '{site}'",
            detail={"output": decoded},
        )

    return Result(
        status=Status.OK,
        data=UnlockOutcome(
            site=site,
            bench_path=bench_path,
            locks_path=locks_path,
            removed=_parse_removed(decoded),
            already_unlocked=False,
        ),
        warnings=warnings,
    )
