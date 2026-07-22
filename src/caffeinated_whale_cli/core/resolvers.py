"""Pure core resolvers split out of ``commands/utils.py``.

Each reports a decision without prompting or printing: it returns the resolved
value, a ``NEEDS_CHOICE`` :class:`~.envelope.Result`, or raises a typed
:class:`~.errors.CwcliError`. The interactive halves (``questionary`` prompts,
the bench-list print, the decline/non-TTY ``typer.Exit``) live in the thin CLI
wrappers in ``commands/utils.py`` that call these.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from docker.errors import APIError, NotFound

from ..utils import bench_labels, db_utils
from . import docker as core_docker
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind

DEFAULT_BENCH_PATH = "/workspace/frappe-bench"

# Shell metacharacters rejected in site names and bench paths. Defense in depth:
# every container command is issued as an argv list (no shell), so these can never
# be interpolated as syntax regardless - this rejects them early with a clear error
# rather than letting a nonsense name reach a probe. Kept in ONE place so the
# bench-op verbs (backup, unlock) cannot drift apart on what they reject.
_INVALID_CHARS = [";", "&", "|", "$", "`", "(", ")", "<", ">", "\n", "\r", "\\"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerState:
    """Whether a project's frappe container is running / should be started."""

    running: bool
    start_requested: bool  # auto-start was requested and the container was down


def cached_benches(project_name: str) -> list[dict]:
    """The cached bench list for a project, or ``[]`` when nothing is cached.

    Lives here rather than in ``commands/`` because BOTH layers need it and the
    core cannot import the frontend: it was previously a private helper in
    ``commands/utils.py`` that the core had to re-implement inline.
    """
    cached_data = db_utils.get_cached_project_data(project_name)
    return (cached_data or {}).get("bench_instances") or []


