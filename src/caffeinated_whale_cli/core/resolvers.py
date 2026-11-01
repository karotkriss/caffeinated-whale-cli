"""Pure core resolvers split out of ``commands/utils.py``.

Each reports a decision without prompting or printing: it returns the resolved
value, a ``NEEDS_CHOICE`` :class:`~.envelope.Result`, or raises a typed
:class:`~.errors.CwcliError`. The interactive halves (``questionary`` prompts,
the bench-list print, the decline/non-TTY ``typer.Exit``) live in the thin CLI
wrappers in ``commands/utils.py`` that call these.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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

    This is the RAW read: every row is remembered, none is observed. A read that
    VOUCHES for those rows pairs it with :func:`present_bench_paths` - see that
    function's docstring for the rule and where its boundary sits.
    """
    cached_data = db_utils.get_cached_project_data(project_name)
    return (cached_data or {}).get("bench_instances") or []


# ----------------------------------------------------- cached-bench verification
#
# The cache OUTLIVES the benches it describes, and a removed bench directory is
# invisible to it: nothing prunes the row, so a read served straight from
# `cached_benches` keeps vouching for a bench that is gone, indefinitely and
# identically to a live one. That was caught three times on three different read
# surfaces before it was settled here, at the layer, rather than a fourth time on
# a fourth verb.
#
# THE RULE, stated once: a cached read that VOUCHES for a bench's existence
# verifies it when reality is reachable, and says plainly that it could not when
# reality is not. Never silently either way. The token vocabulary is
# `core.where`'s, deliberately reused rather than re-invented - three states, not
# two, so an unanswerable check can never read as a confirmation.
#
# THE BOUNDARY: this applies to a read that PRESENTS bench rows as an answer
# (`core.label.list_benches`, `core.status`). It deliberately does NOT apply to
# the resolve paths (`resolve_bench`, and its callers in `logs`/`open`/`apps`/
# `bench_ops`), which hand the path straight to a live container op that fails
# honestly within the same command - remembered ADDRESSING validated on use, not
# consumed as evidence. Adding a probe there would buy nothing and cost an exec
# on every bench verb.
#
# Verification NEVER mutates: an absent row is REPORTED, not pruned. Refreshing
# the cache is `cwcli inspect`'s job, and a read command that silently deleted
# cached state would be the same defect pointing the other way (the `core.where`
# rule, same reason).

#: ``state`` tokens for a cached bench row. ``unverified`` means the check could
#: not run and is deliberately distinct from ``present``.
BENCH_PRESENT = "present"
BENCH_ABSENT = "absent"
BENCH_UNVERIFIED = "unverified"

# The bench-shape test (a ``sites`` dir, an ``apps`` dir, and
# ``sites/common_site_config.json``) as a POSIX-sh function over ``"$1"``. It is the
# ONE definition every batched probe shares - this presence check and
# ``core.inspect``'s one-exec discovery and partial refresh - so "is this a bench"
# cannot mean different things to different verbs.
IS_BENCH_SH = (
    'is_bench() { [ -d "$1/sites" ] && [ -d "$1/apps" ] '
    '&& [ -f "$1/sites/common_site_config.json" ]; }'
)

# Every candidate path in ONE exec instead of one exec per bench - so the cost of
# verifying is flat in bench count, the property that made `core.where`'s
# per-invocation Docker listing affordable. `if/then/fi` rather than `&& echo`
# keeps the loop's exit status 0, so a non-zero code means the exec ITSELF failed
# and is never confused with "nothing matched".
_PRESENT_BENCHES_SCRIPT = (
    IS_BENCH_SH + '; for p in "$@"; do if is_bench "$p"; then echo "$p"; fi; done'
)


def present_bench_paths(container, bench_paths: list[str]) -> set[str] | None:
    """Which of ``bench_paths`` still exist as benches inside ``container``.

    Returns ``None`` when the question could not be asked at all (the exec failed,
    the container went away, the daemon is unreachable). Fail-HONEST, never
    fail-open: it must never degrade to an empty set, which the caller would render
    as "every cached bench is gone" - the same wrong answer as the staleness this
    exists to remove, just louder.

    Paths ride as argv positionals (``sh -c <script> sh <path>...``), so a cached
    path is never interpolated into the script text no matter what it contains.
    """
    if not bench_paths:
        return set()
    try:
        exit_code, output = container.exec_run(
            ["sh", "-c", _PRESENT_BENCHES_SCRIPT, "sh", *bench_paths]
        )
    except Exception:  # noqa: BLE001 - any transport failure is "could not ask"
        return None
    if exit_code != 0:
        return None
    return {line for line in _decode(output).split("\n") if line}


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


