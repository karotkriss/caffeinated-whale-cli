"""Maintenance mode the way Frappe answers it, and taking it off as one tracked step list.

Frappe decides "in maintenance" per REQUEST. ``frappe.app.init_request`` loads the
site's config - ``sites/common_site_config.json`` with ``sites/<site>/site_config.json``
laid over it (``frappe.get_site_config``) - and raises ``SessionStopped`` (HTTP 503)
while ``maintenance_mode`` is truthy. Three facts decide what a request is answered
with, and this module models all three rather than reading one file:

1. **The merge order.** A site's own key wins; a site without one inherits the
   bench-wide value. ``bench update`` writes ``maintenance_mode`` and
   ``pause_scheduler`` bench-wide for its whole run and clears them only when the
   whole run succeeds (no ``finally``), so a failed or killed ``bench update`` holds
   down every site that does not override them.
2. **Which site the web server answers AS.** ``bench serve`` (the Procfile ``web:``
   line cwcli's supervisor runs) pins every request to ONE site when it starts with
   one: its ``--site``, else the bench's default site (``default_site`` in
   common_site_config on v15+, ``sites/currentsite.txt`` on v14). ``frappe.app``'s
   ``_site`` then wins over the ``Host`` header, so every site on the bench is
   answered with the default site's config. That is how a site whose own file says
   ``maintenance_mode: 0`` still answers SessionStopped: it is served as the default
   site, which inherits the bench-wide 1.
3. **Frappe v16 caches the merged config** per web process for up to 60 seconds
   (``frappe.config._cached_get_site_config``, ``site_cache(ttl=60)``), so a cleared
   flag can still be answered for up to a minute. The clear waits for the sites to
   answer rather than restarting the web server: the cache expires on its own and a
   restart would drop every healthy site on the bench too.

A live HTTP probe (``/api/method/ping`` with each site's ``Host``) is the final word
wherever a caller can afford requests; the file model explains WHY.

Origin. cwcli records the maintenance it turns on (``bench_ops.set_maintenance``
with an ``owner``, one file per site under ``logs/.cwcli-maintenance/``), so a flag
cwcli was killed before clearing names the operation and whether it still runs. A
flag with no record was set outside cwcli: it is reported neutrally, never assumed
to be a leftover.

Clearing is refused while anything that sets maintenance mode still runs on the
bench: the recorded owner's process, an in-container ``bench migrate``/``update``/
``restore`` (found by its working directory under the bench), or a held migrate
lock. Clearing behind a live migrate would let users write to a half-migrated site.
"""

from __future__ import annotations

import json
import re
import shlex
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..utils import daemon_identity
from . import bench_ops, resolvers, supervision
from .envelope import Choice, Message, Result, Status
from .errors import CwcliError, ErrorKind

SENTINEL = "@@CWCLI-MAINTENANCE@@"

#: How long the clear waits for every site to answer again: Frappe v16 answers a
#: site from a per-process config cache for up to 60 seconds after the write.
VERIFY_TIMEOUT_S = 90.0
_VERIFY_INTERVAL_S = 2.0

# Frappe CLI subcommands that put sites into maintenance mode or rewrite their
# database underneath them. While one runs on the bench, maintenance mode is not
# a leftover and clearing it would expose a site mid-change.
_BUSY_SUBCOMMANDS = frozenset({"migrate", "update", "restore", "partial-restore", "reinstall"})

_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"](\d+)", re.MULTILINE)

