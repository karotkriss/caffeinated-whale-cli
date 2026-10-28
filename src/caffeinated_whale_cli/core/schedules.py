"""Scheduled jobs (UI-pure): the store, a five-field cron, the guards and the tick.

A schedule is a job the daemon starts on a timetable. Every run is an ordinary
job (``origin.schedule = <id>``), so history, logs and Retry work unchanged.

* Store: ``<cwcli home>/schedules.json`` - the schedules, each instance's
  maintenance window, the quiet hours and the resource guards. Every schedule
  records an ``owner`` and an edit ``history`` (shared mode asks for both; they
  cost nothing elsewhere).
* Time: ``cron`` (``m h dom mon dow``, with ``*``, lists, ranges and ``/step``)
  or a one-off ``at``; ``next_run`` is computed in the machine's local time.
* Consent: backup, restart, start/stop and cache clears run unattended freely;
  ``apps-update`` and ``migrate`` only when the schedule says ``unattended``.
  Removals are never schedulable (``SCHEDULABLE`` is the whole list).
* Missed runs: ``missed = "run"`` (default) runs once on wake, ``"skip"`` waits
  for the next occurrence.
* Guards at fire time: the instance's maintenance window (when the schedule
  respects one), quiet hours, a stopped instance (started first when the
  schedule says so, else skipped), free disk and memory thresholds, and one job
  per instance at a time (the job lock; retried on the next tick).
* Two schedulers must never fire the same minute twice: every write (``tick``
  and each edit) takes the store's lock file, so the UI daemon, the machine-wide
  service and the CLI coexist.

Everything takes ``now`` so a test controls the clock.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..utils import config_utils, shared_home
from .errors import CwcliError, ErrorKind

SCHEDULABLE: dict[str, str] = {
    # verb -> what it needs: "project" | "bench" | "site" | "process"
    "backup-sites": "project",
    "backup": "site",
    "prune-backups": "project",
    "s3-copy": "project",
    "s3-prune": "project",
    "restore-drill": "site",
    "apps-update": "bench",
    "migrate": "site",
    "restart": "project",
    "restart-process": "process",
    "start": "project",
    "stop": "project",
    "stop-bench": "bench",
    "dev-mode": "site",
    "clear-maintenance": "site",
    "build": "bench",
}
NEEDS_UNATTENDED_FLAG = frozenset({"apps-update", "migrate"})
NEVER = frozenset({"rm", "rm-site", "rm-bench", "delete-backup", "move-sites", "restore-set"})

DEFAULT_GUARDS = {"min_free_disk_gb": 5, "min_free_mem_mb": 512}
_lock = threading.RLock()
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def store_path() -> Path:
    return config_utils.cwcli_home() / "schedules.json"


# ----------------------------------------------------------------- the file


def _empty() -> dict[str, Any]:
    return {"version": 1, "schedules": {}, "windows": {}, "quiet_hours": None, "guards": {}}


def _load() -> dict[str, Any]:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "schedules.unreadable", f"{store_path()} is unreadable: {e}"
        ) from e
    if not isinstance(data, dict):
        raise CwcliError(
            ErrorKind.PRECONDITION, "schedules.unreadable", f"{store_path()} is not a JSON object"
        )
    base = _empty()
    base.update(data)
    return base


def _save(data: dict[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, shared_home.file_mode(0o600))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    shared_home.apply_group(path)


# --------------------------------------------------------------------- cron


def _field(spec: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            raise ValueError("empty field")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError("step must be positive")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _num(a, names), _num(b, names)
        else:
            start = end = _num(part, names)
            if step > 1:
                end = hi
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"{part} is outside {lo}-{hi}")
        out.update(range(start, end + 1, step))
    return out


def _num(token: str, names: dict[str, int] | None) -> int:
    if names and token in names:
        return names[token]
    return int(token)


_MONTHS = {
    m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())
}
_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}  # cron's numbering


def parse_cron(expr: str) -> dict[str, Any]:
    """``m h dom mon dow`` -> the allowed values per field; raises ValueError on a bad one.
    Day-of-week is cron's (0 and 7 are Sunday); day-of-month and day-of-week are
    OR'ed when both are restricted, as cron does."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("a cron expression has five fields: minute hour day month weekday")
    m, h, dom, mon, dow = parts
    days = _field(dow, 0, 7, _DOW)
    if 7 in days:
        days.discard(7)
        days.add(0)
    return {
        "minute": _field(m, 0, 59),
        "hour": _field(h, 0, 23),
        "dom": _field(dom, 1, 31),
        "month": _field(mon, 1, 12, _MONTHS),
        "dow": days,
        "dom_any": dom.strip() == "*",
        "dow_any": dow.strip() == "*",
    }