def bench_cli_flag(project_name: str, bench_path: str) -> str:
    """The `` --bench``/`` --path`` fragment a suggested ``cwcli <verb> <project>`` needs
    to act on ``bench_path`` - empty when :func:`resolve_bench` already lands there
    with no selector (the sole cached bench, or the default with nothing cached).

    A path no cached ``--bench`` value names falls back to `` --path`` rather than
    to no selector, which would act on a DIFFERENT bench.
    """
    benches = cached_benches(project_name)
    if not benches:
        implicit: str | None = DEFAULT_BENCH_PATH
    else:
        implicit = benches[0].get("path") if len(benches) == 1 else None
    if bench_path == implicit:
        return ""
    for position, cached in enumerate(benches):
        if cached.get("path") == bench_path:
            return f" --bench {cached.get('label') or cached.get('index', position)}"
    return f" --path {shlex.quote(bench_path)}"


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
    options = [
        {"value": str(b.get("index", position)), "label": _bench_option_label(b)}
        for position, b in enumerate(benches)
    ]
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


def require_sole_site(project_name: str, bench_path: str) -> str:
    """The single site of a single-site bench, else a typed error that lists sites.

    Used by a MUTATING verb (backup/unlock/migrate/restore) that was called without
    ``--site`` and whose bench has no configured default site - the shape a fresh
    ``cwcli init`` leaves (one site, no ``bench use``, so no ``default_site`` /
    ``currentsite.txt``). A single-site bench resolves unambiguously to its sole
    site. A multi-site bench REFUSES (guessing one destructive target by sort order
    is unsafe, unlike :func:`resolve_representative_site` which a READ verb can
    afford), but the error LISTS the sites so the caller can pick one, and carries
    no misleading ``cwcli inspect`` tip (inspect only refreshes the cache; it never
    creates a default site).
    """
    try:
        sites = sorted(db_utils.get_all_site_configs(project_name, bench_path))
    except Exception:  # noqa: BLE001 - a cache read must never crash the caller
        sites = []
    if len(sites) == 1:
        return sites[0]
    if not sites:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "site.no_default",
            "No site specified and no default site found in config.",
            hint="Specify the site explicitly with --site <site>.",
        )
    raise CwcliError(
        ErrorKind.NOT_FOUND,
        "site.ambiguous",
        f"No site specified and project '{project_name}' has multiple sites; "
        "specify one with --site.",
        hint="Available sites: " + ", ".join(sites),
    )


def resolve_sole_or_require_site(project_name: str, bench_path: str) -> str:
    """The site a mutating verb acts on when ``--site`` was omitted.

    The bench's configured default when one exists (:func:`resolve_default_site`);
    otherwise the sole site of a single-site bench, else a typed refusal that lists
    the sites (:func:`require_sole_site`). This gives backup/unlock/migrate the same
    single-site fallback the read verbs get from :func:`resolve_representative_site`,
    WITHOUT its unsafe first-of-many pick on a multi-site bench. A ``default_site``
    lookup that itself errors (not merely "no default") still propagates.
    """
    try:
        return resolve_default_site(project_name, bench_path)
    except CwcliError as e:
        if e.code != "site.no_default":
            raise
    return require_sole_site(project_name, bench_path)


def resolve_representative_site(project_name: str, bench_path: str) -> str | None:
    """A site that stands for this bench when one is needed but none was named.

    Frappe is multi-tenant and routes by ``Host``, so "is this bench serving?" and
    "what is this bench's address?" both need a SITE, not just a port - a request
    naming none is answered 404 by a perfectly healthy bench. Neither question is
    about a particular site, so neither can require one from the caller, but a
    hint or a health field that names no site is not an answer either.

    Order: the bench's own default site (:func:`resolve_default_site` -
    ``default_site`` or ``currentsite.txt``); else its cached sites, the sole one
    when there is only one; else the first in stable sorted order; else None. The
    fallback matters, and is not a corner case: a bench ``cwcli init`` creates has
    exactly one site and records NO default (nothing runs ``bench use``), which is
    the shape of most instances, so a default-site-only lookup answers None on
    precisely the benches that most need an answer.

    Choosing among several undefaulted sites is a pick, not a deduction, so every
    consumer reports WHICH site it used (``BenchStatus.web_site``, the URL itself)
    rather than presenting the result as sitewide. Returns None when the bench has
    no site at all - reported as unknown, never guessed.
    """
    try:
        return resolve_default_site(project_name, bench_path)
    except CwcliError:
        pass
    try:
        sites = sorted(db_utils.get_all_site_configs(project_name, bench_path))
    except Exception:  # noqa: BLE001 - a cache read must never fail the caller
        return None
    return sites[0] if sites else None


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