# One exec: every config file the decision needs, plus every process whose working
# directory is inside the bench (the busy check). Plain ``sh`` and ``cat``: a Python
# start-up would cost more than the read itself. Each block opens with a
# ``SENTINEL kind name`` line; a file that exists but cannot be read says so, so
# "could not tell" never reads as "not set". It only READS.
_READ_SCRIPT = r"""
b="$1"; m="$2"; r="$3"
cd "$b" 2>/dev/null || exit 3
emit() { printf '\n%s %s %s\n' "$m" "$1" "$2"; }
put() { if [ -e "$1" ]; then cat "$1" 2>/dev/null || emit unreadable -; else emit missing -; fi; }
emit common -; put sites/common_site_config.json
for f in sites/*/site_config.json; do
  [ -f "$f" ] || continue; s="${f#sites/}"; emit site "${s%/site_config.json}"; put "$f"
done
emit procfile -; cat Procfile 2>/dev/null
emit currentsite -; cat sites/currentsite.txt 2>/dev/null
emit version -; grep -m1 '^__version__' apps/frappe/frappe/__init__.py 2>/dev/null
for f in "$r"/*.json; do [ -f "$f" ] || continue; s="${f##*/}"; emit owner "${s%.json}"; put "$f"; done
real=$(pwd -P)
for d in /proc/[0-9]*; do
  c=$(readlink "$d/cwd" 2>/dev/null) || continue
  case "$c" in "$real"|"$real"/*) emit proc "${d##*/}"; tr '\0' ' ' < "$d/cmdline" 2>/dev/null;; esac
done
exit 0
""".strip()

# The only config keys the decision reads. Everything else (database names and
# passwords) is dropped as soon as a file is parsed.
_KEYS = ("maintenance_mode", "pause_scheduler", "default_site")

# One exec: each named site's answer to Frappe's ping, with whether the body is the
# SessionStopped error maintenance mode raises. ``$1`` is the port, the rest sites.
_PROBE_SCRIPT = (
    'port="$1"; shift; for s in "$@"; do '
    'r=$(curl -s --max-time 5 -H "Host: $s" -w " %{http_code}" '
    '"http://localhost:$port/api/method/ping" 2>/dev/null) || r="${r:-} 000"; '
    'code="${r##* }"; stopped=0; '
    'case "$r" in *SessionStopped*) stopped=1;; esac; '
    'echo "$s $code $stopped"; done'
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteMaintenance:
    """One site that is in maintenance mode, or whose state could not be read.

    ``on`` is what Frappe answers the site with (True: 503 SessionStopped), or None
    when its config could not be read. ``cause`` says which write holds it there:
    ``"site"`` (its own ``site_config.json``), ``"bench"`` (inherited from
    ``common_site_config.json``), ``"served_as"`` (the web server answers it as
    ``served_as``, which is in maintenance), or ``"cached"`` (no file sets it any
    more; the web server is still answering from Frappe's config cache).
    ``http_code``/``session_stopped`` are the live probe's answer when one ran.
    """

    site: str
    on: bool | None
    cause: str | None = None
    served_as: str | None = None
    http_code: str | None = None
    session_stopped: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceOwner:
    """The cwcli operation that recorded turning ``site``'s maintenance mode on.

    ``running`` is True while that process is alive, False once it is gone (the
    flag is its leftover), None when it ran on another machine and cannot be checked.
    """

    site: str
    verb: str
    job_id: str | None = None
    at: str | None = None
    running: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchMaintenance:
    """Maintenance mode across ONE bench: every site, both levels, why, and the way out.

    ``sites`` lists only the sites in maintenance (or unreadable); a site absent from
    it answers normally. ``down`` is their names. ``bench_flags`` are the
    ``BENCH_WIDE_FLAGS`` set in common_site_config. ``served_as`` names the site every
    request is answered as (``bench serve`` pinned to the default site), else None.
    ``owners`` are cwcli's records for the sites in scope; empty means the flags were
    set outside cwcli. ``busy`` explains, in plain words, what still runs that sets
    maintenance mode; clearing is refused while it is non-empty. ``command`` is the
    cwcli line that takes the bench out of maintenance.
    """

    bench_path: str
    bench_flags: list[str]
    sites: list[SiteMaintenance]
    down: list[str]
    served_as: str | None = None
    owners: list[MaintenanceOwner] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)
    probed: bool = False
    command: str = ""
    #: :func:`describe` and :func:`prompt_text` for this report, so every frontend
    #: (the web UI above all) says what the CLI says.
    summary: str = ""
    question: str = ""

    @property
    def needs_clearing(self) -> bool:
        """A site answers maintenance, or the bench-wide flag waits for the next site."""
        return bool(self.down) or "maintenance_mode" in self.bench_flags


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceClearReport:
    """What the clear wrote and what the sites answered afterwards."""

    ok: bool
    project: str
    bench_path: str
    sites: list[str]
    cleared_bench_flags: list[str] = field(default_factory=list)
    cleared_sites: list[str] = field(default_factory=list)
    scheduler_resumed: bool = False
    answering: list[str] = field(default_factory=list)
    still_down: list[str] = field(default_factory=list)
    verified: bool = False
    owners: list[MaintenanceOwner] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MaintenanceStepStart:
    phase: str
    message: str


