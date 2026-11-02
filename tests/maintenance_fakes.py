"""Fakes for ``core.maintenance``'s two container execs (not collected by pytest).

``read_output`` prints exactly what ``core.maintenance._READ_SCRIPT`` prints for a
bench: a ``SENTINEL kind name`` header per block, then the file's bytes, a
``missing``/``unreadable`` marker for a config file that is absent or unreadable.
``probe_output`` prints ``_PROBE_SCRIPT``'s ``site code stopped`` lines.

A test states the bench as plain data (configs, Procfile, records, processes), so
the fake never consults the code under test to decide an answer.
"""

from __future__ import annotations

import json

from caffeinated_whale_cli.core import maintenance

UNREADABLE = object()


def _file_block(kind: str, name: str, value) -> list[str]:
    header = f"{maintenance.SENTINEL} {kind} {name}"
    if value is None:
        return [header, f"{maintenance.SENTINEL} missing -"]
    if value is UNREADABLE:
        return [header, f"{maintenance.SENTINEL} unreadable -"]
    return [header, value if isinstance(value, str) else json.dumps(value)]


def read_output(
    *,
    common=None,
    sites: dict | None = None,
    procfile: str = "web: bench serve --port 8000\n",
    currentsite: str = "",
    version: str = '__version__ = "16.0.0"',
    owners: dict | None = None,
    procs: dict | None = None,
) -> bytes:
    """The read exec's stdout. ``common``/``sites`` values: a dict, a JSON string,
    None (absent) or :data:`UNREADABLE`. ``procs``: pid -> argv list."""
    sentinel = maintenance.SENTINEL
    lines = _file_block("common", "-", common)
    for name, value in (sites or {}).items():
        lines += _file_block("site", name, value)
    lines += [f"{sentinel} procfile -", procfile]
    lines += [f"{sentinel} currentsite -", currentsite]
    lines += [f"{sentinel} version -", version]
    for name, value in (owners or {}).items():
        lines += _file_block("owner", name, value)
    for pid, argv in (procs or {}).items():
        lines += [f"{sentinel} proc {pid}", " ".join(argv)]
    return ("\n" + "\n".join(lines) + "\n").encode()


def is_read(cmd) -> bool:
    return isinstance(cmd, list) and cmd[:2] == ["sh", "-c"] and maintenance.SENTINEL in cmd


def is_probe(cmd) -> bool:
    return isinstance(cmd, list) and cmd[:2] == ["sh", "-c"] and "api/method/ping" in cmd[2]


def probe_output(cmd, answers: dict) -> bytes:
    """``answers``: site -> (code, session_stopped); a site not named answers 200."""
    lines = []
    for site in cmd[5:]:
        code, stopped = answers.get(site, ("200", False))
        lines.append(f"{site} {code} {1 if stopped else 0}")
    return ("\n".join(lines) + "\n").encode()