# The bench's Procfile ``web:`` line, and the port it names: ``bench serve --port N``
# (what bench writes while ``webserver_port`` is set) or a gunicorn ``-b <host>:N``.
_WEB_LINE_RE = re.compile(r"^[ \t]*web[ \t]*:(.*)$", re.MULTILINE)
_PORT_FLAG_RE = re.compile(r"--port[=\s]+(\d+)")
_BIND_FLAG_RE = re.compile(r"(?:--bind|-b)[=\s]+\S*:(\d+)")

# Exit 0 when every listener on the port belongs to a process running in the bench (or
# nothing listens yet); 1 when any listener is held by another process, or by one
# whose ownership cannot be read.
_FOREIGN_LISTENER_SCRIPT = r"""
import os
import sys

port = int(sys.argv[1])
bench = os.path.realpath(sys.argv[2])
listeners = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(table, encoding="ascii") as rows:
            next(rows)
            for row in rows:
                fields = row.split()
                if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                    listeners.add(f"socket:[{fields[9]}]")
    except OSError:
        pass
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        cwd = os.path.realpath(f"/proc/{pid}/cwd")
        if os.path.commonpath((cwd, bench)) != bench:
            continue
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                listeners.discard(os.readlink(f"/proc/{pid}/fd/{fd}"))
            except OSError:
                pass
    except (OSError, ValueError):
        continue
raise SystemExit(1 if listeners else 0)
""".strip()


def _read_json_file(container, path: str) -> dict | None:
    """A JSON object file read inside the container, or None if unreadable or not one."""
    exit_code, output = container.exec_run(["cat", path])
    if exit_code != 0:
        return None
    try:
        data = json.loads(_decode(output))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _common_site_config(container, bench_path: str) -> dict | None:
    return _read_json_file(container, f"{bench_path.rstrip('/')}/sites/common_site_config.json")


def _procfile_web_port(container, bench_path: str) -> int | None:
    """The port the bench's Procfile ``web:`` line binds, or None when it has none.

    The Procfile is what runs: cwcli's supervisord launcher and honcho both exec its
    ``web:`` line. bench writes ``bench serve --port <webserver_port>`` there at init,
    so the line still names the bench's port after an administrator removes the key.
    A line written without the key names no port, and ``bench serve`` and gunicorn
    then bind their own default, 8000 - which a sibling bench may already hold, so
    8000 is accepted only while no process outside this bench listens on it
    (:func:`_no_foreign_listener`); otherwise the port is unknown.
    """
    exit_code, output = container.exec_run(["cat", f"{bench_path.rstrip('/')}/Procfile"])
    if exit_code != 0:
        return None
    line = _WEB_LINE_RE.search(_decode(output))
    if line is None:
        return None
    command = line.group(1)
    named = _PORT_FLAG_RE.search(command) or _BIND_FLAG_RE.search(command)
    if named is not None:
        return int(named.group(1))
    if any(server in command for server in ("bench serve", "frappe serve", "gunicorn")):
        if _no_foreign_listener(container, bench_path, WEB_CONTAINER_BASE):
            return WEB_CONTAINER_BASE
    return None


def _no_foreign_listener(container, bench_path: str, port: int) -> bool:
    """Whether ``port`` is free of listeners held by processes outside this bench.

    A bench whose web line names no port binds 8000, but on a multi-bench instance a
    sibling bench may already hold it; probing 8000 then measures that sibling. True
    when this bench's own process holds the listener or nothing listens yet (a probe
    then honestly finds nothing); False when another process holds it or its owner
    cannot be read.
    """
    exit_code, _output = container.exec_run(
        ["python3", "-c", _FOREIGN_LISTENER_SCRIPT, str(port), bench_path.rstrip("/")]
    )
    return bool(exit_code == 0)