@dataclass(frozen=True, slots=True)
class MaintenanceStepEnd:
    phase: str
    status: str  # "ok" | "failed" | "warning"
    message: str


MaintenanceEvent = MaintenanceStepStart | MaintenanceStepEnd
OnEvent = Callable[[MaintenanceEvent], None]


# ------------------------------------------------------------------- the read


def _value(record: object) -> dict | None:
    """A file record's JSON object; ``{}`` when the file is absent (Frappe treats a
    missing config as empty), None when it is unreadable or not an object."""
    if not isinstance(record, dict):
        return None
    if record.get("missing"):
        return {}
    value = record.get("value")
    return value if isinstance(value, dict) else None


def _file_record(text: str, *, keys: tuple[str, ...] | None = _KEYS) -> dict:
    """One file block of :data:`_READ_SCRIPT` as ``{"value"}``/``{"missing"}``/
    ``{"unreadable"}``, keeping only ``keys`` of a JSON object."""
    stripped = text.strip()
    marker = f"{SENTINEL} "
    if stripped.startswith(f"{marker}missing"):
        return {"missing": True}
    if stripped.startswith(f"{marker}unreadable") or not stripped:
        return {"unreadable": True}
    try:
        value = json.loads(stripped)
    except ValueError:
        return {"unreadable": True}
    if not isinstance(value, dict):
        return {"unreadable": True}
    return {"value": value if keys is None else {k: value[k] for k in keys if k in value}}


def read_bench(container, bench_path: str) -> dict | None:
    """Every fact the maintenance decision needs, in ONE exec; None when it failed.

    ``{"common", "sites", "procfile", "currentsite", "frappe_init", "owners",
    "procs"}``: config records keep only :data:`_KEYS`; ``procs`` is ``[pid, argv]``
    for every process whose working directory is inside the bench.
    """
    try:
        exit_code, output = container.exec_run(
            [
                "sh",
                "-c",
                _READ_SCRIPT,
                "maintenance-read",
                bench_path,
                SENTINEL,
                bench_ops.MAINTENANCE_RECORD_DIR,
            ]
        )
    except Exception:  # noqa: BLE001 - an unreadable bench is an honest unknown
        return None
    if exit_code not in (0, None):
        return None
    raw: dict = {"sites": {}, "owners": {}, "procs": []}
    blocks: list[tuple[str, str, list[str]]] = []
    for line in supervision._decode(output).splitlines():
        if line.startswith(f"{SENTINEL} "):
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1] not in ("missing", "unreadable"):
                blocks.append((parts[1], parts[2], []))
                continue
        if blocks:
            blocks[-1][2].append(line)
    if not blocks or blocks[0][0] != "common":
        return None
    for kind, name, lines in blocks:
        text = "\n".join(lines)
        if kind == "common":
            raw["common"] = _file_record(text)
        elif kind == "site":
            raw["sites"][name] = _file_record(text)
        elif kind == "owner":
            raw["owners"][name] = _file_record(text, keys=None)
        elif kind == "proc" and name.isdigit():
            raw["procs"].append([int(name), text.split()])
        elif kind in ("procfile", "currentsite"):
            raw[kind] = text
        elif kind == "version":
            raw["frappe_init"] = text
    return raw


def _frappe_major(raw: dict) -> int | None:
    match = _VERSION_RE.search(raw.get("frappe_init") or "")
    return int(match.group(1)) if match else None