def _day_matches(c: dict[str, Any], d: datetime) -> bool:
    if d.month not in c["month"]:
        return False
    py_dow = (d.weekday() + 1) % 7  # Monday=0 -> cron Sunday=0
    dom_ok = d.day in c["dom"]
    dow_ok = py_dow in c["dow"]
    if c["dom_any"] and c["dow_any"]:
        return True
    if c["dom_any"]:
        return dow_ok
    if c["dow_any"]:
        return dom_ok
    return dom_ok or dow_ok


def next_cron(expr: str, after: datetime) -> datetime | None:
    """The first minute strictly after ``after`` that the expression matches, within
    366 days, or None. Matched against the machine's local wall clock, so a daily
    02:00 stays 02:00 across a daylight-saving change."""
    c = parse_cron(expr)
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hours = sorted(c["hour"])
    minutes = sorted(c["minute"])
    day = start.astimezone().replace(tzinfo=None, hour=0, minute=0)
    for _ in range(367):
        if _day_matches(c, day):
            for hh in hours:
                for mm in minutes:
                    cand = day.replace(hour=hh, minute=mm).astimezone()
                    if cand >= start:
                        return cand
        day += timedelta(days=1)
    return None


def describe_cron(expr: str) -> str:
    """A short human reading for the common shapes; the raw expression otherwise."""
    try:
        parts = expr.split()
        m, h, dom, mon, dow = parts
        hm = f"{int(h):02d}:{int(m):02d}" if h.isdigit() and m.isdigit() else None
        if hm and dom == "*" and mon == "*" and dow == "*":
            return f"daily at {hm}"
        if hm and dom == "*" and mon == "*":
            names = ", ".join(_dow_name(x) for x in dow.split(","))
            return f"weekly on {names} at {hm}"
        if hm and mon == "*" and dow == "*" and dom.isdigit():
            return f"monthly on day {int(dom)} at {hm}"
        if m.isdigit() and h == "*" and dom == "*" and mon == "*" and dow == "*":
            return f"hourly at :{int(m):02d}"
        if h.startswith("*/") and m.isdigit():
            return f"every {h[2:]} hours at :{int(m):02d}"
        if m.startswith("*/") and h == "*":
            return f"every {m[2:]} minutes"
    except (ValueError, IndexError):
        pass
    return expr


def _dow_name(x: str) -> str:
    def one(v: str) -> str:
        return _DAYS[(int(v) - 1) % 7].capitalize() if v.isdigit() else v

    if "-" in x and all(p.isdigit() for p in x.split("-")):
        a, b = x.split("-", 1)
        return f"{one(a)}-{one(b)}"
    return one(x)


# ---------------------------------------------------------------- records


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M:%S%z") if d else None


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    return d if d.tzinfo else d.astimezone()


def compute_next(sched: dict[str, Any], after: datetime) -> datetime | None:
    if not sched.get("enabled", True):
        return None
    if sched.get("kind") == "once":
        at = _parse(sched.get("at"))
        return at if at and (at > after or not sched.get("last_run")) else None
    if sched.get("kind") == "event":
        return None
    try:
        return next_cron(str(sched.get("cron") or ""), after)
    except ValueError:
        return None


