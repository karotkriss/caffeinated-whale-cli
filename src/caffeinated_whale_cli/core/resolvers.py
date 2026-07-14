"""Pure core resolvers split out of ``commands/utils.py``.

Each reports a decision without prompting or printing: it returns the resolved
value, a ``NEEDS_CHOICE`` :class:`~.envelope.Result`, or raises a typed
:class:`~.errors.CwcliError`. The interactive halves (``questionary`` prompts,
the bench-list print, the decline/non-TTY ``typer.Exit``) live in the thin CLI
wrappers in ``commands/utils.py`` that call these.
"""

from __future__ import annotations

from dataclasses import dataclass

from docker.errors import APIError, NotFound

from ..utils import bench_labels, db_utils
from .envelope import Choice, Result, Status
from .errors import CwcliError, ErrorKind

DEFAULT_BENCH_PATH = "/workspace/frappe-bench"


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerState:
    """Whether a project's frappe container is running / should be started."""

    running: bool
    start_requested: bool  # auto-start was requested and the container was down


def resolve_container_state(
    project_name: str,
    frappe_container,
    *,
    auto_start: bool = False,
    offer_choice: bool = True,
) -> Result[ContainerState]:
    """Report a frappe container's run-state without prompting, printing or starting.

    - running -> ``Result(OK, ContainerState(running=True))``
    - stopped + ``auto_start`` -> ``Result(OK, ContainerState(start_requested=True))``
      (the caller performs the actual, UI-coupled start)
    - stopped, no auto-start, ``offer_choice`` -> ``NEEDS_CHOICE`` ``confirm_start``
    - stopped, no auto-start, not ``offer_choice`` -> raises ``CwcliError(NOT_RUNNING)``
    """
    # A container rm'd/errored between resolution and here would otherwise leak a
    # raw docker exception past the core boundary; map it to a typed CwcliError.
    try:
        frappe_container.reload()
    except (APIError, NotFound) as e:
        raise CwcliError(
            ErrorKind.DOCKER,
            "container.reload_failed",
            f"Could not read state of the frappe container for project '{project_name}'.",
            detail={"output": str(e)},
        ) from e

    if frappe_container.status == "running":
        return Result(status=Status.OK, data=ContainerState(running=True, start_requested=False))

    if auto_start:
        return Result(status=Status.OK, data=ContainerState(running=False, start_requested=True))

    if offer_choice:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_start",
                param="auto_start",
                prompt=(
                    f"Frappe container for project '{project_name}' is not running. " "Start it?"
                ),
                default="true",
            ),
        )

    raise CwcliError(
        ErrorKind.NOT_RUNNING,
        "container.not_running",
        f"Frappe container for project '{project_name}' is not running.",
        hint=f"Pass --yes to auto-start it, or start it first with 'cwcli start {project_name}'.",
    )


def _bench_option_label(bench: dict) -> str:
    """The per-bench display used in ``select_bench`` options (mirrors the list text)."""
    label = bench.get("label")
    prefix = f"'{label}' " if label else ""
    return f"{prefix}{bench.get('path', '?')}"


def resolve_bench(
    project_name: str,
    bench_selector: str | None,
    path_override: str | None,
) -> Result[str] | None:
    """Resolve which bench a command should operate on (``--bench``/``--path``).

    Precedence matches the historical ``resolve_bench_path``:

    - ``path_override`` + ``bench_selector`` together -> raises ``CwcliError(USAGE)``
    - ``path_override`` -> ``Result(OK, data=path_override)``
    - ``bench_selector`` -> the matching bench's path, or ``CwcliError(NOT_FOUND)``
    - neither, single bench -> that path (with a ``bench.sole`` warning)
    - neither, multi-bench -> ``NEEDS_CHOICE`` ``select_bench`` (options list the benches)
    - neither, no cached benches -> ``None`` (the caller falls back to its own default)
    """
    from .envelope import Message

    if path_override and bench_selector:
        raise CwcliError(
            ErrorKind.USAGE,
            "bench.selector_conflict",
            "Use either --bench or --path, not both.",
        )

    if path_override:
        return Result(status=Status.OK, data=path_override)

    cached_data = db_utils.get_cached_project_data(project_name)
    benches = (cached_data or {}).get("bench_instances") or []

    if bench_selector is not None:
        chosen = bench_labels.resolve_bench(benches, bench_selector)
        if chosen is None:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "bench.not_found",
                f"No bench '{bench_selector}' in project '{project_name}'.",
            )
        return Result(status=Status.OK, data=chosen["path"])

    if len(benches) == 1:
        path = benches[0]["path"]
        return Result(
            status=Status.OK,
            data=path,
            warnings=[Message("bench.sole", f"Using the only bench: {path}")],
        )

    if len(benches) == 0:
        return None

    # Multiple benches, no selector -> the frontend decides (error, or first-with-note).
    options = [{"value": str(i), "label": _bench_option_label(b)} for i, b in enumerate(benches)]
    return Result(
        status=Status.NEEDS_CHOICE,
        choice=Choice(
            kind="select_bench",
            param="bench",
            prompt=f"Project '{project_name}' has multiple benches; select one.",
            options=options,
        ),
    )