def _served_as(raw: dict, common: dict, sites: set[str]) -> str | None:
    """The site ``bench serve`` answers EVERY request as, or None when it routes by Host.

    Mirrors ``frappe.utils.bench_helper.get_sites`` for a ``serve`` with no ``--site``:
    ``default_site`` on v15+ (v15 still warns about, and ignores, ``currentsite.txt``),
    ``currentsite.txt`` on v14 and earlier. A web line that is not ``bench serve``
    (gunicorn) routes by Host.
    """
    match = resolvers._WEB_LINE_RE.search(raw.get("procfile") or "")
    if not match:
        return None
    try:
        argv = shlex.split(match.group(1))
    except ValueError:
        return None
    if "serve" not in argv or not any(a.rsplit("/", 1)[-1] == "bench" for a in argv):
        return None
    pinned: str | None = None
    if "--site" in argv and argv.index("--site") + 1 < len(argv):
        pinned = argv[argv.index("--site") + 1]
    else:
        major = _frappe_major(raw)
        current = (raw.get("currentsite") or "").strip() or None
        default = (
            common.get("default_site") if isinstance(common.get("default_site"), str) else None
        )
        if major is None:
            pinned = default or current
        elif major >= 15:
            pinned = default
        else:
            pinned = current
    return pinned if pinned in sites else None


def _owners(raw: dict, sites: set[str]) -> list[MaintenanceOwner]:
    here = socket.gethostname()
    owners = []
    for site, record in sorted((raw.get("owners") or {}).items()):
        data = _value(record)
        if site not in sites or not data or not isinstance(data.get("verb"), str):
            continue
        pid = data.get("pid")
        running: bool | None = None
        if data.get("host") == here and isinstance(pid, int):
            running = bool(
                daemon_identity.pid_alive(pid)
                and daemon_identity.process_start_time(pid) == data.get("process_started")
            )
        owners.append(
            MaintenanceOwner(
                site=site,
                verb=data["verb"],
                job_id=data.get("job_id") if isinstance(data.get("job_id"), str) else None,
                at=data.get("at") if isinstance(data.get("at"), str) else None,
                running=running,
            )
        )
    return owners


def _busy_processes(raw: dict) -> list[str]:
    reasons = []
    for pid, argv in raw.get("procs") or []:
        names = [a.rsplit("/", 1)[-1] for a in argv]
        frappe_cli = any(
            n == "bench" or n == "frappe" or "bench_helper" in a
            for n, a in zip(names, argv, strict=True)
        )
        sub = next((a for a in argv if a in _BUSY_SUBCOMMANDS), None)
        if frappe_cli and sub:
            reasons.append(f"an in-container 'bench {sub}' is still running (pid {pid})")
    return sorted(set(reasons))


def assess(
    raw: dict | None, *, bench_path: str, command: str = "", busy: list[str] | None = None
) -> BenchMaintenance | None:
    """The pure decision over a :func:`read_bench` record; None when it is missing."""
    if raw is None:
        return None
    common = _value(raw.get("common"))
    configs = {name: _value(rec) for name, rec in (raw.get("sites") or {}).items()}
    names = set(configs)
    flags = supervision.bench_wide_flags(common) or []
    served_as = _served_as(raw, common or {}, names)
    sites: list[SiteMaintenance] = []
    for name in sorted(names):
        target = served_as or name
        config = configs.get(target)
        if config is None or common is None:
            sites.append(SiteMaintenance(site=name, on=None))
            continue
        if "maintenance_mode" in config:
            on, cause = bool(config["maintenance_mode"]), "site"
        else:
            on, cause = bool(common.get("maintenance_mode")), "bench"
        if on:
            sites.append(
                SiteMaintenance(
                    site=name,
                    on=True,
                    cause="served_as" if target != name else cause,
                    served_as=target if target != name else None,
                )
            )
    down = [s.site for s in sites if s.on]
    in_scope = set(down) | ({served_as} if served_as else set())
    owners = [o for o in _owners(raw, names) if o.site in in_scope]
    return BenchMaintenance(
        bench_path=bench_path,
        bench_flags=flags,
        sites=sites,
        down=down,
        served_as=served_as,
        owners=owners,
        busy=list(busy or []),
        command=command,
    )


def probe_sites(container, *, port: int, sites: list[str]) -> dict[str, tuple[str, bool]]:
    """Each site's live answer to ``/api/method/ping``: ``(http_code, session_stopped)``.

    ONE exec for every site. A site that does not answer reads ``"000"``. Empty when
    the probe itself could not run.
    """
    if not sites:
        return {}
    try:
        exit_code, output = container.exec_run(
            ["sh", "-c", _PROBE_SCRIPT, "probe", str(port), *sites]
        )
    except Exception:  # noqa: BLE001 - no answer is an honest "not probed"
        return {}
    if exit_code not in (0, None):
        return {}
    answers: dict[str, tuple[str, bool]] = {}
    for line in supervision._decode(output).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in sites:
            answers[parts[0]] = (parts[1], parts[2] == "1")
    return answers