def _web_port_value(container, bench_path: str, config: dict):
    """``webserver_port`` as written, else the Procfile's port; None when neither says."""
    configured = config.get("webserver_port")
    if configured is not None:
        return configured
    return _procfile_web_port(container, bench_path)


def _web_port(container, bench_path: str, config: dict) -> int | None:
    """:func:`_web_port_value` as a number; None when unknown or not numeric."""
    try:
        value = _web_port_value(container, bench_path, config)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def resolve_web_ports(container, bench_paths: list[str]) -> dict[str, int]:
    """Each bench's CONTAINER web port: where its web process listens.

    Resolution order, each step evidence rather than a guess:

    1. ``webserver_port`` in the bench's ``sites/common_site_config.json`` - the port
       bench's ``make_ports`` assigned and wrote there.
    2. The port the bench's Procfile ``web:`` line binds (see
       :func:`_procfile_web_port`); a line naming no port binds 8000, accepted only
       while no process outside this bench listens there. An administrator may remove ``webserver_port``
       on purpose: it is the only way to stop Frappe appending the internal port to
       the URLs it builds (``frappe.utils.get_url``) on a bench served by its
       hostname through a reverse proxy, and such a bench runs fully without it.

    A bench whose config is unreadable, unparseable, or not a mapping, or that names
    a non-numeric port, or whose Procfile has no web line, is SKIPPED (absent from
    the result) - never defaulted, so a caller can tell "this bench serves 8000"
    from "I could not find out". A guessed 8000 measures a sibling bench's server on
    any bench past the first.
    """
    ports: dict[str, int] = {}
    for bench_path in bench_paths:
        config = _common_site_config(container, bench_path)
        web = _web_port(container, bench_path, config) if config is not None else None
        if web is not None:
            ports[bench_path] = web
    return ports


def resolve_assigned_ports(
    container, bench_paths: list[str], *, fill_defaults: bool
) -> dict[str, tuple[int, int]]:
    """Each bench's ``(web, socket.io)`` container ports, for callers that need both.

    The web half is :func:`resolve_web_ports`'s answer; the socket.io half is the
    config's ``socketio_port``. Unreadable configs and non-numeric ports are SKIPPED,
    as there.

    ``fill_defaults`` decides what is left - a web port neither the config nor the
    Procfile names, or an omitted ``socketio_port`` - and the two callers need
    opposite answers, which is why it is keyword-only with NO default:

    - ``True`` (``core.scale``): fall back to 8000/9000, Frappe's own defaults.
      Scale is computing which host ports to PUBLISH, so a bench serving a default
      must be covered by the range.
    - ``False`` (``resolve_host_ports``, a reverse proxy's forwarding target): skip
      the bench. Forwarding a site to a guessed port is the defect this resolver
      exists to prevent.
    """
    assigned: dict[str, tuple[int, int]] = {}
    for bench_path in bench_paths:
        config = _common_site_config(container, bench_path)
        if config is None:
            continue
        web = _web_port_value(container, bench_path, config)
        sio = config.get("socketio_port")
        if fill_defaults:
            web = WEB_CONTAINER_BASE if web is None else web
            sio = SOCKETIO_CONTAINER_BASE if sio is None else sio
        if web is None or sio is None:
            continue
        try:
            assigned[bench_path] = (int(web), int(sio))
        except (TypeError, ValueError):
            continue
    return assigned


def _is_localhost(host: str) -> bool:
    """``localhost``, a ``*.localhost`` name, or a loopback IP: an address on this machine.

    Browsers resolve every ``*.localhost`` name to loopback (RFC 6761), so such a site
    is reached on the host through the instance's published port.
    """
    name = host.strip().lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteAddress:
    """The address a person opens to reach a site, and where it came from.

    ``url`` is None only when a localhost address needs a host port and no host
    binding publishes it. ``source`` is ``host_name`` (the site's own declared address)
    or ``site`` (the site's name). ``internal_port`` is True when Frappe appended the
    bench's ``webserver_port`` to an address whose name is not a localhost one - what
    Frappe reports, but an address that fails when the site is served by its hostname
    through a reverse proxy. ``webserver_port_in`` names the bench-relative config
    files that set ``webserver_port``. ``web_port`` is the bench's CONTAINER web port
    (:func:`resolve_web_ports`'s answer, read in the same pass), where a probe of the
    site goes; None when it cannot be determined.
    """

    url: str | None
    source: str
    internal_port: bool
    webserver_port_in: list[str]
    web_port: int | None


