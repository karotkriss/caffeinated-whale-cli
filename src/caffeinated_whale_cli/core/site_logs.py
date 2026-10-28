"""Per-site logs for the web UI's site page: the site's own Frappe log files under
``sites/<site>/logs`` (resumable, cursor-based tails like ``core.logs``), and the
site's Error Log entries read from its database. Read-only throughout.

A RUNNING instance is read through the container (one ``stat``/``tail`` exec per
poll, the ``core.logs`` transport); a STOPPED instance's files are read from the
bench on the host bind mount (``~/.cwcli/projects/<p>/data/<bench>``). The Error
Log needs the site's database, so it is only readable while the instance runs; a
stopped one says so instead of guessing.

The Error Log is read with ``bench --site <site> mariadb --batch -e <select>``:
bench opens the client with the site's own credentials, so cwcli handles none,
and ``--batch`` escapes newlines and tabs in the traceback column so one row stays
one line.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import docker as core_docker
from . import resolvers, supervision
from .app_catalog import _host_bench_dir
from .errors import CwcliError, ErrorKind

RESUME_CAP_BYTES = 1024 * 1024
_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.log(\.\d+)?$")
_ERROR_LOG_SQL = (
    "SELECT name, creation, method, LEFT(error, 20000) AS error "
    "FROM `tabError Log` ORDER BY creation DESC LIMIT {limit}"
)


def _target(project: str, bench_path: str):
    """``(container, host_dir)``: the container when the instance runs, else the
    bench's directory on the host bind mount (None, None when neither exists)."""
    container = None
    try:
        container = core_docker.get_frappe_container(project)
    except CwcliError:
        container = None
    if container is not None and getattr(container, "status", "") == "running":
        return container, None
    return None, _host_bench_dir(project, bench_path)


def _check(site: str, file: str | None = None) -> None:
    resolvers.validate_site_name(site)
    if file is not None and not _FILE_RE.match(file):
        raise CwcliError(ErrorKind.USAGE, "site_logs.file_invalid", "Not a site log file name.")


def list_site_logs(project: str, bench_path: str, site: str) -> dict[str, Any]:
    _check(site)
    container, host_dir = _target(project, bench_path)
    files: list[dict[str, Any]] = []
    if container is not None:
        _code, out = container.exec_run(
            [
                "sh",
                "-c",
                'cd "$1/sites/$2/logs" 2>/dev/null && stat -c "%s %n" * 2>/dev/null',
                "sh",
                bench_path,
                site,
            ]
        )
        for line in supervision._decode(out).splitlines():
            size, _sep, name = line.partition(" ")
            if size.isdigit() and _FILE_RE.match(name):
                files.append({"name": name, "size": int(size)})
        source = "container"
    elif host_dir is not None:
        logs_dir = os.path.join(host_dir, "sites", site, "logs")
        try:
            for name in os.listdir(logs_dir):
                if _FILE_RE.match(name):
                    files.append(
                        {"name": name, "size": os.path.getsize(os.path.join(logs_dir, name))}
                    )
        except OSError:
            pass
        source = "host"
    else:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "site_logs.unreachable",
            f"{project} is not running and its bench is not on this host's disk.",
            hint="Start the instance to read the site's logs.",
        )
    files.sort(key=lambda f: (f["name"].split(".log")[0], f["name"]))
    return {"source": source, "running": container is not None, "files": files}


