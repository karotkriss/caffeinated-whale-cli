"""Site database users and the host they are allowed to connect from (steering 041).

Frappe creates each site's MariaDB user host-scoped to the CLIENT it saw at
``bench new-site`` (``DbManager.get_current_host`` reads ``USER()``), which in Docker
is the frappe container's IP. A restart that gives the container a new IP then
refuses every site: ``OperationalError 1045 Access denied for user '_<hash>'@'<new ip>'``
and a bare 500 in the browser (staging16, 2026-10-28). The Frappe docker convention
is ``'<user>'@'%'``, and that is what cwcli now ensures: after every site it creates,
and as a health check after start/restart and the core update that offers the one
repair (``RENAME USER ... TO '<user>'@'%'``).

Everything here runs as the MariaDB root user through the frappe container's own
client (the one ``bench new-site`` uses), with the root password in the exec's
environment (``MYSQL_PWD``), never on an argv.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from . import db_password, supervision
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

OnEvent = Callable[[Any], None]


@dataclass(frozen=True, slots=True)
class SiteAccess:
    site: str
    db_user: str
    hosts: list[str]  # the hosts the user may connect from ('%' is any)
    client_host: str | None  # how MariaDB sees the frappe container now
    ok: bool  # '%' present, or the current client host is
    repaired: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DbAccessStep:
    phase: str
    message: str
    status: str = "start"


def _noop(_e: Any) -> None:
    return None


def _decode(output: Any) -> str:
    raw = output[0] if isinstance(output, tuple) else output
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")


_CLIENT = (
    'C=$(command -v mariadb || command -v mysql) || { echo "no mariadb client" >&2; exit 97; }'
)


def root_sql(container, bench_path: str, sql: str, root_password: str) -> str:
    """Run ``sql`` as MariaDB root from inside the frappe container (``--batch``,
    tab-separated, no header). The password rides in the environment."""
    host = (supervision.common_site_config(container, bench_path) or {}).get("db_host") or "mariadb"
    port = (supervision.common_site_config(container, bench_path) or {}).get("db_port") or 3306
    script = (
        f"{_CLIENT}; $C -h {shlex.quote(str(host))} -P {shlex.quote(str(port))} -u root "
        f"--batch --skip-column-names -e {shlex.quote(sql)}"
    )
    # demux: the client prints advisory WARNING lines on stderr (ssl-verify-server-cert)
    # that must never be parsed as a query answer.
    code, out = container.exec_run(
        ["sh", "-c", script],
        workdir=bench_path,
        environment={"MYSQL_PWD": root_password},
        demux=True,
    )
    stdout, stderr = out if isinstance(out, tuple) else (out, b"")
    text = _decode(stdout)
    if code != 0:
        text = text or _decode(stderr)
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "db.root_query_failed",
            f"MariaDB root query failed: {text.strip()[-300:] or f'exit {code}'}",
            hint="cwcli remembers the root password it set at init; pass --mariadb-root-password otherwise.",
        )
    return text


def site_db_users(container, bench_path: str) -> dict[str, str]:
    """``{site: db_user}`` from every ``sites/*/site_config.json`` (``db_user`` else ``db_name``)."""
    script = (
        "python3 - <<'PY'\nimport glob, json\nout = {}\nfor p in sorted(glob.glob('sites/*/site_config.json')):\n"
        "    try:\n        d = json.load(open(p))\n    except Exception:\n        continue\n"
        "    u = d.get('db_user') or d.get('db_name')\n    if u:\n        out[p.split('/')[1]] = u\n"
        "print(json.dumps(out))\nPY"
    )
    code, out = container.exec_run(["sh", "-c", script], workdir=bench_path)
    if code != 0:
        return {}
    try:
        data = json.loads(_decode(out).strip() or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()}


def check(
    container,
    bench_path: str,
    *,
    project: str,
    root_password: str | None = None,
    sites: list[str] | None = None,
) -> list[SiteAccess]:
    """Which sites' database users cannot connect from where the frappe container is now."""
    pw = db_password.resolve(project, root_password)
    users = site_db_users(container, bench_path)
    if sites:
        users = {s: u for s, u in users.items() if s in sites}
    if not users:
        return []
    client = (
        root_sql(container, bench_path, "SELECT SUBSTRING_INDEX(USER(),'@',-1)", pw).strip() or None
    )
    names = ",".join("'" + u.replace("'", "''") + "'" for u in users.values())
    rows = root_sql(
        container, bench_path, f"SELECT user, host FROM mysql.user WHERE user IN ({names})", pw
    )
    hosts: dict[str, list[str]] = {}
    for line in rows.splitlines():
        if "\t" in line:
            u, h = line.split("\t", 1)
            hosts.setdefault(u, []).append(h)
    out = []
    for site, user in users.items():
        allowed = hosts.get(user, [])
        ok = "%" in allowed or (client is not None and client in allowed)
        out.append(SiteAccess(site, user, allowed, client, ok))
    return out


