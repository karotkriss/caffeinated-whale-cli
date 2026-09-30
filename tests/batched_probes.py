"""Answer cwcli's batched container probes from a per-path test fake.

Not collected by pytest (no ``test_`` prefix). ``list_sites``, bench discovery and
the T2 partial refresh each run ONE ``sh -c <script> sh <paths...>`` exec, and
``core.bench_read``'s full read runs one Python process per bench in one exec. The
unit fakes model a bench as answers to per-path commands (``ls -1 <dir>``, the
per-entry site probe, the ``test -d`` bench check, ``find <root>``), so
:func:`emulate_batched` evaluates each batched script's semantics by issuing those
per-path commands to the SAME fake and assembling the script's output. A fake opts
in with two lines at the top of its ``exec_run``::

    batched = emulate_batched(self, cmd)
    if batched is not None:
        return batched

The sub-commands go back through ``container.exec_run`` (dynamic dispatch), so a
subclass overriding one per-path answer still takes effect. The scripts' real
shell behavior is pinned separately, under a real ``sh``, by
``tests/test_batched_probes.py``.
"""

from __future__ import annotations

import json
import shlex

from caffeinated_whale_cli.core import bench_read, resolvers
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.utils import bench_labels, bench_sites

_SITE_PROBE = shlex.quote(
    'if [ ! -d "$1" ]; then echo NOTASITE; '
    'elif [ -f "$1/site_config.json" ]; then echo SITE; '
    'elif [ -r "$1" ]; then echo NOTASITE; '
    "else echo AMBIGUOUS; fi"
)


def _raw(output) -> bytes:
    raw = output[0] if isinstance(output, tuple) else output
    return raw.encode() if isinstance(raw, str) else raw


def _text(container, cmd, *, workdir=None, strict=False) -> str | None:
    code, out = container.exec_run(cmd, workdir=workdir) if workdir else container.exec_run(cmd)
    if code != 0:
        return None
    try:
        return _raw(out).decode("utf-8", errors="strict" if strict else "replace")
    except UnicodeDecodeError:
        return None


def _is_bench(container, path: str) -> bool:
    check = (
        f'sh -c "test -d {path}/sites && test -d {path}/apps '
        f'&& test -f {path}/sites/common_site_config.json"'
    )
    return container.exec_run(check)[0] == 0


def _site_verdict_lines(container, bench: str) -> list[bytes] | None:
    code, out = container.exec_run(f"ls -1 {bench}/sites")
    if code != 0:
        return None
    lines = []
    for entry in _raw(out).split(b"\n"):
        entry = entry.strip()
        if not entry:
            continue
        entry_dir = f"{bench}/sites/{entry.decode('utf-8', errors='replace')}"
        probe_code, probe_out = container.exec_run(
            f"sh -c {_SITE_PROBE} sh {shlex.quote(entry_dir)}"
        )
        verdict = _raw(probe_out).strip() if probe_code == 0 else b""
        lines.append((verdict or b"AMBIGUOUS") + b"\t" + entry)
    return lines


def _list_sites(container, bench: str):
    lines = _site_verdict_lines(container, bench)
    if lines is None:
        return (3, b"")
    return (0, b"".join(line + b"\n" for line in lines))


def _discover(container, roots: list[str]):
    out = []
    for root in roots:
        # Read find's output whatever its exit status, as the real script does.
        _code, found = container.exec_run(f"find {root} -maxdepth 2 -type d -name 'apps'")
        for line in _raw(found).decode("utf-8", errors="replace").split("\n"):
            bench = line.strip().removesuffix("/apps")
            if line.strip() and _is_bench(container, bench):
                out.append(bench)
    return (0, "".join(f"{b}\n" for b in out).encode())


def _partial_refresh(container, benches: list[str]):
    out: list[bytes] = []
    for bench in benches:
        if not _is_bench(container, bench):
            out.append(f"G\t{bench}".encode())
            continue
        out.append(f"B\t{bench}".encode())
        code, apps = container.exec_run(f"ls -1 {bench}/apps")
        if code == 0:
            out += [b"A\t" + app for app in _raw(apps).strip().split(b"\n") if app]
        out += [b"S\t" + line for line in _site_verdict_lines(container, bench) or []]
    return (0, b"".join(line + b"\n" for line in out))


def _installed(container, bench: str, site: str) -> list[str] | None:
    cmd = f"bench --site {shlex.quote(site)} execute frappe.get_installed_apps"
    out = _text(container, cmd, workdir=bench)
    lines = [line for line in (out or "").splitlines() if line.strip()]
    try:
        apps = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return None
    return apps if isinstance(apps, list) else None


def _full_read(container, args: list[str]):
    """``core.bench_read``'s record per bench, from the fake's per-path answers."""
    _script, opts_json, *benches = args
    opts = json.loads(opts_json)
    lines = []
    for bench in benches:
        record: dict = {"path": bench, "venv": True, "sites": {}}
        if opts["files"]:
            record["apps"] = _text(container, f"ls -1 {shlex.quote(bench + '/apps')}")
            names = bench_read.output_lines(record["apps"] or "")
            if names:
                probe = [f"{bench}/env/bin/python", "-c", resolvers._APP_IMPORT_PROBE, bench]
                record["imports"] = _text(container, [*probe, *dict.fromkeys(names)])
            sites_dir = f"{bench}/sites"
            record["common"] = _text(
                container, f"cat {shlex.quote(sites_dir + '/common_site_config.json')}"
            )
            record["current"] = _text(
                container, f"cat {shlex.quote(sites_dir + '/currentsite.txt')}", strict=True
            )
            marker = f"{bench.rstrip('/')}/{bench_labels.MARKER_REL_PATH}"
            record["marker"] = _text(container, ["cat", marker])
        sites = opts["sites"]
        if sites is None:
            verdicts = _site_verdict_lines(container, bench)
            record["listing"] = (
                None if verdicts is None else b"".join(v + b"\n" for v in verdicts).decode()
            )
            sites = [
                site
                for line in (record["listing"] or "").split("\n")
                if (site := bench_sites.site_from_verdict(line))
            ]
        for site in sites:
            found: dict = {}
            if opts["files"]:
                config = f"{bench}/sites/{site}/site_config.json"
                found["config"] = _text(container, f"cat {shlex.quote(config)}")
            if opts["list_apps"]:
                cmd = f"bench --site {shlex.quote(site)} list-apps"
                found["list_apps"] = _text(container, cmd, workdir=bench)
            if opts["installed"]:
                found["installed"] = _installed(container, bench, site)
            record["sites"][site] = found
        lines.append(bench_read.SENTINEL + json.dumps(record))
    return (0, ("\n" + "\n".join(lines) + "\n").encode())


_SCRIPTS = {
    bench_sites._LIST_SITES_SCRIPT: lambda c, args: _list_sites(c, args[0]),
    core_inspect._DISCOVER_SCRIPT: _discover,
    core_inspect._PARTIAL_REFRESH_SCRIPT: _partial_refresh,
    bench_read._READ_SH: _full_read,
}


def emulate_batched(container, cmd):
    """``(exit_code, output)`` for a batched probe command, else ``None``."""
    if not isinstance(cmd, (list, tuple)) or list(cmd[:2]) != ["sh", "-c"] or len(cmd) < 4:
        return None
    handler = _SCRIPTS.get(cmd[2])
    if handler is None:
        return None
    return handler(container, list(cmd[4:]))