def _with_probe(
    report: BenchMaintenance, answers: dict[str, tuple[str, bool]], all_sites: list[str]
) -> BenchMaintenance:
    """Lay the live answers over the file model: what the site ANSWERS is the truth.

    A site answering SessionStopped is in maintenance whatever the files say (the
    files were changed less than Frappe's cache lifetime ago); a site answering 200
    is not. Any other answer (unreachable, another error) keeps the file model's.
    """
    if not answers:
        return report
    by_name = {s.site: s for s in report.sites}
    sites: list[SiteMaintenance] = []
    for name in all_sites:
        known = by_name.get(name)
        code, stopped = answers.get(name, (None, None))
        if code is None:
            if known is not None:
                sites.append(known)
            continue
        if stopped:
            base = known or SiteMaintenance(site=name, on=True, cause="cached")
            sites.append(replace(base, on=True, http_code=code, session_stopped=True))
        elif code == "200":
            continue
        elif known is not None:
            sites.append(replace(known, http_code=code, session_stopped=False))
    down = [s.site for s in sites if s.on]
    return replace(report, sites=sites, down=down, probed=True)


def _lock_reasons(container, bench_path: str, sites: list[str]) -> list[str]:
    reasons = []
    for site in sites:
        try:
            held = bench_ops.migrate_lock_held(container, bench_path, site)
        except CwcliError:
            reasons.append(f"cwcli could not tell whether the migrate lock on '{site}' is held")
            continue
        if held:
            reasons.append(f"a migrate holds the migrate lock on '{site}'")
    return reasons


def inspect(
    container,
    bench_path: str,
    *,
    project: str,
    bench_flag: str = "",
    port: int | None = None,
) -> BenchMaintenance | None:
    """One bench's maintenance state; None when the bench could not be read.

    ``port`` (the bench's container web port) turns on the live HTTP probe of every
    site; pass None from a repeating caller, which must not fill the access log.
    The probe and the busy checks run only when something is in maintenance, so the
    healthy case costs exactly one exec.
    """
    raw = read_bench(container, bench_path)
    command = clear_command(project, bench_flag)
    report = assess(raw, bench_path=bench_path, command=command)
    if report is None or raw is None:
        return report
    if not (report.sites or report.bench_flags):
        return report
    if port is not None:
        all_sites = sorted((raw.get("sites") or {}).keys())
        report = _with_probe(report, probe_sites(container, port=port, sites=all_sites), all_sites)
    if not report.needs_clearing:
        return report
    report = replace(report, busy=_busy(container, raw, report))
    return replace(report, summary=describe(report), question=prompt_text(report))


def _busy(container, raw: dict, report: BenchMaintenance) -> list[str]:
    reasons = [
        f"the cwcli '{o.verb}' that turned it on for '{o.site}' is still running"
        + (f" (job {o.job_id})" if o.job_id else "")
        for o in report.owners
        if o.running
    ]
    reasons += _busy_processes(raw)
    targets = sorted(set(report.down) | ({report.served_as} if report.served_as else set()))
    reasons += _lock_reasons(container, report.bench_path, targets)
    return reasons


def clear_command(project: str, bench_flag: str = "") -> str:
    """The one cwcli line that takes a bench's sites out of maintenance mode."""
    return f"cwcli status {project}{bench_flag} --clear-maintenance"


# ------------------------------------------------------------------- the words