def repair(
    container,
    bench_path: str,
    *,
    project: str,
    root_password: str | None = None,
    sites: list[str] | None = None,
    on_event: OnEvent | None = None,
    force: bool = False,
) -> Result[list[SiteAccess]]:
    """Make every mismatched site user ``'<user>'@'%'``: the first host-scoped row is
    renamed, any other dropped, privileges flushed. Already-ok sites are untouched
    unless ``force`` (a site cwcli just created is widened even though its user can
    connect from the current address: the next IP change must not break it)."""
    emit = on_event or _noop
    pw = db_password.resolve(project, root_password)
    found = check(container, bench_path, project=project, root_password=root_password, sites=sites)
    out: list[SiteAccess] = []
    warnings: list[Message] = []
    for a in found:
        if (a.ok and not force) or not a.hosts or a.hosts == ["%"]:
            out.append(a)
            continue
        emit(
            DbAccessStep(
                "db_access",
                f"{a.site}: {a.db_user} may connect only from {', '.join(a.hosts)}; renaming to '%'",
            )
        )
        q = a.db_user.replace("'", "''")
        first, rest = a.hosts[0], a.hosts[1:]
        statements = [f"RENAME USER '{q}'@'{first}' TO '{q}'@'%'"]
        statements += [f"DROP USER '{q}'@'{h}'" for h in rest]
        statements.append("FLUSH PRIVILEGES")
        try:
            root_sql(container, bench_path, "; ".join(statements), pw)
        except CwcliError as exc:
            warnings.append(Message("db.repair_failed", f"{a.site}: {exc.message}"))
            emit(DbAccessStep("db_access", f"{a.site}: repair failed - {exc.message}", "failed"))
            out.append(a)
            continue
        emit(DbAccessStep("db_access", f"{a.site}: {a.db_user} now connects from any host", "ok"))
        out.append(SiteAccess(a.site, a.db_user, ["%"], a.client_host, True, repaired=True))
    return Result(status=Status.WARNING if warnings else Status.OK, data=out, warnings=warnings)


def widen_after_create(
    container,
    bench_path: str,
    *,
    project: str,
    site: str,
    root_password: str | None = None,
) -> tuple[bool, Message | None]:
    """The one post-creation hook every site-creating path calls (``init``, the web
    UI's new site, ``restore`` - bench re-creates the user on ``restore --force`` too):
    ``(widened, warning)``. A failure to widen is a warning, never a failed creation."""
    try:
        widened = ensure_wildcard(
            container, bench_path, project=project, site=site, root_password=root_password
        )
    except CwcliError as e:
        return False, Message(
            "db.widen_failed", f"{site}: its database user stays host-scoped ({e.message})"
        )
    return widened is not None and widened.repaired, None


def ensure_wildcard(
    container,
    bench_path: str,
    *,
    project: str,
    site: str,
    root_password: str | None = None,
    on_event: OnEvent | None = None,
) -> SiteAccess | None:
    """After cwcli creates a site: widen its freshly created host-scoped user to '%'."""
    result = repair(
        container,
        bench_path,
        project=project,
        root_password=root_password,
        sites=[site],
        on_event=on_event,
        force=True,
    )
    return next((a for a in (result.data or []) if a.site == site), None)


def login_refused_warning(
    project: str, mismatches: list[SiteAccess], bench_flag: str = ""
) -> Message:
    sites = ", ".join(a.site for a in mismatches)
    detail = "; ".join(
        f"{a.site}: {a.db_user} allowed from {', '.join(a.hosts) or 'nowhere'}, the container is {a.client_host}"
        for a in mismatches
    )
    return Message(
        "db.login_refused",
        f"database login refused for site{'s' if len(mismatches) != 1 else ''} {sites}: the site's "
        f"database user is host-scoped to an address the frappe container no longer has ({detail}). "
        f"Repair with: cwcli db-access repair {shlex.quote(project)}{bench_flag} --yes",
        detail={"sites": [a.to_dict() for a in mismatches]},
    )


def postflight(
    container,
    bench_path: str,
    *,
    project: str,
    sites: list[str] | None = None,
    bench_flag: str = "",
) -> Message | None:
    """The health check after start/restart and the core update: a warning naming
    every site whose database login would be refused, or None. Never raises - a check
    that cannot run is a warning of its own."""
    try:
        found = check(container, bench_path, project=project, sites=sites)
    except CwcliError as exc:
        return Message(
            "db.access_unverified",
            f"cwcli could not check the sites' database users: {exc.message}",
        )
    bad = [a for a in found if not a.ok]
    return login_refused_warning(project, bad, bench_flag) if bad else None