def validate(sched: dict[str, Any]) -> None:
    verb = str(sched.get("verb") or "")
    if verb in NEVER:
        raise CwcliError(ErrorKind.USAGE, "schedule.never", f"'{verb}' can never be scheduled.")
    if verb not in SCHEDULABLE and not sched.get("steps"):
        raise CwcliError(
            ErrorKind.USAGE,
            "schedule.verb_unsupported",
            f"'{verb}' is not a schedulable job; one of: {', '.join(sorted(SCHEDULABLE))}.",
        )
    target = sched.get("target") or {}
    if not target.get("project"):
        raise CwcliError(
            ErrorKind.USAGE, "schedule.project_required", "A schedule needs an instance."
        )
    need = SCHEDULABLE.get(verb, "project")
    if need == "site" and not target.get("site"):
        raise CwcliError(ErrorKind.USAGE, "schedule.site_required", f"'{verb}' needs a site.")
    if need == "process" and not target.get("process"):
        raise CwcliError(
            ErrorKind.USAGE, "schedule.process_required", "restart-process needs a process label."
        )
    for step in sched.get("steps") or []:
        if step.get("verb") in NEVER:
            raise CwcliError(
                ErrorKind.USAGE, "schedule.never", f"'{step.get('verb')}' can never be scheduled."
            )
        if step.get("verb") not in SCHEDULABLE:
            raise CwcliError(
                ErrorKind.USAGE,
                "schedule.verb_unsupported",
                f"'{step.get('verb')}' is not a schedulable job.",
            )
    kind = sched.get("kind") or "cron"
    if kind == "cron":
        try:
            parse_cron(str(sched.get("cron") or ""))
        except ValueError as e:
            raise CwcliError(
                ErrorKind.USAGE, "schedule.cron_invalid", f"Bad cron expression: {e}"
            ) from e
    elif kind == "once":
        if _parse(sched.get("at")) is None:
            raise CwcliError(
                ErrorKind.USAGE,
                "schedule.at_invalid",
                "A one-off schedule needs a valid 'at' time.",
            )
    elif kind == "event":
        trig = sched.get("trigger") or {}
        if trig.get("type") not in {"after_job", "on_start"}:
            raise CwcliError(
                ErrorKind.USAGE,
                "schedule.trigger_invalid",
                "An event schedule needs a trigger: after_job or on_start.",
            )
        if trig["type"] == "after_job" and not trig.get("verb"):
            raise CwcliError(
                ErrorKind.USAGE, "schedule.trigger_invalid", "after_job needs the verb to wait for."
            )
    else:
        raise CwcliError(
            ErrorKind.USAGE, "schedule.kind_invalid", "kind must be cron, once or event."
        )
    if sched.get("missed", "run") not in {"run", "skip"}:
        raise CwcliError(ErrorKind.USAGE, "schedule.missed_invalid", "missed must be run or skip.")


def _public(sid: str, s: dict[str, Any], now: datetime) -> dict[str, Any]:
    out = dict(s)
    out["id"] = sid
    out["next_run"] = _iso(compute_next(s, now)) if s.get("kind") != "event" else None
    out["when_text"] = (
        describe_cron(str(s.get("cron") or ""))
        if s.get("kind", "cron") == "cron"
        else (
            f"once at {s.get('at')}"
            if s.get("kind") == "once"
            else _trigger_text(s.get("trigger") or {})
        )
    )
    return out


def _trigger_text(t: dict[str, Any]) -> str:
    if t.get("type") == "after_job":
        return f"after a successful {t.get('verb')}" + (
            f" on {t.get('project')}" if t.get("project") else ""
        )
    if t.get("type") == "on_start":
        return "when the instance starts"
    return "event"