def resolve_container_state(
    project_name: str,
    frappe_container,
    *,
    auto_start: bool = False,
    offer_choice: bool = True,
    not_running_hint: str | None = None,
) -> Result[ContainerState]:
    """Report a frappe container's run-state without prompting, printing or starting.

    - running -> ``Result(OK, ContainerState(running=True))``
    - stopped + ``auto_start`` -> ``Result(OK, ContainerState(start_requested=True))``
      (the caller performs the actual, UI-coupled start)
    - stopped, no auto-start, ``offer_choice`` -> ``NEEDS_CHOICE`` ``confirm_start``
    - stopped, no auto-start, not ``offer_choice`` -> raises ``CwcliError(NOT_RUNNING)``

    ``not_running_hint`` overrides the hint on that raise. The default names
    ``--yes``, which is right for the verbs that have one but wrong for a caller
    like ``label`` that has no such flag and never will - the hint would tell a
    user (and, via axi's ``help:`` line, an agent) to pass a flag that does not
    exist. Parameterized rather than hardcoded for that reason.
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
        hint=not_running_hint
        or (f"Pass --yes to auto-start it, or start it first with 'cwcli start {project_name}'."),
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

    benches = cached_benches(project_name)

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


# --------------------------------------------------------------- shared bench-op steps
#
# The steps every bench-scoped verb performs between "which bench" and "do the
# thing": resolve the default site, validate the site/path, and probe that both
# exist. Extracted here when `unlock` became `backup`'s second caller - one caller
# is not evidence of a shared concern, two identical ones are.


def resolve_default_site(project_name: str, bench_path: str) -> str:
    """The bench's default site, for a verb called without an explicit ``--site``.

    Raises ``NOT_FOUND`` when the bench has no default site and ``INTERNAL`` when
    the lookup itself fails. The caller records the "using default site" note as
    an envelope warning; this never prints.
    """
    try:
        default_site = db_utils.get_default_site(project_name, bench_path)
    except Exception as e:  # noqa: BLE001 - surface as a typed error, never print
        raise CwcliError(
            ErrorKind.INTERNAL,
            "default_site.error",
            f"Failed to retrieve default site: {e}",
        ) from e

    if not default_site:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "site.no_default",
            "No site specified and no default site found in config.",
        )
    return default_site


def resolve_container_and_bench(
    project_name: str,
    bench: str | None,
    bench_path: str | None,
    *,
    auto_start: bool = False,
) -> tuple[object, str, list[Message]] | Result:
    """The container + bench prologue every bench-scoped verb shares.

    Returns ``(container, bench_path, warnings)``, or a ``NEEDS_CHOICE`` ``Result``
    the caller must return as-is.

    Promoted out of ``core.apps``'s private ``_resolve`` when ``core.bench_ops``
    became its second caller (openspec ``add-axi-bench-exec-verbs``). It is a MOVE,
    not a rewrite: the behaviour is byte-identical and ``core.apps._resolve`` is now
    a one-line delegation. Copying it would have left two prologues to keep in sync,
    and a drifted one resolves a DIFFERENT bench than the verb reports.
    """
    warnings: list[Message] = []

    frappe_container = core_docker.get_frappe_container(project_name)

    state = resolve_container_state(
        project_name, frappe_container, auto_start=auto_start, offer_choice=True
    )
    if state.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=state.choice)

    bench_result = resolve_bench(project_name, bench, bench_path)
    if bench_result is None:
        # No cache to resolve against: fall back to the historical default, matching
        # `run` and the pre-migration `_resolve_bench`'s `or _DEFAULT_BENCH`.
        resolved = DEFAULT_BENCH_PATH
        warnings.append(
            Message(
                "bench.default_used",
                f"No cached bench path found. Using default: {DEFAULT_BENCH_PATH}",
            )
        )
    elif bench_result.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=bench_result.choice)
    else:
        assert bench_result.data is not None  # OK always carries the path
        resolved = bench_result.data
        warnings.extend(bench_result.warnings)

    return frappe_container, resolved, warnings


def validate_site_name(site: str) -> None:
    """Raise ``USAGE`` if a site name is empty or carries shell metacharacters."""
    if not site or not site.strip():
        raise CwcliError(ErrorKind.USAGE, "site.empty", "Site name cannot be empty.")
    if any(char in site for char in _INVALID_CHARS):
        raise CwcliError(
            ErrorKind.USAGE,
            "site.invalid_chars",
            f"Invalid site name '{site}'. Site names cannot contain special shell characters.",
        )


def validate_bench_path(bench_path: str) -> None:
    """Raise ``USAGE`` if a bench path carries shell metacharacters."""
    if any(char in bench_path for char in _INVALID_CHARS):
        raise CwcliError(
            ErrorKind.USAGE,
            "bench_path.invalid_chars",
            f"Invalid bench path '{bench_path}'. Paths cannot contain special shell characters.",
        )


def require_bench_dir(frappe_container, bench_path: str) -> None:
    """Raise ``NOT_FOUND`` unless ``{bench_path}/sites`` exists in the container."""
    exit_code, _ = frappe_container.exec_run(["test", "-d", f"{bench_path}/sites"])
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "bench.dir_missing",
            f"Bench directory not found at {bench_path}",
        )


def require_site_dir(frappe_container, bench_path: str, site: str) -> str:
    """Return ``{bench_path}/sites/{site}``, raising ``NOT_FOUND`` unless it exists."""
    site_path = f"{bench_path}/sites/{site}"
    exit_code, _ = frappe_container.exec_run(["test", "-d", site_path])
    if exit_code != 0:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "site.not_found",
            f"Site '{site}' not found at {site_path}",
        )
    return site_path


# ------------------------------------------------------------------ per-bench ports

# Frappe's own defaults, the values bench's ``make_ports`` counts up from.
WEB_CONTAINER_BASE = 8000
SOCKETIO_CONTAINER_BASE = 9000


def resolve_assigned_ports(
    container, bench_paths: list[str], *, fill_defaults: bool
) -> dict[str, tuple[int, int]]:
    """Each bench's assigned ``(webserver_port, socketio_port)`` from its OWN config.

    Reads ``sites/common_site_config.json`` live inside the container - the source of
    truth, written by bench's ``make_ports``; cwcli writes zero port config. A bench
    whose config is unreadable, unparseable, not a mapping, or carries a non-numeric
    port is SKIPPED (absent from the result), never defaulted, so a caller can tell
    "this bench serves 8000" from "I could not find out".

    ``fill_defaults`` decides the one remaining case - a config that parses as a
    mapping but simply OMITS a port key - and the two callers need opposite answers,
    which is why it is keyword-only with NO default:

    - ``True`` (``core.scale``): fall back to 8000/9000, the same assumption bench
      itself makes. Correct there, because scale is computing which host ports to
      PUBLISH and a bench serving Frappe's default must be covered by the range.
    - ``False`` (``core.status``, ``core.start``): skip the bench. These callers turn
      the answer into a PROBE TARGET, and probing a guessed 8000 is the exact defect
      this resolver was promoted to fix - on any bench past the first it measures a
      different bench's web server. Unresolved is reported as unresolved.

    Promoted out of ``core.scale``'s private ``_read_assigned_ports`` when status and
    start became its second and third callers (the ``set_maintenance`` precedent:
    promote, never copy - three copies of "which port does this bench serve" drift,
    and the day they drift is the day the bug comes back on one of them).
    """
    assigned: dict[str, tuple[int, int]] = {}
    for bench_path in bench_paths:
        config_file = f"{bench_path.rstrip('/')}/sites/common_site_config.json"
        exit_code, output = container.exec_run(["bash", "-lc", f"cat {config_file}"])
        if exit_code != 0:
            continue
        try:
            config = json.loads(_decode(output))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(config, dict):
            continue
        if fill_defaults:
            web = config.get("webserver_port", WEB_CONTAINER_BASE)
            sio = config.get("socketio_port", SOCKETIO_CONTAINER_BASE)
        else:
            web = config.get("webserver_port")
            sio = config.get("socketio_port")
            if web is None or sio is None:
                continue
        try:
            assigned[bench_path] = (int(web), int(sio))
        except (TypeError, ValueError):
            continue
    return assigned


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""