def describe(report: BenchMaintenance) -> str:
    """One plain sentence set: which sites answer 503, which write holds them, and who
    set it (cwcli's interrupted operation by name, or neutrally: set outside cwcli)."""
    parts: list[str] = []
    if report.down:
        listed = ", ".join(report.down)
        noun = "site answers" if len(report.down) == 1 else "sites answer"
        parts.append(f"{len(report.down)} {noun} HTTP 503 (maintenance mode): {listed}.")
    causes = {s.cause for s in report.sites if s.on}
    if "maintenance_mode" in report.bench_flags:
        text = "maintenance_mode is set bench-wide in sites/common_site_config.json"
        if "pause_scheduler" in report.bench_flags:
            text += ", with pause_scheduler (the pair bench update sets while it runs)"
        if not report.down:
            text += "; every site overrides it today, but a new site would inherit it"
        parts.append(text + ".")
    if "site" in causes:
        own = [s.site for s in report.sites if s.on and s.cause == "site"]
        parts.append(f"Set in the site's own site_config.json: {', '.join(own)}.")
    if report.served_as and "served_as" in causes:
        parts.append(
            f"The web server (bench serve) answers every site as the default site "
            f"'{report.served_as}', so they all share its maintenance mode."
        )
    if "cached" in causes:
        parts.append(
            "No config file sets it any more; Frappe is still answering from its config "
            "cache, which expires within a minute."
        )
    left = [o for o in report.owners if o.running is False]
    if left:
        names = sorted({_owner_name(o) for o in left})
        parts.append(
            f"cwcli turned it on for {', '.join(names)}, which is no longer running: "
            "it was interrupted before it could turn it off."
        )
    elif not report.owners and report.needs_clearing:
        parts.append("cwcli has no record of setting it, so it may have been set on purpose.")
    return " ".join(parts)


def _owner_name(owner: MaintenanceOwner) -> str:
    name = f"'{owner.verb}'"
    if owner.job_id:
        name += f" (job {owner.job_id})"
    if owner.at:
        name += f" started {owner.at}"
    return name


def plan(report: BenchMaintenance, site: str | None = None) -> dict:
    """The writes that take ``site`` (or every affected site) out of maintenance.

    ``bench_flags``: bench-wide keys to set to 0 - ``maintenance_mode`` when a site
    in scope inherits it (or, at bench scope, whenever it is set), and
    ``pause_scheduler`` with it when it was paused alongside. ``sites``: sites whose
    OWN ``site_config.json`` sets maintenance mode (a pinned site's default site).
    """
    scope = [s for s in report.sites if s.on and (site is None or s.site == site)]
    own: set[str] = set()
    bench_wide = site is None and "maintenance_mode" in report.bench_flags
    for entry in scope:
        if entry.cause == "site":
            own.add(entry.site)
        elif entry.cause == "served_as" and entry.served_as:
            target = next((s for s in report.sites if s.site == entry.served_as), None)
            if target is not None and target.cause == "site":
                own.add(entry.served_as)
            else:
                bench_wide = bench_wide or "maintenance_mode" in report.bench_flags
        elif entry.cause == "bench":
            bench_wide = True
    flags = []
    if bench_wide and "maintenance_mode" in report.bench_flags:
        flags.append("maintenance_mode")
        if "pause_scheduler" in report.bench_flags:
            flags.append("pause_scheduler")
    return {"bench_flags": flags, "sites": sorted(own), "scope": [s.site for s in scope]}


def prompt_text(report: BenchMaintenance) -> str:
    """The question every frontend asks, built from the same plan the clear runs."""
    steps = plan(report)
    what = (
        f"Take {', '.join(report.down)} out of maintenance mode"
        if report.down
        else "Clear the bench-wide maintenance_mode flag"
    )
    if "pause_scheduler" in steps["bench_flags"]:
        what += " and resume the scheduler"
    return what + "?"


# ------------------------------------------------------------------- the clear