def _site_protocol(conf: dict, site: str) -> str:
    """``https://`` when Frappe would serve ``site`` over TLS, as ``get_url`` decides it."""
    if conf.get("ssl_certificate"):
        return "https://"
    wildcard = conf.get("wildcard")
    if isinstance(wildcard, dict):
        domain = wildcard.get("domain")
        if domain and site.endswith(domain) and wildcard.get("ssl_certificate"):
            return "https://"
    return "http://"


def resolve_site_address(container, bench_path: str, *, site: str | None) -> SiteAddress:
    """The address a person opens to reach ``site``, built the way Frappe builds it.

    Frappe's ``frappe.utils.get_url`` (behind ``bench browse`` and every URL Frappe
    builds, OAuth/SSO redirect URLs included), read from the site's merged config
    (``sites/common_site_config.json`` overridden by ``sites/<site>/site_config.json``):

    1. The base is ``host_name`` (or ``hostname``), else the site name with
       ``https://`` when ``ssl_certificate`` (or a matching ``wildcard``) is set and
       ``http://`` otherwise, else ``http://localhost`` when there is no site.
       ``http://`` is added to a base that names no scheme.
    2. When the base names no port and neither ``restart_supervisor_on_update`` nor
       ``restart_systemd_on_update`` is set, ``:<http_port or webserver_port>`` is
       appended when one is set. The port is never stripped: the address is what
       Frappe reports, and removing ``webserver_port`` is the administrator's fix, so
       an appended ``webserver_port`` on a name that is not a localhost one is
       flagged ``internal_port``.

    A ``localhost``/``*.localhost`` address is always opened through the instance's
    published port, because that is the only way the host reaches it. An address
    with no port, or naming the bench's own CONTAINER web port (what Frappe appends
    from ``webserver_port``), gets the HOST port Docker publishes for that web port (a
    ``--port 21000`` instance publishes container 8001 as 21001). An address already
    naming one of the instance's published HOST ports
    (``host_name: http://dev.localhost:21001``) is kept as written. Any other port is
    ``url=None`` - never a guess. The bench's own web port is decided first, so an
    instance whose host range overlaps its container range (``--port 8001``
    publishes container 8000 as 8001 and 8001 as 8002) never reads a host port as
    another bench's container port.
    """
    bench = bench_path.rstrip("/")
    common = _common_site_config(container, bench)
    web_port = _web_port(container, bench, common) if common is not None else None
    common = common or {}
    own = (
        (_read_json_file(container, f"{bench}/sites/{site}/site_config.json") or {}) if site else {}
    )
    conf = {**common, **own}
    files = [("sites/common_site_config.json", common)]
    if site:
        files.append((f"sites/{site}/site_config.json", own))
    set_in = [name for name, config in files if config.get("webserver_port")]

    declared = conf.get("host_name") or conf.get("hostname")
    if declared:
        url, source = str(declared), "host_name"
    elif site:
        url, source = _site_protocol(conf, site) + site, "site"
    else:
        url, source = "http://localhost", "site"
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    appended = None
    restarts = conf.get("restart_supervisor_on_update") or conf.get("restart_systemd_on_update")
    if len(url.split(":")) <= 2 and not restarts:
        appended = conf.get("http_port") or conf.get("webserver_port")
        if appended:
            url = f"{url}:{appended}"

    def address(url: str | None, internal_port: bool = False) -> SiteAddress:
        return SiteAddress(
            url=url,
            source=source,
            internal_port=internal_port,
            webserver_port_in=set_in,
            web_port=web_port,
        )

    parts = urlsplit(url)
    if not _is_localhost(parts.hostname or ""):
        return address(url, internal_port=bool(appended) and not conf.get("http_port"))
    try:
        named = parts.port
    except ValueError:
        return address(None)
    own_host_port = published_host_port(container, web_port) if web_port is not None else None
    if named is None or named == web_port:
        if own_host_port is None:
            return address(None)
        netloc = parts.netloc.rsplit(":", 1)[0] if named is not None else parts.netloc
        return address(urlunsplit(parts._replace(netloc=f"{netloc}:{own_host_port}")))
    if named == own_host_port or _is_published_host_port(container, named):
        return address(url)
    return address(None)


