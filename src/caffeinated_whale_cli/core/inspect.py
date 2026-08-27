"""``core.inspect`` - the 3-tier cache-backed project read, as UI-pure logic.

Batch 7 of the logic-core rework (openspec ``migrate-inspect-core``). ``inspect``
is the cache's PRODUCER: every bench verb resolves through data only this module
writes, and five subsystems invoke it purely to populate that cache. Three
boundary decisions here are deliberate and load-bearing:

- **The core owns the cache write** (design Decision 2). ``inspect_raw`` calls the
  unchanged ``db_utils.cache_project_data`` itself, because the write IS the
  product for most consumers (``recache_project``, ``auto_inspect``, ``update``'s
  mid-fan-out recache, the open/update/restore no-cache fallbacks) and because the
  freshness semantics are inseparable from the write decision: T2's never-write,
  T3's write-on-success, and drift-degrade's serve-without-persist are ONE
  contract that must not be re-implementable per frontend. Secret redaction stays
  inside ``cache_project_data`` (the single chokepoint), never here - redacting at
  this layer would strip in-memory dicts the same run serves.
- **A plain function, NOT a generator, NOT two-phase** (design Decision 1).
  Nothing consumes intermediate results (the spinner is content-free),
  ``NEEDS_CHOICE``/``confirm_start`` must be returnable at call time (a lazy
  generator body cannot return it), nothing streams (every probe is a short
  buffered ``exec_run``, so ``core.exec_stream`` is deliberately not consumed),
  and there is no ``try/finally`` cleanup pressure. The ``--verbose`` trace rides
  the optional typed-event ``on_event`` callback - ``Result.warnings`` is for
  actionable notes, never debug echoes (batch 5's amended decision).
- **T2 is passive by construction.** The cache-hit freshness pass checks the
  container state with ``auto_start=False`` HARDCODED and ``offer_choice=False``,
  so a plain cache-hit read can never prompt for, or wake, a stopped project -
  even when the caller passed ``auto_start=True``. Auto-start and the
  ``confirm_start`` fork belong only to the T3 full-inspect path, which persists
  fully-fresh data.

Two hardenings over the pre-migration command, disclosed rather than smuggled:
the probe decode is ``errors="replace"`` (one non-UTF-8 byte used to crash the
whole inspect), and a raw docker/transport exception escaping the T3 fan-out is
wrapped into ``CwcliError(DOCKER)`` (it used to be an unhandled traceback). The
outcome contract is unchanged: a T3 mid-fan-out connection loss still aborts
WITHOUT writing the cache (crash-without-corruption, now typed), the T2 pass
still degrades to the cached data on any exception, and per-probe failures still
absorb to ``[]``/``None`` without aborting the fan-out.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import requests
from docker.errors import DockerException

from ..utils import bench_labels, bench_sites, config_utils, db_utils
from . import docker as core_docker
from . import resolvers
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

# ------------------------------------------------------------------------------ DTOs


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteInfo:
    """One site on a bench. ``has_site_config`` is presence-as-data: the config
    CONTENT deliberately does not cross this boundary (a live-gathered config can
    carry secrets; the typed report feeds ``axi``'s stdout document).

    ``installed_apps_verified`` is the per-site VERIFIED-or-REMEMBERED token, the
    same fail-honest signal ``core.where`` carries on every ``WhereMatch``. Each
    ``installed_apps`` entry encodes the app's live version and git ref (e.g.
    ``visa 2.0.0 testing``), but only a T3 full inspect actually re-runs
    ``bench list-apps`` to observe it. The cheap T1/T2 tiers carry the CACHED
    list forward, and a ``git checkout`` inside an app touches neither the
    ``apps/`` directory listing nor the site set that T2's drift check watches -
    so a stale ref rides through a ``served_from: partial`` read looking fresh.
    This flag is True ONLY when the ref was live-observed (T3), so a caller can
    tell a verified ref from a remembered one without having to know that
    ``partial`` silently excludes ``installed_apps`` from its freshness pass."""

    name: str
    installed_apps: list[str]
    installed_apps_verified: bool
    has_site_config: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchInfo:
    """One bench's inspected state (serializable, no live objects).

    ``index`` is the durable numeric identity every ``--bench`` selector resolves
    against. It remains attached to the path when discovery order changes.
    ``default_site`` is resolved: ``common_site_config.default_site`` first,
    ``current_site`` (the ``currentsite.txt`` pointer) as the fallback.
    """

    index: int
    path: str
    label: str | None
    current_site: str | None
    default_site: str | None
    available_apps: list[str]
    sites: list[SiteInfo]


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectReport:
    """The typed outcome of a tiered read.

    ``served_from`` names the tier that produced the data: ``"cache"`` (served
    verbatim, no successful freshness pass), ``"partial"`` (the T2 read-only pass
    confirmed no drift), or ``"full"`` (a T3 full inspect gathered and persisted
    fresh data). ``degraded`` is True only when a drift escalation could not
    re-discover any bench and fell back to the cached data without persisting.
    """

    project: str
    served_from: str
    degraded: bool
    benches: list[BenchInfo]


@dataclass(frozen=True, slots=True, kw_only=True)
class RawInspect:
    """The tier machine's outcome over CACHE-SHAPED dicts - the human renderer's
    twin of :class:`InspectReport`.

    Exists because the human CLI must render byte-identical output over exactly
    the dicts the tiers produced: the ``--json`` bytes preserve each path's own
    key order (a gathered bench dict orders ``current_site`` before ``label``;
    a cache-read dict the reverse), the ``-i`` labeling loop mutates these dicts
    and bulk-persists them, and the configs ride along in full. None of that may
    enter :class:`InspectReport`, which is the secrets-free typed surface.
    """

    project: str
    served_from: str
    degraded: bool
    benches: list[dict]


# ------------------------------------------------------------------------ typed events


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectCommand:
    """A container command about to run (the ``-v`` ``$ cmd`` echo)."""

    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectCommandDone:
    """A container command's outcome (the ``-v`` exit-code/output echo)."""

    exit_code: int
    output: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectTrace:
    """A prose diagnostic (the ``-v`` ``VERBOSE:`` lines)."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectWarning:
    """A note the frontend should surface UNCONDITIONALLY (not ``-v``-gated),
    e.g. a site whose ``bench list-apps`` failed and was recorded as ``[]``."""

    text: str


InspectEvent = InspectCommand | InspectCommandDone | InspectTrace | InspectWarning
OnEvent = Callable[[InspectEvent], None]


def _noop(_event: InspectEvent) -> None:
    """Default event sink; keeps every emit unconditional."""


# ----------------------------------------------------------------------- moved helpers


def _run_command(
    container,
    cmd: str,
    emit: OnEvent,
    workdir: str | None = None,
) -> tuple[int, str]:
    emit(InspectCommand(command=cmd))
    exit_code, output = container.exec_run(cmd, workdir=workdir)
    stdout_bytes = output[0] if isinstance(output, tuple) else output
    # errors="replace": disclosed hardening #1 - a non-UTF-8 byte in one probe's
    # output must degrade that value, never crash the whole inspect (the core
    # idiom, cf. core/backup.py:_decode).
    decoded_output = stdout_bytes.decode("utf-8", errors="replace").strip()
    emit(InspectCommandDone(exit_code=exit_code, output=decoded_output))
    return exit_code, decoded_output


def _is_bench_directory(container, path: str, emit: OnEvent) -> bool:
    check_command = (
        f'sh -c "test -d {path}/sites && test -d {path}/apps '
        f'&& test -f {path}/sites/common_site_config.json"'
    )
    exit_code, _ = _run_command(container, check_command, emit)
    return exit_code == 0


def _get_sites(container, bench_dir: str, emit: OnEvent) -> list[str]:
    # A real Frappe site is a DIRECTORY containing site_config.json. Detect sites
    # by that shape via the canonical shared helper, never by denylisting known
    # non-site names - a denylist can never be complete, so a stray entry like
    # currentsite.txt (a plain file written by `bench use`) was being reported as
    # a site and then failed `bench list-apps`. See utils/bench_sites.py.
    emit(InspectCommand(command=f"ls -1 {bench_dir}/sites (site detection)"))
    sites = bench_sites.list_sites(container, bench_dir)
    return sites if sites is not None else []


def _get_installed_apps(container, bench_dir: str, site: str, emit: OnEvent) -> list[str]:
    cmd = f"bench --site {site} list-apps"
    exit_code, output = _run_command(container, cmd, emit, workdir=bench_dir)
    if exit_code != 0:
        # Surface the failure as an event, never into the returned/cached data. A
        # site with genuinely no apps and a site whose list-apps failed both cache
        # as [] (the honest "nothing to record / unknown" state) - never a poisoned
        # sentinel string that would be persisted and re-emitted forever by the
        # partial-refresh path and rendered as a fake app in the tree.
        emit(InspectWarning(text=f"Failed to list apps for site '{site}'."))
        return []
    return [app for app in output.split("\n") if app]


def _get_available_apps(container, bench_dir: str, emit: OnEvent) -> list[str]:
    exit_code, output = _run_command(container, f"ls -1 {bench_dir}/apps", emit)
    if exit_code != 0:
        return []
    return [app for app in output.split("\n") if app]


def discover_benches(container, *, on_event: OnEvent | None = None) -> list[str]:
    """Find all bench directories using the default and custom TOML config paths.

    The ``sorted(set(...))`` return IS the numeric-label / index order every
    ``--bench`` selector resolves against; do not touch it.
    """
    emit = on_event or _noop
    benches_found = []

    # Each root's scan is `find <root> -maxdepth 2 -type d -name 'apps'`, so a root
    # only reaches an apps dir at most two levels below it. That governs which roots
    # are redundant:
    #  - /home/frappe/workspace/development stays: its bench's apps sits at depth 3
    #    from /home/frappe, unreachable from that shallower root.
    #  - /workspace/development stays: the devcontainer bench is /workspace/development/
    #    frappe-bench, so its apps is at depth 2 here but depth 3 from bare /workspace
    #    (verified against the fakes) - bare /workspace does NOT subsume it.
    #  - /workspace is the addition: it catches a hand-made bench at /workspace/<name>
    #    (bench init run directly in a container shell, which cwcli's own init never
    #    registers), whose apps is at depth 2 - the discovery gap this closes.
    default_search_roots = [
        "/home/frappe",
        "/home/frappe/workspace/development",
        "/workspace/development",
        "/workspace",
    ]
    config = config_utils.load_config()
    custom_search_roots = config.get("search_paths", {}).get("custom_bench_paths", [])

    all_search_roots = list(set(default_search_roots + custom_search_roots))

    for root in all_search_roots:
        emit(InspectTrace(text=f"Searching for benches in '{root}'..."))
        # Find directories named 'apps' which are a reliable indicator of a bench's parent.
        find_cmd = f"find {root} -maxdepth 2 -type d -name 'apps'"
        exit_code, output = _run_command(container, find_cmd, emit)
        if exit_code == 0:
            for path in output.strip().split("\n"):
                if path:
                    # The bench dir is the parent of the 'apps' dir
                    bench_dir = path.removesuffix("/apps")
                    if _is_bench_directory(container, bench_dir, emit):
                        benches_found.append(bench_dir)

    # Sort for deterministic first-discovery identity assignment.
    # Numeric identities are persisted separately, so an existing bench keeps its
    # number even when a newly discovered path sorts before it.
    return sorted(set(benches_found))


def _get_common_site_config(frappe_container, bench_dir: str, emit: OnEvent) -> dict | None:
    """Fetches common_site_config.json from the bench directory."""
    config_path = f"{bench_dir}/sites/common_site_config.json"
    cmd = f"cat {config_path}"

    exit_code, output = _run_command(frappe_container, cmd, emit)

    if exit_code == 0 and output:
        try:
            config: dict = json.loads(output)
            emit(InspectTrace(text=f"Found common_site_config with {len(config)} keys"))
            return config
        except json.JSONDecodeError:
            emit(InspectTrace(text="Failed to parse common_site_config.json"))
            return None
    else:
        emit(InspectTrace(text="common_site_config.json not found or not readable"))
        return None


def _get_site_config(
    frappe_container, bench_dir: str, site_name: str, emit: OnEvent
) -> dict | None:
    """Fetches site_config.json for a specific site."""
    config_path = f"{bench_dir}/sites/{site_name}/site_config.json"
    cmd = f"cat {config_path}"

    exit_code, output = _run_command(frappe_container, cmd, emit)

    if exit_code == 0 and output:
        try:
            config: dict = json.loads(output)
            emit(InspectTrace(text=f"Found site_config for {site_name} with {len(config)} keys"))
            return config
        except json.JSONDecodeError:
            emit(InspectTrace(text=f"Failed to parse site_config.json for {site_name}"))
            return None
    else:
        emit(InspectTrace(text=f"site_config.json not found for {site_name}"))
        return None


def _gather_bench_data(frappe_container, bench_dir: str, emit: OnEvent) -> dict:
    """Gathers sites, apps, and configs for a single bench instance."""
    emit(InspectTrace(text=f"Inspecting Bench Instance: {bench_dir}"))

    available_apps = _get_available_apps(frappe_container, bench_dir, emit)

    # Fetch common site config
    common_site_config = _get_common_site_config(frappe_container, bench_dir, emit)

    sites = _get_sites(frappe_container, bench_dir, emit)
    sites_info = []
    for site in sites:
        emit(InspectTrace(text=f"  - Found Site: {site}"))

        installed_apps = _get_installed_apps(frappe_container, bench_dir, site, emit)

        # Fetch site-specific config
        site_config = _get_site_config(frappe_container, bench_dir, site, emit)

        site_data: dict = {"name": site, "installed_apps": installed_apps}
        if site_config is not None:
            site_data["site_config"] = site_config

        sites_info.append(site_data)

    bench_data: dict = {"path": bench_dir, "sites": sites_info, "available_apps": available_apps}

    # Record the default site pointer from currentsite.txt (written by `bench use`).
    # A bench records its default site in TWO places: common_site_config.json's
    # `default_site` OR sites/currentsite.txt. Persisting currentsite.txt lets the
    # cache-served default-site resolution (restore/backup/unlock and the inspect
    # "(default)" marker) work even when common_site_config has no default_site
    # key - the exact shape a plain `bench use`d dev bench has.
    current_site = bench_sites.read_current_site(frappe_container, bench_dir)
    if current_site:
        bench_data["current_site"] = current_site
        emit(InspectTrace(text=f"Default site from currentsite.txt: {current_site}"))

    # Recover the user label from the per-bench marker file. This is what lets a
    # full inspect rebuild labels after the SQLite cache is lost: the marker lives
    # inside the bench, so it survives a cache wipe. The marker is the source of
    # truth for labels; the rest of the bench config is re-derived live as above.
    marker_label = bench_labels.read_label_marker(frappe_container, bench_dir)
    if marker_label:
        bench_data["label"] = marker_label
        emit(InspectTrace(text=f"Recovered label '{marker_label}' from marker"))

    if common_site_config is not None:
        bench_data["common_site_config"] = common_site_config

    return bench_data


def partial_refresh(
    frappe_container,
    cached_bench_instances: list[dict],
    *,
    on_event: OnEvent | None = None,
) -> tuple[list[dict], bool]:
    """Read-only freshness pass (the "T2 partial inspect") over KNOWN bench paths.

    This is a pure drift detector: for each bench path already in the cache it
    cheaply re-reads only the inexpensive, filesystem-level facts via
    ``test``/``ls`` (the bench check, the available-apps list, and the site list).
    It deliberately does NOT:

    - re-discover bench instances (no ``find`` over the search roots); a brand-new
      bench is only picked up by the full inspect,
    - run the deep per-site ``bench list-apps`` (which boots Frappe); the cached
      per-site ``installed_apps`` are carried forward instead, and
    - re-read any config files (``common_site_config.json`` /
      ``site_config.json``); the cached configs are carried forward, so a
      transient unreadable or half-written config can never silently drop the
      cached ``common_site_config`` / ``default_site`` label.

    It never writes the cache. A freshly installed app shows up in ``apps/``
    immediately, so the cheap ``ls apps`` here catches it - which is exactly what
    fixes ``open --app`` and inspect's "Available Apps" without a manual
    ``inspect -u``.

    Returns ``(refreshed_bench_instances, drift)`` where ``drift`` is True when the
    on-disk available-apps or site set diverged from the cache (or a known bench
    vanished). The caller escalates to a full inspect on drift so the per-site
    installed lists (and any brand-new bench) are also brought up to date; on no
    drift the caller serves the cache unchanged without persisting.

    The dicts returned are the CACHE shape, not DTOs: they feed straight back into
    cache and consumer comparisons, and DTO-ifying an internal pass would force a
    convert-back at the cache boundary for zero consumer benefit.
    """
    emit = on_event or _noop
    refreshed: list[dict] = []
    drift = False

    for cached_bench in cached_bench_instances:
        bench_dir = cached_bench["path"]

        # Known bench vanished -> stale cache, force a full re-inspect.
        if not _is_bench_directory(frappe_container, bench_dir, emit):
            emit(InspectTrace(text=f"Cached bench '{bench_dir}' no longer present; marking drift."))
            drift = True
            continue

        fresh_available = _get_available_apps(frappe_container, bench_dir, emit)
        fresh_sites = _get_sites(frappe_container, bench_dir, emit)

        cached_site_names = {s["name"] for s in cached_bench.get("sites", [])}
        if (
            set(fresh_available) != set(cached_bench.get("available_apps", []))
            or set(fresh_sites) != cached_site_names
        ):
            drift = True

        cached_sites_by_name = {s["name"]: s for s in cached_bench.get("sites", [])}
        sites_info: list[dict] = []
        for site in fresh_sites:
            previous = cached_sites_by_name.get(site)
            # Carry the cached per-site installed apps and config forward; a
            # brand-new site has no cached entry, so it stays empty until a full
            # inspect (triggered by the drift this new site causes) populates it.
            site_data: dict = {
                "name": site,
                "installed_apps": list(previous["installed_apps"]) if previous else [],
            }
            if previous is not None and "site_config" in previous:
                site_data["site_config"] = previous["site_config"]
            sites_info.append(site_data)

        bench_data: dict = {
            "path": bench_dir,
            "sites": sites_info,
            "available_apps": fresh_available,
        }
        # Identity is cached addressing metadata, not a live filesystem fact.
        # Carry it forward exactly like the user label so this helper continues
        # to return the cache shape even when list order and identity differ.
        if "index" in cached_bench:
            bench_data["index"] = cached_bench["index"]
        # Carry the cached user label forward. T2 is a cheap freshness pass and does
        # not re-read the marker; the label is preserved so a partial refresh never
        # drops it (a real label change goes through `label`/`inspect -i`, which
        # updates the cache directly).
        if cached_bench.get("label"):
            bench_data["label"] = cached_bench["label"]
        # Carry the cached default-site pointer forward too. T2 is a cheap
        # freshness pass and does not re-read currentsite.txt; a change to the
        # default site is picked up by the full inspect (Tier 3).
        if cached_bench.get("current_site"):
            bench_data["current_site"] = cached_bench["current_site"]
        if "common_site_config" in cached_bench:
            bench_data["common_site_config"] = cached_bench["common_site_config"]
        refreshed.append(bench_data)

    return refreshed, drift


# --------------------------------------------------------------------- the tier machine

_REFRESH_MODES = ("auto", "cache_only", "full")


def _select_bench(benches: list[dict], bench: str | None, project_name: str) -> list[dict]:
    """Narrow a tier's bench list to ONE, via the same selector every other
    bench-scoped verb uses (``bench_labels.resolve_bench``: label, then numeric
    index). ``bench=None`` is a no-op. An unmatched selector raises the same
    ``bench.not_found`` shape ``resolvers.resolve_bench`` raises, so every
    bench-scoped verb fails the same way on a typo'd ``--bench``.
    """
    if bench is None:
        return benches
    chosen = bench_labels.resolve_bench(benches, bench)
    if chosen is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "bench.not_found",
            f"No bench '{bench}' in project '{project_name}'.",
        )
    return [chosen]


def inspect_raw(
    project_name: str,
    *,
    refresh: str = "auto",
    auto_start: bool = False,
    offer_choice: bool = True,
    on_event: OnEvent | None = None,
    bench: str | None = None,
) -> Result[RawInspect]:
    """The T1/T2/T3 tier machine over cache-shaped dicts. See the module docstring.

    ``refresh="cache_only"`` serves the cache verbatim with zero container calls
    (``--no-refresh``); ``refresh="full"`` forces a full re-inspect (``--update``);
    ``refresh="auto"`` runs the tiered read. ``auto_start`` applies to T3 only
    (T2 is passive by construction); with ``offer_choice=False`` a stopped project
    on the T3 path raises ``CwcliError(NOT_RUNNING)`` instead of returning the
    ``confirm_start`` choice (the spinner-borne caller contract).

    ``bench`` narrows the OUTPUT to one bench (``--bench <index|label>``, the same
    selector ``status``/``logs``/``apps`` take): every tier still does its full
    discovery/refresh/cache-write work over EVERY bench (the cache would otherwise
    go stale for the benches not asked about), and only the returned/rendered list
    is narrowed at the end. A selector matching nothing raises ``bench.not_found``.
    """
    if refresh not in _REFRESH_MODES:
        raise CwcliError(
            ErrorKind.USAGE,
            "inspect.bad_refresh",
            f"Unknown refresh mode '{refresh}'. Use one of: {', '.join(_REFRESH_MODES)}.",
        )

    emit = on_event or _noop
    warnings: list[Message] = []

    # Set only when a T2 drift-escalation forces a full inspect while a valid cache
    # exists; if that full inspect then can't discover any bench (e.g. a custom
    # search path was removed), we degrade to this cached data instead of failing.
    drift_fallback_benches: list[dict] | None = None
    benches: list[dict] | None = None
    served_from = "cache"

    cached_data = None
    if refresh != "full" or bench is not None:
        cached_data = db_utils.get_cached_project_data(project_name)

    if refresh != "full":
        if cached_data:
            cached_benches = cached_data["bench_instances"]
            emit(
                InspectTrace(
                    text=(
                        "Found cached data for this project from " f"{cached_data['last_updated']}."
                    )
                )
            )
            if refresh == "cache_only":
                # Tier 1: serve the cache verbatim, no container calls (fastest path).
                emit(InspectTrace(text="Cache-only refresh; serving cached data as-is."))
                benches = cached_benches
            else:
                # Tier 2: a lightweight, read-only freshness pass over the known
                # benches. It only runs when the containers are already up; the
                # run-state check never prompts AND never starts anything
                # (auto_start=False by construction, even when the caller passed
                # auto_start=True), so a cache hit can never block on a
                # "start the containers?" question or disturb a stopped project.
                # Auto-start belongs only to the Tier 3 full inspect below, which
                # persists fully-fresh data. The pass never writes the cache: on no
                # drift the cached data is served unchanged; on drift it falls
                # through to the full inspect (Tier 3). If anything goes wrong it
                # degrades to the cached data rather than failing a
                # previously-working read.
                benches = cached_benches
                # A missing project / no frappe service / unreachable daemon is a
                # hard error even on a cache hit (matching the pre-migration
                # behavior, where the run-state prologue exited); only failures
                # AFTER the container resolves degrade to the cache.
                frappe_container = core_docker.get_frappe_container(project_name)
                try:
                    resolvers.resolve_container_state(
                        project_name,
                        frappe_container,
                        auto_start=False,
                        offer_choice=False,
                    )
                    _refreshed, drift = partial_refresh(
                        frappe_container, cached_benches, on_event=on_event
                    )
                    if drift:
                        # Escalate-on-drift: a full inspect (Tier 3) also refreshes
                        # the deep per-site installed-app lists and any new bench.
                        # Remember the cached benches so the full inspect can
                        # degrade to them rather than hard-failing if the bench is
                        # no longer discoverable (e.g. its search path was removed).
                        emit(
                            InspectTrace(
                                text=(
                                    "Partial inspect detected drift; "
                                    "escalating to a full inspect."
                                )
                            )
                        )
                        benches = None
                        drift_fallback_benches = cached_benches
                    else:
                        emit(
                            InspectTrace(
                                text=(
                                    "Partial inspect found no drift; "
                                    "serving cached data unchanged."
                                )
                            )
                        )
                        served_from = "partial"
                except CwcliError as e:
                    if e.kind is ErrorKind.NOT_RUNNING:
                        emit(
                            InspectTrace(text="Containers not running; serving cached data as-is.")
                        )
                    else:
                        emit(
                            InspectTrace(text=f"Partial inspect failed ({e}); serving cached data.")
                        )
                except Exception as e:  # noqa: BLE001 - degrade, never fail a working read
                    emit(InspectTrace(text=f"Partial inspect failed ({e}); serving cached data."))
        else:
            emit(InspectTrace(text="No cached data found, proceeding with inspect."))
    else:
        emit(InspectTrace(text="Full refresh requested, ignoring cache."))

    if benches is None:
        # Tier 3: the full inspect. The only tier that may auto-start or surface
        # the confirm_start fork - and the fork must be resolvable at CALL time,
        # which is why this is a plain function (design Decision 1).
        frappe_container = core_docker.get_frappe_container(project_name)
        state = resolvers.resolve_container_state(
            project_name,
            frappe_container,
            auto_start=auto_start,
            offer_choice=offer_choice,
        )
        if state.status is Status.NEEDS_CHOICE:
            if bench is not None and cached_data:
                _select_bench(cached_data["bench_instances"], bench, project_name)
            return Result(status=Status.NEEDS_CHOICE, choice=state.choice)
        # OK: running, or start_requested - the actual (UI-coupled) start is the
        # caller's job, performed before/around this call (cf. core.update).

        # Disclosed hardening #2: a raw docker/transport exception escaping the
        # fan-out becomes a typed DOCKER error. The cache is untouched on this
        # path (the write below is only reached on success), so the pre-migration
        # crash-without-corruption contract holds - now typed.
        try:
            bench_paths = discover_benches(frappe_container, on_event=on_event)
            gathered = [
                _gather_bench_data(frappe_container, bench_path, emit) for bench_path in bench_paths
            ]
        except CwcliError:
            raise
        except (DockerException, requests.RequestException) as e:
            raise CwcliError(
                ErrorKind.DOCKER,
                "inspect.fanout_failed",
                f"Docker error while inspecting project '{project_name}'.",
                detail={"output": str(e)},
            ) from e

        if not bench_paths:
            # A drift-escalation that can't rediscover the bench has a valid cache
            # to fall back on; degrade to it (WITHOUT persisting) instead of
            # failing a previously-working read. A full-refresh / cache-miss run
            # has no such fallback, so it keeps the hard error.
            if drift_fallback_benches is not None:
                emit(
                    InspectTrace(
                        text=(
                            "Drift escalation found no discoverable benches; "
                            "serving cached data."
                        )
                    )
                )
                warnings.append(
                    Message(
                        "inspect.degraded",
                        (
                            "Drift was detected but no bench could be re-discovered; "
                            "serving cached data without refreshing it."
                        ),
                    )
                )
                return Result(
                    status=Status.WARNING,
                    data=RawInspect(
                        project=project_name,
                        served_from="cache",
                        degraded=True,
                        benches=_select_bench(drift_fallback_benches, bench, project_name),
                    ),
                    warnings=warnings,
                )
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "bench.none_found",
                f"No Bench Instances found for project '{project_name}'.",
            )

        identities = db_utils.cache_project_data(project_name, gathered)
        # Keep the gathered/cache-write dicts untouched. The human renderer's
        # gathered-vs-cache key order is a characterized contract, and a cache
        # adapter may retain the objects it was handed. Add identity on fresh
        # report copies only.
        benches = [
            {
                **bench,
                "index": (
                    identities[bench["path"]]
                    if identities is not None
                    else bench.get("index", position)
                ),
            }
            for position, bench in enumerate(gathered)
        ]
        # Serve in identity order, matching the cached read (db_utils
        # get_cached_project_data): a list position is a reference a caller can
        # hold, and identity order never shifts an existing row.
        benches.sort(key=lambda bench: bench["index"])
        served_from = "full"

    return Result(
        status=Status.OK,
        data=RawInspect(
            project=project_name,
            served_from=served_from,
            degraded=False,
            benches=_select_bench(benches, bench, project_name),
        ),
        warnings=warnings,
    )


def _to_bench_info(index: int, bench: dict, *, apps_verified: bool) -> BenchInfo:
    common = bench.get("common_site_config") or {}
    # The "(default)" resolution order: common_site_config.default_site first,
    # the currentsite.txt pointer as the fallback (falsy-checked, matching the
    # tree renderer).
    default_site = common.get("default_site") or bench.get("current_site") or None
    return BenchInfo(
        index=index,
        path=bench["path"],
        label=bench.get("label"),
        current_site=bench.get("current_site"),
        default_site=default_site,
        available_apps=list(bench.get("available_apps", [])),
        sites=[
            SiteInfo(
                name=site["name"],
                installed_apps=list(site.get("installed_apps", [])),
                installed_apps_verified=apps_verified,
                has_site_config="site_config" in site,
            )
            for site in bench.get("sites", [])
        ],
    )


def inspect(
    project_name: str,
    *,
    refresh: str = "auto",
    auto_start: bool = False,
    offer_choice: bool = True,
    on_event: OnEvent | None = None,
    bench: str | None = None,
) -> Result[InspectReport]:
    """The tiered project read, returning the typed (secrets-free) report.

    A thin conversion over :func:`inspect_raw`, which owns the tier machine and
    the cache write; see its docstring for the parameter contract, including
    ``bench`` (``--bench <index|label>``, narrowing the returned report to one
    bench). Frontends that only need the side effect (recache) or the typed
    report (``axi``, a GUI) call this; the human CLI renderer calls
    :func:`inspect_raw` for the byte-identical cache-shaped dicts.
    """
    raw = inspect_raw(
        project_name,
        refresh=refresh,
        auto_start=auto_start,
        offer_choice=offer_choice,
        on_event=on_event,
        bench=bench,
    )
    if raw.status is Status.NEEDS_CHOICE:
        return Result(status=Status.NEEDS_CHOICE, choice=raw.choice)

    assert raw.data is not None  # OK/WARNING always carries a RawInspect
    # Only a T3 full inspect re-observes each site's installed apps (and their
    # git refs) live; the T1 cache and T2 partial tiers carry the cached list
    # forward, so their per-site installed_apps are REMEMBERED, not verified.
    apps_verified = raw.data.served_from == "full"
    benches = [
        _to_bench_info(b.get("index", position), b, apps_verified=apps_verified)
        for position, b in enumerate(raw.data.benches)
    ]

    warnings = list(raw.warnings)
    # Name the remedy when a served list carries app refs it never observed live -
    # the same fail-honest nudge core.where gives on its unverified path. Scoped to
    # the case where there is actually a remembered app list to be stale about, so
    # a container-less or app-less read stays quiet.
    if not apps_verified and any(s.installed_apps for b in benches for s in b.sites):
        warnings.append(
            Message(
                "inspect.apps_unverified",
                "installed_apps were served from cache and NOT observed live; an app's "
                "checked-out git ref may be stale. Run 'cwcli inspect "
                f"{project_name} --update' to refresh (or 'cwcli axi inspect "
                f"{project_name} --update' on the agent surface).",
            )
        )

    return Result(
        status=raw.status,
        data=InspectReport(
            project=raw.data.project,
            served_from=raw.data.served_from,
            degraded=raw.data.degraded,
            benches=benches,
        ),
        warnings=warnings,
    )


def resolve_bench_with_fallback(
    project_name: str,
    bench: str | None,
    bench_path: str | None,
    *,
    auto_start: bool = False,
) -> Result[str]:
    """Resolve which bench a command should operate on, populating a cold cache
    via inspect rather than silently guessing ``resolvers.DEFAULT_BENCH_PATH``.

    The ``core.open`` no-cache fallback (:func:`~.open._fallback_populate`),
    generalized so ``backup``/``restore`` share it instead of re-implementing
    it: a fresh or explicitly cleared cache must not dead-end (or silently
    guess a possibly-wrong path) when the real bench list is one ``inspect``
    away. Same abort/degrade contract:

    - a hard ``CwcliError`` from the populate PROPAGATES - the guessed default
      is never used to paper over a real failure;
    - a non-``CwcliError`` exception, or a populate that still resolves
      nothing, degrades to ``resolvers.DEFAULT_BENCH_PATH`` with a
      ``bench.default_used`` warning (the historical behavior).

    Returns ``Result(OK, path)`` or ``Result(NEEDS_CHOICE, ...)`` - never
    ``None`` - so callers no longer need a separate no-cache branch.
    """
    bench_result = resolvers.resolve_bench(project_name, bench, bench_path)
    if bench_result is not None:
        return bench_result

    try:
        inspect(project_name, refresh="auto", auto_start=auto_start, offer_choice=False)
        re_resolved = resolvers.resolve_bench(project_name, bench, None)
    except CwcliError:
        raise
    except Exception:  # noqa: BLE001 - the disclosed degrade residue (core.open's precedent)
        re_resolved = None

    if re_resolved is None:
        return Result(
            status=Status.OK,
            data=resolvers.DEFAULT_BENCH_PATH,
            warnings=[
                Message(
                    "bench.default_used",
                    f"No cached bench path found. Using default: {resolvers.DEFAULT_BENCH_PATH}",
                )
            ],
        )
    return re_resolved