def clear(
    project: str,
    *,
    bench: str | None = None,
    path: str | None = None,
    site: str | None = None,
    consent: bool = False,
    on_event: OnEvent | None = None,
    verify_timeout: float = VERIFY_TIMEOUT_S,
) -> Result[MaintenanceClearReport]:
    """Take one bench's sites (or one ``site``) out of maintenance mode, then verify.

    Steps, each reported as a start/end event pair:

    1. **Check.** Read the bench (:func:`inspect`, with the live probe). Nothing in
       maintenance is an OK no-op. Anything that sets maintenance mode still running
       (:attr:`BenchMaintenance.busy`) RAISES ``CONFLICT`` naming it, before consent
       is even asked: clearing behind a live migrate is never offered.
    2. **Consent.** ``consent=False`` returns ``NEEDS_CHOICE``
       ``confirm_clear_maintenance`` carrying the same question every frontend asks.
    3. **Clear the bench-wide flags** with ``bench set-config --global --parse <flag>
       0`` - the write ``bench update`` makes when it succeeds - and the paused
       scheduler with them, when they are the cause. **Clear each site's own flag**
       with ``bench --site <site> set-maintenance-mode off``.
    4. **Verify** no site still answers Frappe's ping with 503/SessionStopped, waiting
       up to ``verify_timeout`` for Frappe's 60-second config cache. A site that does
       not answer at all is not waited for: it leaves ``verified`` False with a
       ``maintenance.unverified`` warning.

    ``ok`` is False when a write failed or a site still answers maintenance.
    """
    emit = on_event or (lambda _e: None)
    if site is not None:
        resolvers.validate_site_name(site)
    resolved = resolvers.resolve_container_and_bench(project, bench, path)
    if isinstance(resolved, Result):
        return resolved
    container, bench_path, warnings = resolved
    bench_flag = resolvers.bench_cli_flag(project, bench_path)
    ports = resolvers.resolve_web_ports(container, [bench_path])
    port = ports.get(bench_path)

    emit(MaintenanceStepStart(phase="check", message="Read maintenance mode on every site"))
    report = inspect(container, bench_path, project=project, bench_flag=bench_flag, port=port)
    if report is None:
        emit(MaintenanceStepEnd(phase="check", status="failed", message="Could not read the bench"))
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "maintenance.unreadable",
            f"cwcli could not read the site configs of {bench_path}.",
        )
    steps = plan(report, site)
    if not (steps["bench_flags"] or steps["sites"] or steps["scope"]):
        emit(
            MaintenanceStepEnd(phase="check", status="ok", message="No site is in maintenance mode")
        )
        return Result(
            status=Status.OK,
            data=MaintenanceClearReport(ok=True, project=project, bench_path=bench_path, sites=[]),
            warnings=warnings,
        )
    if report.busy:
        emit(MaintenanceStepEnd(phase="check", status="failed", message=report.busy[0]))
        raise CwcliError(
            ErrorKind.CONFLICT,
            "maintenance.busy",
            "Maintenance mode is not cleared while it is still in use: "
            + "; ".join(report.busy)
            + ".",
            hint=f"Wait for it to finish, then check again with 'cwcli status {project}{bench_flag}'.",
            detail={"busy": report.busy, "bench_path": bench_path},
        )
    found = ", ".join(steps["scope"]) or "the bench-wide flag"
    emit(
        MaintenanceStepEnd(
            phase="check",
            status="ok",
            message=f"In maintenance mode: {found}; nothing that sets it is running",
        )
    )
    if not consent:
        return Result(
            status=Status.NEEDS_CHOICE,
            choice=Choice(
                kind="confirm_clear_maintenance",
                param="consent",
                prompt=prompt_text(report),
            ),
            warnings=warnings,
        )

    ok = True
    cleared_flags: list[str] = []
    if steps["bench_flags"]:
        resumes = "pause_scheduler" in steps["bench_flags"]
        emit(
            MaintenanceStepStart(
                phase="bench_flags",
                message="Clear bench-wide maintenance mode"
                + (" and resume the scheduler" if resumes else ""),
            )
        )
        for flag in steps["bench_flags"]:
            if _clear_bench_flag(container, bench_path, flag):
                cleared_flags.append(flag)
        flags_ok = cleared_flags == steps["bench_flags"]
        ok = ok and flags_ok
        emit(
            MaintenanceStepEnd(
                phase="bench_flags",
                status="ok" if flags_ok else "failed",
                message=(
                    "Set "
                    + " and ".join(f"{f}=0" for f in cleared_flags)
                    + " in sites/common_site_config.json"
                    + (
                        "; the scheduler resumes on its next tick"
                        if "pause_scheduler" in cleared_flags
                        else ""
                    )
                    if cleared_flags
                    else "bench set-config refused the write"
                ),
            )
        )
    cleared_sites: list[str] = []
    for name in steps["sites"]:
        emit(
            MaintenanceStepStart(
                phase=f"site_{name}", message=f"Turn off {name}'s own maintenance mode"
            )
        )
        done = bench_ops.set_maintenance(container, bench_path, name, enable=False)
        if done:
            cleared_sites.append(name)
        ok = ok and done
        emit(
            MaintenanceStepEnd(
                phase=f"site_{name}",
                status="ok" if done else "failed",
                message=f"{name}: maintenance_mode=0" if done else f"bench refused for {name}",
            )
        )
    # A bench-wide clear also clears any record for the sites it freed.
    for name in steps["scope"]:
        if name not in cleared_sites and cleared_flags:
            bench_ops.forget_maintenance_owner(container, bench_path, name)

    answering, still_down, unanswered = _verify(
        container, port=port, sites=steps["scope"], timeout=verify_timeout, emit=emit
    )
    ok = ok and not still_down
    verified = port is not None and bool(steps["scope"]) and not unanswered
    if unanswered:
        warnings.append(
            Message(
                "maintenance.unverified",
                f"{', '.join(unanswered)} did not answer at all, so cwcli could not check "
                f"that they are out of maintenance mode; start the bench and run "
                f"`cwcli status {project}{bench_flag}`.",
            )
        )
    if port is None and steps["scope"]:
        warnings.append(
            Message(
                "maintenance.unverified",
                f"cwcli could not tell which port {bench_path} serves, so it did not check "
                "that the sites answer again.",
            )
        )
    return Result(
        status=Status.OK,
        data=MaintenanceClearReport(
            ok=ok,
            project=project,
            bench_path=bench_path,
            sites=steps["scope"],
            cleared_bench_flags=cleared_flags,
            cleared_sites=cleared_sites,
            scheduler_resumed="pause_scheduler" in cleared_flags,
            answering=answering,
            still_down=still_down,
            verified=verified,
            owners=report.owners,
        ),
        warnings=warnings,
    )