def list_schedules(now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    with _lock:
        data = _load()
    rows = [_public(k, v, now) for k, v in data["schedules"].items()]
    rows.sort(
        key=lambda r: (r.get("next_run") is None, r.get("next_run") or "", r.get("name") or "")
    )
    return {
        "schedules": rows,
        "windows": data.get("windows") or {},
        "quiet_hours": data.get("quiet_hours"),
        "guards": {**DEFAULT_GUARDS, **(data.get("guards") or {})},
        "digest": data.get("digest") or "off",
        "upcoming": upcoming(data, now),
    }


def upcoming(
    data: dict[str, Any], now: datetime, days: int = 7, limit: int = 60
) -> list[dict[str, Any]]:
    """The next runs across every schedule inside ``days``, soonest first."""
    horizon = now + timedelta(days=days)
    out: list[dict[str, Any]] = []
    for sid, s in data["schedules"].items():
        if not s.get("enabled", True) or s.get("kind") == "event":
            continue
        t = compute_next(s, now)
        n = 0
        while t and t <= horizon and n < 20:
            out.append(
                {
                    "id": sid,
                    "name": s.get("name"),
                    "verb": s.get("verb"),
                    "target": s.get("target"),
                    "at": _iso(t),
                }
            )
            n += 1
            t = compute_next(s, t) if s.get("kind") == "cron" else None
    out.sort(key=lambda r: r["at"])
    return out[:limit]


def save(sched: dict[str, Any], *, by: str = "", now: datetime | None = None) -> dict[str, Any]:
    """Create (no ``id``) or update a schedule; validated first. Returns the public row."""
    now = now or _now()
    validate(sched)
    with _lock, _StoreLock():
        data = _load()
        sid = str(sched.get("id") or "")
        existing = data["schedules"].get(sid) if sid else None
        if sid and existing is None:
            raise CwcliError(ErrorKind.NOT_FOUND, "schedule.unknown", f"No schedule '{sid}'.")
        if not sid:
            sid = "sch-" + secrets.token_hex(4)
        rec = dict(existing or {})
        keep = {"last_run", "history", "owner", "created_at", "await_start", "recipe", "due_from"}
        rec.update({k: v for k, v in sched.items() if k not in keep and k != "id"})
        rec.setdefault("enabled", True)
        rec.setdefault("kind", "cron")
        rec.setdefault("missed", "run")
        if existing and any(
            rec.get(k) != existing.get(k) for k in ("kind", "cron", "at", "enabled")
        ):
            _rearm(rec, now)
        rec.setdefault("created_at", _iso(now))
        rec.setdefault("owner", by or "")
        hist = list(rec.get("history") or [])
        hist.append(
            {"at": _iso(now), "by": by or "", "action": "updated" if existing else "created"}
        )
        rec["history"] = hist[-20:]
        data["schedules"][sid] = rec
        _save(data)
    return _public(sid, rec, now)


def delete(sid: str) -> None:
    with _lock, _StoreLock():
        data = _load()
        if sid not in data["schedules"]:
            raise CwcliError(ErrorKind.NOT_FOUND, "schedule.unknown", f"No schedule '{sid}'.")
        del data["schedules"][sid]
        _save(data)


def set_enabled(sid: str, enabled: bool, *, by: str = "", now: datetime | None = None) -> None:
    now = now or _now()
    with _lock, _StoreLock():
        data = _load()
        s = data["schedules"].get(sid)
        if s is None:
            raise CwcliError(ErrorKind.NOT_FOUND, "schedule.unknown", f"No schedule '{sid}'.")
        if enabled and not s.get("enabled", True):
            _rearm(s, now)
        s["enabled"] = bool(enabled)
        s.setdefault("history", []).append(
            {"at": _iso(now), "by": by, "action": "enabled" if enabled else "disabled"}
        )
        _save(data)


def save_settings(
    *,
    windows: dict[str, Any] | None = None,
    quiet_hours: dict[str, Any] | None | Literal[False] = False,
    guards: dict[str, Any] | None = None,
    digest: str | None = None,
) -> None:
    """Maintenance windows (``{project: {start, end, days}}``), quiet hours
    (``{start, end}``; pass ``None`` to clear), guards and the digest choice."""
    with _lock, _StoreLock():
        data = _load()
        if windows is not None:
            for w in windows.values():
                if w:
                    _check_hhmm(w.get("start"))
                    _check_hhmm(w.get("end"))
            data["windows"] = {p: w for p, w in windows.items() if w}
        if quiet_hours is not False:
            if isinstance(quiet_hours, dict):
                _check_hhmm(quiet_hours.get("start"))
                _check_hhmm(quiet_hours.get("end"))
                data["quiet_hours"] = quiet_hours
            else:
                data["quiet_hours"] = None
        if guards is not None:
            data["guards"] = {k: float(v) for k, v in guards.items() if k in DEFAULT_GUARDS}
        if digest is not None:
            if digest not in {"off", "daily", "weekly"}:
                raise CwcliError(
                    ErrorKind.USAGE,
                    "schedule.digest_invalid",
                    "digest must be off, daily or weekly.",
                )
            data["digest"] = digest
        _save(data)


def _check_hhmm(v: Any) -> None:
    try:
        h, m = str(v).split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, AssertionError) as e:
        raise CwcliError(ErrorKind.USAGE, "schedule.time_invalid", f"'{v}' is not HH:MM.") from e