def resolve_host_ports(container, bench_path: str) -> tuple[int, int] | None:
    """The HOST ports ``(web, socket.io)`` this bench is published on, or None.

    Two hops for each of the bench's ports - its container port
    (:func:`resolve_assigned_ports`), then the host port Docker publishes it on
    (:func:`published_host_port`): a reverse proxy in front of the bench needs the
    socket.io port as well as the web port. Either one unresolved is None, never a
    guess.
    """
    assigned = resolve_assigned_ports(container, [bench_path], fill_defaults=False).get(bench_path)
    if assigned is None:
        return None
    web, socketio = (published_host_port(container, port) for port in assigned)
    return (web, socketio) if web and socketio else None


def _is_published_host_port(container, port: int | None) -> bool:
    """Whether ``port`` is a HOST port one of the container's live bindings publishes."""
    bindings = getattr(container, "ports", None) or {}
    return port is not None and any(
        str((entry or {}).get("HostPort")) == str(port)
        for entries in bindings.values()
        for entry in entries or []
    )


def published_host_port(container, container_port: int) -> int | None:
    """The host port ``container_port/tcp`` is published on, from the live bindings.

    Docker's binding table IS the published mapping, so nothing is re-derived from
    the compose file and nothing can drift out of step with what is bound.
    """
    bindings = getattr(container, "ports", None) or {}
    entries = bindings.get(f"{container_port}/tcp") or []
    for entry in entries:
        host_port = (entry or {}).get("HostPort")
        if host_port:
            try:
                return int(host_port)
            except (TypeError, ValueError):
                continue
    return None


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


# --------------------------------------------------- which copy of an app is live
#
# Every apps verb operates on the literal path `<bench>/apps/<app>` and reports
# `ok` from that copy's git exit code alone. Two real layouts make that a
# WRONG-RESULT: `apps/<app>` is a symlink to a source dir outside the bench (a
# `.hdsrc`-style deploy), or `apps/<app>` is a real dir while the bench venv
# imports a SEPARATE copy (a hand-installed app). In both, an `apps update` /
# `apps checkout` can succeed while the running site keeps executing stale code.
#
# The one honest answer is Python's own: ask the bench virtualenv where it imports
# the app from, and compare that dir (symlinks resolved on BOTH sides) against the
# dir cwcli operated on. The normal symlink layout - import path and `apps/<app>`
# resolve to the same real dir - is deliberately NOT flagged. One exec, and a
# failure of the probe itself is reported as `checked=False`, never a crash or a
# blocked update.


@dataclass(frozen=True, slots=True, kw_only=True)
class AppImport:
    """Where an app physically lives vs where the bench actually imports it from."""

    app: str
    entry: str  # {bench}/apps/{app} - the literal path every apps verb operates on
    is_symlink: bool  # apps/<app> is a symlink (its real target is resolved_path)
    resolved_path: str | None  # realpath of apps/<app> - the copy cwcli operated on
    imported_path: str | None  # repo dir the bench venv imports <app> from
    diverged: bool  # True iff the imported copy differs from the operated-on copy
    checked: bool  # False => the probe could not run at all (a warning, not a fact)


# Emits one JSON list of raw facts per app; the comparison stays in Python so it is
# unit-testable without a container. `find_spec` LOCATES the module (respecting the
# venv's editable-install finders) without importing it, so no app code or DB
# connection runs. Run by the bench venv's own python, so "what Python imports" is
# the bench runtime's answer, not a directory-name guess.
_APP_IMPORT_PROBE = (
    "import json,os,sys,importlib.util\n"
    "bench=sys.argv[1]\n"
    "out=[]\n"
    "for app in sys.argv[2:]:\n"
    "    entry=bench.rstrip('/')+'/apps/'+app\n"
    "    rec={'app':app,'entry':entry,'is_symlink':False,"
    "'resolved_path':None,'imported_origin':None}\n"
    "    try:\n"
    "        rec['is_symlink']=os.path.islink(entry)\n"
    "    except OSError:\n"
    "        pass\n"
    "    try:\n"
    "        if os.path.exists(entry):\n"
    "            rec['resolved_path']=os.path.realpath(entry)\n"
    "    except OSError:\n"
    "        pass\n"
    "    try:\n"
    "        spec=importlib.util.find_spec(app)\n"
    "    except Exception:\n"
    "        spec=None\n"
    "    origin=getattr(spec,'origin',None) if spec is not None else None\n"
    "    if origin and origin not in ('built-in','frozen'):\n"
    "        try:\n"
    "            rec['imported_origin']=os.path.realpath(origin)\n"
    "        except OSError:\n"
    "            rec['imported_origin']=origin\n"
    "    out.append(rec)\n"
    "sys.stdout.write(json.dumps(out))\n"
)