def _clear_bench_flag(container, bench_path: str, flag: str) -> bool:
    """``bench set-config --global --parse <flag> 0``: the bench-wide write ``bench
    update`` itself makes when it succeeds. ``--parse`` stores the integer 0; the
    string "0" a bare ``set-config`` would store is truthy, so Frappe would still
    read the flag as set."""
    code, _ = container.exec_run(
        f"bench set-config --global --parse {shlex.quote(flag)} 0", workdir=bench_path
    )
    return bool(code == 0)


def _verify(
    container, *, port: int | None, sites: list[str], timeout: float, emit: OnEvent
) -> tuple[list[str], list[str], list[str]]:
    """Poll while any site still answers maintenance (503/SessionStopped), within
    ``timeout``. Returns ``(answering, still_down, unanswered)``: a site that answers
    nothing (web not listening, probe failed) is neither, and is not waited for."""
    if not sites or port is None:
        return [], [], []
    emit(MaintenanceStepStart(phase="verify", message="Check every site answers again"))
    deadline = time.monotonic() + timeout
    answers: dict[str, tuple[str, bool]] = {}
    while True:
        answers = probe_sites(container, port=port, sites=sites)
        still_down = [
            s for s in sites if s in answers and (answers[s][1] or answers[s][0] == "503")
        ]
        if not still_down or time.monotonic() >= deadline:
            break
        time.sleep(min(_VERIFY_INTERVAL_S, max(0.0, deadline - time.monotonic())))
    unanswered = [s for s in sites if answers.get(s, ("000", False))[0] == "000"]
    answering = [s for s in sites if s not in still_down and s not in unanswered]
    if still_down:
        detail = ", ".join(
            f"{s} (HTTP {answers[s][0]}" + (", still SessionStopped" if answers[s][1] else "") + ")"
            for s in still_down
        )
        emit(
            MaintenanceStepEnd(
                phase="verify", status="failed", message=f"Still in maintenance mode: {detail}"
            )
        )
    elif unanswered:
        emit(
            MaintenanceStepEnd(
                phase="verify",
                status="warning",
                message=f"No answer from {', '.join(unanswered)}: the web server may not be running",
            )
        )
    else:
        emit(
            MaintenanceStepEnd(
                phase="verify",
                status="ok",
                message=f"No site answers maintenance mode: {', '.join(answering)}",
            )
        )
    return answering, still_down, unanswered