def _in_window(w: dict[str, Any] | None, now: datetime) -> bool:
    """Inside ``{start, end, days}`` (local HH:MM; a window may cross midnight)."""
    if not w:
        return True
    days = w.get("days") or _DAYS
    sh, sm = (int(x) for x in str(w["start"]).split(":"))
    eh, em = (int(x) for x in str(w["end"]).split(":"))
    start_min, end_min, cur = sh * 60 + sm, eh * 60 + em, now.hour * 60 + now.minute
    today = _DAYS[now.weekday()]
    yesterday = _DAYS[(now.weekday() - 1) % 7]
    if start_min <= end_min:
        return today in days and start_min <= cur < end_min
    # crosses midnight: the late part belongs to the start day, the early part to the next
    return (today in days and cur >= start_min) or (yesterday in days and cur < end_min)


# ------------------------------------------------------------------- guards


def _free_disk_gb() -> float:
    try:
        return shutil.disk_usage(config_utils.cwcli_home()).free / 1e9
    except OSError:
        return float("inf")


def _free_mem_mb() -> float:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return float("inf")


def templates() -> list[dict[str, Any]]:
    """Prefilled schedules the page offers; each is a valid ``save`` body minus the target."""
    return [
        {
            "key": "nightly-backup",
            "name": "Nightly backup",
            "verb": "backup-sites",
            "cron": "0 2 * * *",
            "args": {"reason": "scheduled"},
            "respect_window": False,
        },
        {
            "key": "weekly-core-update",
            "name": "Weekly core update",
            "verb": "apps-update",
            "cron": "0 3 * * 0",
            "args": {"apps": ["frappe"], "build": True},
            "unattended": True,
            "respect_window": True,
            "note": "Runs inside the instance's maintenance window and takes the automatic backup before the core update.",
        },
        {
            "key": "nightly-worker-restart",
            "name": "Nightly worker restart",
            "verb": "restart-process",
            "cron": "30 4 * * *",
            "process": "worker:default",
            "respect_window": False,
        },
        {
            "key": "stop-evening",
            "name": "Stop in the evening",
            "verb": "stop",
            "cron": "0 20 * * 1-5",
            "respect_window": False,
        },
        {
            "key": "start-morning",
            "name": "Start in the morning",
            "verb": "start",
            "cron": "0 7 * * 1-5",
            "respect_window": False,
        },
    ]


# --------------------------------------------------------------------- tick

CreateJob = Callable[..., dict[str, Any]]
InstanceStatus = Callable[[str], str | None]  # project -> "running" | "stopped" | None (unknown)
ReadJob = Callable[[str], dict[str, Any]]

_START_STOP = frozenset({"start", "stop", "stop-bench"})
_ACTIVE = frozenset({"queued", "running", "interrupting"})
_STOPPED = "the instance is stopped"


def _read_job(job_id: str) -> dict[str, Any]:
    from .. import jobs

    return jobs.read_job(job_id)


def _consent_for(s: dict[str, Any], verb: str) -> bool:
    return verb not in NEEDS_UNATTENDED_FLAG or bool(s.get("unattended"))