def _is_under(child: str, parent: str) -> bool:
    """Is ``child`` the same path as, or nested inside, ``parent``? (real paths)."""
    parent = parent.rstrip("/")
    return child == parent or child.startswith(parent + "/")


def resolve_app_imports(container, bench_path: str, apps) -> dict[str, AppImport]:
    """For each app, resolve ``apps/<app>``'s real dir + symlink flag and the repo
    the bench venv actually imports it from, in ONE exec.

    Returns a mapping app -> :class:`AppImport`. Fails soft: if the probe itself
    cannot run (no bench venv python, an exec error, unparseable output), every app
    comes back ``checked=False`` with no divergence, so a caller never crashes or
    blocks on it. An app whose import cannot be located is ``checked=True`` but not
    ``diverged`` - "could not tell" is never "wrong copy".
    """
    names = list(dict.fromkeys(a for a in apps if a))
    if not names:
        return {}

    bench = bench_path.rstrip("/")
    venv_python = f"{bench}/env/bin/python"
    try:
        exit_code, output = container.exec_run(
            [venv_python, "-c", _APP_IMPORT_PROBE, bench, *names]
        )
    except Exception:  # noqa: BLE001 - any transport failure is "could not check"
        return app_imports_from_probe(bench_path, names, None)
    if exit_code != 0:
        return app_imports_from_probe(bench_path, names, None)
    return app_imports_from_probe(bench_path, names, _decode(output))


def app_imports_from_probe(bench_path: str, apps, output: str | None) -> dict[str, AppImport]:
    """Interpret :data:`_APP_IMPORT_PROBE` output for ``apps``.

    ``output=None`` means the probe could not run, so every app is ``checked=False``.
    Shared by :func:`resolve_app_imports` and ``core.bench_read``'s batched read,
    which runs the same probe inside its per-bench process, so the comparison rules
    cannot drift between them.
    """
    names = list(dict.fromkeys(a for a in apps if a))
    bench = bench_path.rstrip("/")
    unchecked = {
        name: AppImport(
            app=name,
            entry=f"{bench}/apps/{name}",
            is_symlink=False,
            resolved_path=None,
            imported_path=None,
            diverged=False,
            checked=False,
        )
        for name in names
    }
    if output is None:
        return unchecked
    try:
        records = json.loads(output)
    except (json.JSONDecodeError, ValueError, TypeError):
        return unchecked
    if not isinstance(records, list):
        return unchecked

    resolved = dict(unchecked)
    for rec in records:
        if not isinstance(rec, dict):
            continue
        app = rec.get("app")
        if not isinstance(app, str) or app not in resolved:
            continue
        operated = rec.get("resolved_path")
        operated = operated if isinstance(operated, str) and operated else None
        origin = rec.get("imported_origin")
        origin = origin if isinstance(origin, str) and origin else None
        imported_repo: str | None = None
        diverged = False
        if origin is not None:
            imported_repo = os.path.dirname(os.path.dirname(origin))
            if operated is not None:
                diverged = not _is_under(origin, operated)
        entry = rec.get("entry")
        resolved[app] = AppImport(
            app=app,
            entry=entry if isinstance(entry, str) else f"{bench}/apps/{app}",
            is_symlink=bool(rec.get("is_symlink")),
            resolved_path=operated,
            imported_path=imported_repo,
            diverged=diverged,
            checked=True,
        )
    return resolved


def app_import_divergence_warnings(imports) -> list[Message]:
    """A :class:`Message` per diverged app, naming BOTH copies. Shared by every verb
    that mutates an app's code so the wording never drifts between them."""
    messages: list[Message] = []
    for imp in imports:
        if not imp.diverged:
            continue
        messages.append(
            Message(
                "app.wrong_copy",
                f"App '{imp.app}': cwcli operated on {imp.resolved_path}, but the bench "
                f"imports it from {imp.imported_path}. The running site keeps executing the "
                "other copy, so this change did NOT reach it.",
                detail={
                    "app": imp.app,
                    "operated_on": imp.resolved_path,
                    "imported": imp.imported_path,
                },
            )
        )
    return messages