def read_site_log(
    project: str,
    bench_path: str,
    site: str,
    file: str,
    *,
    lines: int = 200,
    after: int | None = None,
) -> dict[str, Any]:
    """A resumable tail of one site log file (``core.logs.read_logs_since``'s contract:
    the last ``lines`` lines cold, only the bytes since ``after`` warm, whole lines only)."""
    _check(site, file)
    container, host_dir = _target(project, bench_path)
    path = f"{bench_path}/sites/{site}/logs/{file}"
    if container is not None:
        _code, out = container.exec_run(["stat", "-c", "%s", path])
        size_text = supervision._decode(out).strip()
        size = int(size_text) if size_text.isdigit() else 0

        def read(start: int, count: int) -> bytes:
            _c, raw = container.exec_run(
                [
                    "sh",
                    "-c",
                    'tail -c +"$1" "$2" | head -c "$3"',
                    "sh",
                    str(start + 1),
                    path,
                    str(count),
                ]
            )
            return bytes(raw or b"")

        def tail(count_lines: int) -> bytes:
            _c, raw = container.exec_run(
                [
                    "sh",
                    "-c",
                    'head -c "$1" "$2" | tail -n "$3"',
                    "sh",
                    str(size),
                    path,
                    str(count_lines),
                ]
            )
            return bytes(raw or b"")

    elif host_dir is not None:
        host_path = os.path.join(host_dir, "sites", site, "logs", file)
        try:
            size = os.path.getsize(host_path)
        except OSError:
            size = 0

        def read(start: int, count: int) -> bytes:
            try:
                with open(host_path, "rb") as f:
                    f.seek(start)
                    return f.read(count)
            except OSError:
                return b""

        def tail(count_lines: int) -> bytes:
            data = read(max(0, size - RESUME_CAP_BYTES), size)
            parts = data.split(b"\n")
            kept = parts[-(count_lines + 1) :]
            out = b"\n".join(kept)
            if len(parts) > count_lines + 1:
                out = out[out.find(b"\n") + 1 :] if size > RESUME_CAP_BYTES else out
            return out

    else:
        raise CwcliError(
            ErrorKind.NOT_FOUND,
            "site_logs.unreachable",
            f"{project} is not running and its bench is not on this host's disk.",
        )

    if after is not None and 0 <= after <= size:
        if after == size:
            return {
                "file": file,
                "lines": [],
                "offset": after,
                "skipped_bytes": 0,
                "source": "container" if container else "host",
            }
        base = max(after, size - RESUME_CAP_BYTES)
        data = read(base, size - base)
        if base > after:
            partial = data.find(b"\n") + 1
            data, base = data[partial:], base + partial
    else:
        data = tail(lines)
        base = size - len(data)
        after = None
    cut = data.rfind(b"\n") + 1
    consumed, data = base + cut, data[:cut]
    skipped = base - after if after is not None and 0 <= after < base else 0
    return {
        "file": file,
        "lines": supervision._decode(data).splitlines(),
        "offset": consumed,
        "skipped_bytes": skipped,
        "source": "container" if container else "host",
    }


_ROTATED = re.compile(r"\.log\.\d+$")


def read_site_logs(
    project: str,
    bench_path: str,
    site: str,
    *,
    lines: int = 60,
    after: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Every LIVE log file of the site together (the default view, captain note 39):
    one resumable tail per current file, rotated copies (``*.log.N``) left to their
    own tabs. ``after`` maps a file name to the cursor its last read returned."""
    listing = list_site_logs(project, bench_path, site)
    tails: dict[str, dict[str, Any]] = {}
    for entry in listing["files"]:
        name = entry["name"]
        if _ROTATED.search(name):
            continue
        cursor = (after or {}).get(name)
        tails[name] = read_site_log(
            project,
            bench_path,
            site,
            name,
            lines=lines,
            after=cursor if isinstance(cursor, int) and cursor >= 0 else None,
        )
    return {**listing, "tails": tails}


_UNESCAPE = {"n": "\n", "t": "\t", "\\": "\\", "0": "\0"}


def _unescape(cell: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(cell):
        c = cell[i]
        if c == "\\" and i + 1 < len(cell) and cell[i + 1] in _UNESCAPE:
            out.append(_UNESCAPE[cell[i + 1]])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_error_log(project: str, bench_path: str, site: str, *, limit: int = 50) -> dict[str, Any]:
    _check(site)
    limit = max(1, min(int(limit), 500))
    container, _host_dir = _target(project, bench_path)
    if container is None:
        raise CwcliError(
            ErrorKind.NOT_RUNNING,
            "site_logs.error_log_needs_db",
            f"{project} is not running, and the Error Log lives in {site}'s database.",
            hint="Start the instance to read it.",
        )
    sql = _ERROR_LOG_SQL.format(limit=limit)
    code, out = container.exec_run(
        ["bench", "--site", site, "mariadb", "--batch", "-e", sql],
        user="frappe",
        workdir=bench_path,
    )
    text = supervision._decode(out)
    if code != 0:
        last = text.strip().splitlines()[-1] if text.strip() else f"exit {code}"
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "site_logs.error_log_unreadable",
            f"Could not read {site}'s Error Log: {last[:200]}",
        )
    rows = [line for line in text.splitlines() if line.strip()]
    entries: list[dict[str, Any]] = []
    if rows:
        header = rows[0].split("\t")
        for line in rows[1:]:
            cells = [_unescape(c) for c in line.split("\t")]
            if len(cells) != len(header):
                continue
            entry = dict(zip(header, cells, strict=True))
            entries.append(
                {
                    "name": entry.get("name"),
                    "creation": entry.get("creation"),
                    "method": entry.get("method") or "",
                    "error": entry.get("error") or "",
                }
            )
    return {"site": site, "limit": limit, "entries": entries}