def _rearm(s: dict[str, Any], now: datetime) -> None:
    """Wait for the first slot after ``now``, not one that passed while the schedule
    was off or before it was edited."""
    s["due_from"] = _iso(now)
    if s.get("last_run"):
        s["last_run"] = {k: v for k, v in s["last_run"].items() if k != "due"}


def _advance(s: dict[str, Any], now: datetime) -> str | None:
    """The slot after ``now`` the schedule waits on next; a one-off retires instead."""
    if s.get("kind") == "once":
        s["enabled"] = False
        return None
    return _iso(compute_next(s, now))


def _skip_reason(
    s: dict[str, Any], verb: str, data: dict[str, Any], now: datetime, status: str | None
) -> str | None:
    """Why ``verb`` cannot start for the schedule right now (the fire-time guards), or None."""
    guards = {**DEFAULT_GUARDS, **(data.get("guards") or {})}
    quiet = data.get("quiet_hours")
    project = str((s.get("target") or {}).get("project") or "")
    verbs = [str(step.get("verb") or "") for step in s.get("steps") or []] or [verb]
    if not all(_consent_for(s, v) for v in verbs):
        return "updates and migrate run on a schedule only when it allows unattended runs"
    if quiet and _in_window(quiet, now) and verb not in _START_STOP:
        return "quiet hours"
    if s.get("respect_window") and not _in_window((data.get("windows") or {}).get(project), now):
        return "outside the maintenance window"
    if verb not in {"stop", "stop-bench"}:
        if _free_disk_gb() < float(guards["min_free_disk_gb"]):
            return f"less than {guards['min_free_disk_gb']} GB free on disk"
        if _free_mem_mb() < float(guards["min_free_mem_mb"]):
            return f"less than {int(guards['min_free_mem_mb'])} MB of memory available"
    if verb not in _START_STOP and status == "stopped":
        return _STOPPED
    return None


def _waits(reason: str) -> bool:
    """Windows, quiet hours and low resources hold the slot; everything else skips it."""
    return reason in {"quiet hours", "outside the maintenance window"} or reason.startswith(
        "less than"
    )


