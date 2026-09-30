"""Answer cwcli's batched container probes from a per-path test fake.

Not collected by pytest (no ``test_`` prefix). ``list_sites``, bench discovery and
the T2 partial refresh each run ONE ``sh -c <script> sh <paths...>`` exec. The
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

import shlex

from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.utils import bench_sites

_SITE_PROBE = shlex.quote(
    'if [ ! -d "$1" ]; then echo NOTASITE; '
    'elif [ -f "$1/site_config.json" ]; then echo SITE; '
    'elif [ -r "$1" ]; then echo NOTASITE; '
    "else echo AMBIGUOUS; fi"
)


def _raw(output) -> bytes:
    return output[0] if isinstance(output, tuple) else output


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


_SCRIPTS = {
    bench_sites._LIST_SITES_SCRIPT: lambda c, args: _list_sites(c, args[0]),
    core_inspect._DISCOVER_SCRIPT: _discover,
    core_inspect._PARTIAL_REFRESH_SCRIPT: _partial_refresh,
}


def emulate_batched(container, cmd):
    """``(exit_code, output)`` for a batched probe command, else ``None``."""
    if not isinstance(cmd, (list, tuple)) or list(cmd[:2]) != ["sh", "-c"] or len(cmd) < 4:
        return None
    handler = _SCRIPTS.get(cmd[2])
    if handler is None:
        return None
    return handler(container, list(cmd[4:]))