def _fire(
    sid: str,
    s: dict[str, Any],
    verb: str,
    target: dict[str, Any],
    args: dict[str, Any],
    create_job: CreateJob,
    now: datetime,
    origin_extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Start one job for the schedule; a busy instance is left for the next tick. The
    slot being waited on (``due``) is carried over so a recipe's later steps do not
    re-open the slot the first step already consumed."""
    due = (s.get("last_run") or {}).get("due")
    try:
        record = create_job(
            verb,
            dict(target),
            dict(args or {}),
            consent=_consent_for(s, verb),
            origin={
                "schedule": sid,
                "account": "schedule",
                "via": "schedule",
                **(origin_extra or {}),
            },
        )
    except CwcliError as e:
        if e.code == "job.project_busy":
            s["last_skip"] = {
                "at": _iso(now),
                "reason": "the instance has a job running; retried next minute",
            }
            return None
        s["last_run"] = {
            "due": due,
            "at": _iso(now),
            "job_id": None,
            "state": "failed",
            "error": e.message,
        }
        if s.get("kind") != "event" and not (origin_extra or {}).get("manual"):
            s["last_run"]["due"] = _advance(s, now)
        return None
    s["last_run"] = {
        "due": due,
        "at": _iso(now),
        "job_id": record.get("id"),
        "state": record.get("state", "queued"),
        "verb": verb,
    }
    s.pop("last_skip", None)
    return record


def tick(
    create_job: CreateJob,
    instance_status: InstanceStatus,
    *,
    now: datetime | None = None,
    finished_jobs: list[dict[str, Any]] | None = None,
    started_projects: set[str] | None = None,
    read_job: ReadJob | None = None,
) -> list[dict[str, Any]]:
    """One scheduler pass: fire what is due (and allowed), advance, record. Returns
    the job records it created. ``finished_jobs`` (terminal records since the last
    tick) and ``started_projects`` (instances seen going stopped -> running) feed the
    event triggers; the last run's outcome and a recipe's next step come from the
    job records themselves (``read_job``), so they move on with no page open."""
    now = now or _now()
    read_job = read_job or _read_job
    created: list[dict[str, Any]] = []
    with _lock, _StoreLock():
        data = _load()
        changed = False
        for sid, s in data["schedules"].items():
            if not s.get("enabled", True):
                continue
            if s.get("recipe") and s.get("steps"):
                continue  # a recipe still walking its steps is not started again
            verb = str(s.get("verb") or "")
            target = dict(s.get("target") or {})
            project = str(target.get("project") or "")
            if s.get("kind") == "event":
                if not _event_fires(
                    sid, s, finished_jobs or [], started_projects or set(), project
                ):
                    continue
            else:
                nxt = _next_due(s, now)
                if nxt is None or nxt > now:
                    continue
                if s.get("missed", "run") == "skip" and now - nxt > timedelta(minutes=2):
                    s["last_run"] = {
                        **(s.get("last_run") or {}),
                        "due": _advance(s, now),
                        "state": "skipped",
                        "at": _iso(now),
                        "reason": "missed while the machine was off",
                    }
                    changed = True
                    continue
            status = instance_status(project) if verb not in _START_STOP else None
            skip = _skip_reason(s, verb, data, now, status)
            if skip == _STOPPED:
                if s.get("start_if_stopped") and not s.get("await_start"):
                    rec = _fire(
                        sid, s, "start", {"project": project}, {}, create_job, now, {"for": verb}
                    )
                    if rec:
                        created.append(rec)
                        s["await_start"] = True
                    changed = True
                    continue  # the real job fires on a later tick, once the instance is up
                if s.get("await_start"):
                    skip = "the instance did not start"
                    s.pop("await_start", None)
            if skip:
                s["last_skip"] = {"at": _iso(now), "reason": skip}
                if not _waits(skip) and s.get("kind") != "event":
                    s["last_run"] = {
                        **(s.get("last_run") or {}),
                        "due": _advance(s, now),
                        "state": "skipped",
                        "at": _iso(now),
                        "reason": skip,
                    }
                changed = True
                continue
            s.pop("await_start", None)
            steps = s.get("steps") or []
            if steps:
                step = steps[0]
                rec = _fire(
                    sid,
                    s,
                    str(step["verb"]),
                    {**target, **(step.get("target") or {})},
                    step.get("args") or {},
                    create_job,
                    now,
                    {"recipe_step": 0, "recipe_total": len(steps)},
                )
            else:
                rec = _fire(sid, s, verb, target, s.get("args") or {}, create_job, now)
            if rec:
                created.append(rec)
                if steps:
                    s["recipe"] = {"job_id": rec.get("id"), "step": 0}
                s["last_run"]["due"] = _advance(s, now) if s.get("kind") != "event" else _iso(now)
            changed = True
        # ---- outcomes and recipes, read from the job records
        for sid, s in data["schedules"].items():
            last = s.get("last_run") or {}
            recipe, steps = s.get("recipe"), s.get("steps") or []
            if recipe and steps:
                job_id = recipe.get("job_id")
            elif last.get("state") in _ACTIVE:
                job_id = last.get("job_id")
            else:
                continue
            try:
                done = read_job(str(job_id))
            except CwcliError:
                done = {"id": job_id, "state": "unknown"}
            if done.get("state") in _ACTIVE:
                continue
            if last.get("job_id") == job_id and last.get("state") != done.get("state"):
                s["last_run"] = last = {
                    **last,
                    "state": done.get("state"),
                    "finished_at": done.get("finished_at"),
                }
                changed = True
            if not (recipe and steps):
                continue
            nxt_i = int(recipe.get("step", 0)) + 1
            if done.get("state") != "succeeded" or nxt_i >= len(steps):
                s["last_run"] = {**last, "state": done.get("state"), "job_id": job_id}
                s.pop("recipe", None)
                changed = True
                continue
            step = steps[nxt_i]
            step_verb = str(step["verb"])
            target = dict(s.get("target") or {})
            project = str(target.get("project") or "")
            status = instance_status(project) if step_verb not in _START_STOP else None
            skip = _skip_reason(s, step_verb, data, now, status)
            if skip:
                s["last_skip"] = {"at": _iso(now), "reason": skip}
                if not _waits(skip):
                    s["last_run"] = {**last, "state": "skipped", "at": _iso(now), "reason": skip}
                    s.pop("recipe", None)
                changed = True
                continue
            rec = _fire(
                sid,
                s,
                step_verb,
                {**target, **(step.get("target") or {})},
                step.get("args") or {},
                create_job,
                now,
                {"recipe_step": nxt_i, "recipe_total": len(steps)},
            )
            if rec:
                created.append(rec)
                s["recipe"] = {"job_id": rec.get("id"), "step": nxt_i}
            changed = True
        if changed:
            _save(data)
    return created


def _next_due(s: dict[str, Any], now: datetime) -> datetime | None:
    """The slot the schedule is waiting on: the recorded ``due`` after its last run,
    else the first occurrence after creation, an edit or a re-enable (so it does not
    fire for a slot that passed before then)."""
    base = _parse(s.get("due_from"))
    if s.get("kind") == "once":
        at = _parse(s.get("at"))
        return at if at and (base is None or at > base) else None
    recorded = _parse((s.get("last_run") or {}).get("due"))
    if recorded:
        return recorded
    return compute_next(s, base or _parse(s.get("created_at")) or now)


def _event_fires(
    sid: str, s: dict[str, Any], finished: list[dict[str, Any]], started: set[str], project: str
) -> bool:
    trig = s.get("trigger") or {}
    if trig.get("type") == "on_start":
        return project in started
    if trig.get("type") == "after_job":
        want_project = trig.get("project") or project
        for j in finished:
            if j.get("state") != "succeeded" or j.get("verb") != trig.get("verb"):
                continue
            if (j.get("origin") or {}).get("schedule") == sid:
                continue
            if (j.get("target") or {}).get("project") == want_project:
                return True
    return False


def run_now(sid: str, create_job: CreateJob, *, now: datetime | None = None) -> dict[str, Any]:
    """Start the schedule's job this minute, guards aside (a person asked); the
    timetable is untouched."""
    now = now or _now()
    with _lock, _StoreLock():
        data = _load()
        s = data["schedules"].get(sid)
        if s is None:
            raise CwcliError(ErrorKind.NOT_FOUND, "schedule.unknown", f"No schedule '{sid}'.")
        target = dict(s.get("target") or {})
        steps = s.get("steps") or []
        if steps:
            step = steps[0]
            rec = _fire(
                sid,
                s,
                str(step["verb"]),
                {**target, **(step.get("target") or {})},
                step.get("args") or {},
                create_job,
                now,
                {"recipe_step": 0, "recipe_total": len(steps), "manual": True},
            )
            if rec:
                s["recipe"] = {"job_id": rec.get("id"), "step": 0}
        else:
            rec = _fire(
                sid,
                s,
                str(s.get("verb") or ""),
                target,
                s.get("args") or {},
                create_job,
                now,
                {"manual": True},
            )
        _save(data)
    if rec is None:
        last = s.get("last_skip") or s.get("last_run") or {}
        raise CwcliError(
            ErrorKind.CONFLICT,
            "schedule.not_started",
            str(last.get("reason") or last.get("error") or "the job could not be started"),
        )
    return rec


class _StoreLock:
    """``flock`` on the store, held by every write, so two schedulers (the UI daemon
    and the machine-wide service) never fire the same slot twice and an edit from the
    CLI never races a tick. A platform without flock runs unlocked; a lock that cannot
    be taken refuses the write."""

    def __enter__(self):
        self._fd = None
        try:
            import fcntl
        except ImportError:
            return self
        path = store_path().with_suffix(".lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(path, os.O_RDWR | os.O_CREAT, shared_home.file_mode(0o600))
            shared_home.secure_file_shared_only(path)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError as e:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise CwcliError(
                ErrorKind.PRECONDITION, "schedules.lock_unavailable", f"Cannot lock {path}: {e}"
            ) from e
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
        return False
