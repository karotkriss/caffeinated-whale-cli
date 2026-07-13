"""``core.supervision`` - the shared read-side process-supervision substrate.

This is the ONE tracked-state contract that :mod:`core.start` writes and
:mod:`core.status` reads, so the two verbs always agree on which bench they act
on and what "running" means (openspec ``migrate-start-status-core``, D1/D2). cwcli
is a short-lived CLI reaching long-lived in-container processes only through
``docker exec``, so it can never be the live PARENT of the bench stack; it owns
the READ side only. bench keeps launching its own ``honcho`` supervisor; this
module observes it.

What lives here:

- :func:`discover_stack` - ONE ``ps`` in the frappe container mapping each PID to
  its Procfile label from its self-describing cmdline, keyed to a RESOLVED bench
  path so a multi-bench instance never mis-attributes another bench's processes.
- :func:`expected_labels` - the live ``Procfile`` parse (which labels SHOULD run).
- :func:`read_marker` / :func:`write_marker` - the minimal supervisor marker that
  distinguishes "started, supervisor now down" (marker present, honcho absent)
  from "never started" (no marker); honcho writes no pidfile, so pure discovery
  cannot make this distinction.
- :func:`bench_start_log_path` / :func:`launch` / :func:`web_http_code` /
  :func:`per_process_log_lines` - the persisted, size-bounded bench-start log on
  the workspace volume, the detached ``bench start`` launch, the retained curl web
  probe, and honcho's ``HH:MM:SS name|`` prefix parse for per-process log views.

No ``rich``/``questionary``/``typer`` (a unit test enforces the ban), and the
frappe ``Container`` object stays INTERNAL - it is passed in for exec calls and is
never returned across a boundary; every return is plain serializable data.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# The captured honcho stream + the tracked-state files all live under the bench's
# own ``logs/`` dir, which sits on the frappe_docker workspace volume, so they
# survive a container restart (unlike the old ephemeral ``/tmp/bench-<p>.log``).
_LOG_NAME = "bench-start.log"
_MARKER_NAME = ".cwcli-supervisor.json"
_CAPPER_NAME = ".cwcli-logcap.py"

SUPERVISOR = "honcho"

# The per-run byte cap for the captured log (D2): a real (re)launch truncates the
# log; this bounds it WITHIN a run without a daemon. When the live segment reaches
# the cap it is rotated to ``<log>.1`` and a fresh segment is opened, so the file
# set is hard-bounded at ~2x this value while the most recent output is always in
# the live segment ``cwcli logs`` tails.
_LOG_CAP_BYTES = 5 * 1024 * 1024

# The in-container log capper (D2). Streamed as honcho's stdout sink: it truncates
# the log on (re)launch (open "w"), hard-bounds it within a run by rotating at the
# cap, preserves honcho's line prefix (lines pass through verbatim), and NEVER
# exits early or lets a write error propagate - draining stdin unconditionally so
# it can never SIGPIPE honcho. Uses only python3 (guaranteed in the frappe image),
# no new dependency, no cwcli daemon. Deliberately single-quote-free so it can be
# written to the container with a list-form exec (no shell quoting). Unit-tested
# on the host (it is a self-contained python3 program).
_LOG_CAPPER_SRC = (
    "import sys, os\n"
    "log = sys.argv[1]\n"
    "cap = int(sys.argv[2])\n"
    "size = 0\n"
    "def _open():\n"
    "    try:\n"
    '        return open(log, "w")\n'
    "    except Exception:\n"
    "        return None\n"
    "f = _open()\n"
    "for line in sys.stdin:\n"
    '    n = len(line.encode("utf-8", "replace"))\n'
    "    if f is not None and size + n > cap:\n"
    "        try:\n"
    "            f.close()\n"
    '            os.replace(log, log + ".1")\n'
    "        except Exception:\n"
    "            pass\n"
    "        f = _open()\n"
    "        size = 0\n"
    "    if f is not None:\n"
    "        try:\n"
    "            f.write(line)\n"
    "            f.flush()\n"
    "        except Exception:\n"
    "            pass\n"
    "    size += n\n"
)

# A tiny writer used to drop a controlled file into the container via a LIST-form
# exec, so neither the destination path nor the payload passes through a shell.
_WRITE_FILE_PROG = (
    "import sys, os\n"
    "p = sys.argv[1]\n"
    "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
    'open(p, "w").write(sys.argv[2])\n'
)

# honcho prefixes every multiplexed line with ``HH:MM:SS name| `` (honcho/printer).
_HONCHO_PREFIX_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\s+(?P<name>\S+?)\s*\|\s?(?P<rest>.*)$")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessHealth:
    """Per-process liveness + resources, from one ``ps`` (serializable, no live obj)."""

    label: str
    up: bool
    pid: int | None = None
    uptime_s: int | None = None
    cpu_pct: float | None = None
    rss_kb: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StackSnapshot:
    """The discovered honcho stack for one resolved bench."""

    supervisor_up: bool
    supervisor_pid: int | None
    processes: list[ProcessHealth]


@dataclass(frozen=True, slots=True)
class _PsRow:
    pid: int
    ppid: int
    etimes: int | None
    cpu: float | None
    rss: int | None
    args: str


# --------------------------------------------------------------------------- paths


def bench_start_log_path(bench_path: str) -> str:
    """The single-source-of-truth captured-log path for a bench (``cwcli logs`` reads this)."""
    return f"{bench_path}/logs/{_LOG_NAME}"


def _marker_path(bench_path: str) -> str:
    return f"{bench_path}/logs/{_MARKER_NAME}"


def _capper_path(bench_path: str) -> str:
    return f"{bench_path}/logs/{_CAPPER_NAME}"


def _decode(output) -> str:
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", errors="replace")
    return str(output) if output is not None else ""


# ---------------------------------------------------------------------- discovery


def _ps_rows(container) -> list[_PsRow]:
    """One ``ps`` in the container -> parsed rows (pid, ppid, etimes, cpu, rss, args)."""
    exit_code, output = container.exec_run(["ps", "-eo", "pid=,ppid=,etimes=,pcpu=,rss=,args="])
    if exit_code not in (0, None):
        return []
    rows: list[_PsRow] = []
    for line in _decode(output).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid_s, ppid_s, etimes_s, cpu_s, rss_s, args = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        rows.append(
            _PsRow(
                pid=pid,
                ppid=ppid,
                etimes=_int_or_none(etimes_s),
                cpu=_float_or_none(cpu_s),
                rss=_int_or_none(rss_s),
                args=args,
            )
        )
    return rows


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _is_honcho(args: str) -> bool:
    return "honcho" in args and " start" in args


def _resolve_cwds(container, pids: list[int]) -> dict[int, str]:
    """``readlink /proc/<pid>/cwd`` for the given pids, in one exec (empty on miss)."""
    if not pids:
        return {}
    joined = " ".join(str(p) for p in pids)
    script = (
        f"for p in {joined}; do "
        'printf "%s\\t%s\\n" "$p" "$(readlink /proc/$p/cwd 2>/dev/null)"; '
        "done"
    )
    exit_code, output = container.exec_run(["sh", "-c", script])
    cwds: dict[int, str] = {}
    if exit_code not in (0, None):
        return cwds
    for line in _decode(output).splitlines():
        if "\t" not in line:
            continue
        pid_s, cwd = line.split("\t", 1)
        pid = _int_or_none(pid_s.strip())
        if pid is not None and cwd.strip():
            cwds[pid] = cwd.strip()
    return cwds


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.rstrip("/") == b.rstrip("/")


def _honcho_pids_for_bench(container, rows: list[_PsRow], bench_path: str) -> list[int]:
    """The honcho supervisor PID(s) whose bench is ``bench_path`` (keyed by ``-f`` arg or cwd)."""
    candidates = [r for r in rows if _is_honcho(r.args)]
    if not candidates:
        return []

    matched: list[int] = []
    need_cwd: list[int] = []
    for r in candidates:
        # honcho launched with ``-f <bench>/Procfile`` is self-describing.
        f_dir = _procfile_dir_from_args(r.args)
        if f_dir is not None:
            if _same_path(f_dir, bench_path):
                matched.append(r.pid)
        else:
            need_cwd.append(r.pid)

    if need_cwd:
        # bench's default ``bench start`` runs honcho with cwd == bench_path.
        cwds = _resolve_cwds(container, need_cwd)
        for pid in need_cwd:
            if _same_path(cwds.get(pid), bench_path):
                matched.append(pid)
    return matched


def _procfile_dir_from_args(args: str) -> str | None:
    """If honcho was launched ``-f <dir>/Procfile``, return ``<dir>``; else None."""
    tokens = args.split()
    for i, tok in enumerate(tokens):
        if tok in ("-f", "--procfile") and i + 1 < len(tokens):
            path = tokens[i + 1]
            if "/" in path:
                return path.rsplit("/", 1)[0]
            return "."
    return None


def _descendants(rows: list[_PsRow], roots: set[int]) -> set[int]:
    """All PIDs reachable from ``roots`` following ``ppid`` links (the process tree)."""
    children: dict[int, list[int]] = {}
    for r in rows:
        children.setdefault(r.ppid, []).append(r.pid)
    seen: set[int] = set(roots)
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def label_for(args: str) -> str | None:
    """Map a self-describing cmdline to its Procfile label (honcho excluded)."""
    if "socketio" in args:
        return "socketio"
    if "redis_cache" in args:
        return "redis_cache"
    if "redis_queue" in args:
        return "redis_queue"
    if "redis-server" in args:
        return "redis"
    if "bench worker" in args or "frappe worker" in args:
        queue = _queue_from_args(args)
        return f"worker:{queue}" if queue else "worker"
    if "bench schedule" in args:
        return "schedule"
    if "bench watch" in args:
        return "watch"
    if "bench serve" in args or "gunicorn" in args:
        return "web"
    return None


def _queue_from_args(args: str) -> str | None:
    tokens = args.split()
    for i, tok in enumerate(tokens):
        if tok == "--queue" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--queue="):
            return tok.split("=", 1)[1]
    return None


def discover_stack(container, bench_path: str) -> StackSnapshot:
    """Discover the honcho stack for ``bench_path`` from one ``ps`` (keyed to the bench).

    Returns ``supervisor_up`` (a honcho for this bench is live), its pid, and the
    label-mapped live child processes with uptime/CPU/RSS. On a multi-bench
    instance this reports ONLY the requested bench's processes.
    """
    rows = _ps_rows(container)
    honcho_pids = _honcho_pids_for_bench(container, rows, bench_path)
    if not honcho_pids:
        return StackSnapshot(supervisor_up=False, supervisor_pid=None, processes=[])

    tree = _descendants(rows, set(honcho_pids))
    processes: list[ProcessHealth] = []
    for r in rows:
        if r.pid not in tree or r.pid in honcho_pids:
            continue
        label = label_for(r.args)
        if label is None:
            continue
        processes.append(
            ProcessHealth(
                label=label,
                up=True,
                pid=r.pid,
                uptime_s=r.etimes,
                cpu_pct=r.cpu,
                rss_kb=r.rss,
            )
        )
    return StackSnapshot(supervisor_up=True, supervisor_pid=honcho_pids[0], processes=processes)


def stop_supervisor(container, bench_path: str, *, timeout: float = 15.0) -> bool:
    """Terminate the honcho supervisor for ``bench_path`` by its DISCOVERED PID.

    ``SIGTERM`` to honcho triggers its own "one dies, all die" teardown of the
    stack. Waits (bounded) for the tree to actually exit before returning, so a
    caller relaunching does not race a still-running honcho on the container
    ports; escalates to ``SIGKILL`` if it overstays. Keyed to the bench PID (NOT
    the old ``pkill -f 'bench start'``, which never matched honcho). Returns True
    if a supervisor was found and signalled.
    """
    pids = _honcho_pids_for_bench(container, _ps_rows(container), bench_path)
    if not pids:
        return False
    joined = " ".join(str(p) for p in pids)
    container.exec_run(["sh", "-c", f"kill -TERM {joined} 2>/dev/null || true"])

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _honcho_pids_for_bench(container, _ps_rows(container), bench_path):
            return True
        time.sleep(0.5)

    container.exec_run(["sh", "-c", f"kill -KILL {joined} 2>/dev/null || true"])
    return True


def expected_labels(container, bench_path: str) -> list[str]:
    """The labels a bench's live ``Procfile`` defines (the expected-to-run set)."""
    quoted = shlex.quote(f"{bench_path}/Procfile")
    exit_code, output = container.exec_run(["sh", "-c", f"cat {quoted}"])
    if exit_code not in (0, None):
        return []
    labels: list[str] = []
    for line in _decode(output).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        if not key:
            continue
        labels.append(_normalize_procfile_key(key))
    return labels


def _normalize_procfile_key(key: str) -> str:
    """Align a Procfile key with discovery's labels (``worker_<q>`` -> ``worker:<q>``)."""
    if key.startswith("worker_"):
        return "worker:" + key[len("worker_") :]
    return key


# ------------------------------------------------------------------- marker (state)


def read_marker(container, bench_path: str) -> dict | None:
    """The supervisor marker for a bench, or None if it was never started."""
    exit_code, output = container.exec_run(["cat", _marker_path(bench_path)])
    if exit_code not in (0, None):
        return None
    text = _decode(output).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(container, bench_path: str) -> str:
    """Write the launch marker (``supervisor``/``started_at``/``log_path``); return log path."""
    log_path = bench_start_log_path(bench_path)
    payload = json.dumps(
        {
            "supervisor": SUPERVISOR,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "log_path": log_path,
        }
    )
    container.exec_run(["python3", "-c", _WRITE_FILE_PROG, _marker_path(bench_path), payload])
    return log_path


# -------------------------------------------------------------------------- launch


def web_http_code(container) -> str | None:
    """The web server's HTTP code on :8000 (today's curl probe), or None if unreachable."""
    exit_code, output = container.exec_run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:8000"]
    )
    if exit_code not in (0, None):
        return None
    code = _decode(output).strip()
    return code or None


def launch(container, bench_path: str) -> str:
    """Launch ``bench start`` detached, streaming into the bounded log; return its path.

    Writes the log capper into the bench, then starts honcho (via ``bench start``)
    with cwd == ``bench_path`` (so discovery can key it to this bench) piped through
    the capper. Truncate-on-launch happens inside the capper (it opens the log
    ``"w"``). The exec is detached; ``nohup`` keeps honcho alive past it.
    """
    log_path = bench_start_log_path(bench_path)
    capper_path = _capper_path(bench_path)
    # 1. Drop the capper into the bench (list-form exec: no shell, no quoting risk).
    container.exec_run(["python3", "-c", _WRITE_FILE_PROG, capper_path, _LOG_CAPPER_SRC])
    # 2. Launch honcho detached, piping its combined stream through the capper.
    b = shlex.quote(bench_path)
    log = shlex.quote(log_path)
    capper = shlex.quote(capper_path)
    cmd = (
        f"cd {b} && mkdir -p logs && "
        f"nohup bench start 2>&1 | python3 {capper} {log} {_LOG_CAP_BYTES} &"
    )
    container.exec_run(["bash", "-c", cmd], detach=True)
    return log_path


# --------------------------------------------------------------- per-process logs


def per_process_log_lines(log_text: str, label: str) -> list[str]:
    """Select a label's lines from honcho's combined, prefixed stream (D2)."""
    out: list[str] = []
    for line in log_text.splitlines():
        m = _HONCHO_PREFIX_RE.match(line)
        if m and m.group("name") == label:
            out.append(m.group("rest"))
    return out


def _self_check() -> None:
    """Runnable check: the capper bounds the file and keeps recent output; labels map."""
    import os
    import subprocess
    import sys
    import tempfile

    # Label mapping covers every Procfile process shape.
    assert label_for("/env/bin/python /env/bin/bench serve --port 8000") == "web"
    assert label_for("node /apps/frappe/socketio.js") == "socketio"
    assert label_for("/env/bin/bench schedule") == "schedule"
    assert label_for("/env/bin/bench watch") == "watch"
    assert label_for("/env/bin/bench worker") == "worker"
    assert label_for("/env/bin/bench worker --queue short") == "worker:short"
    assert label_for("redis-server /w/b/config/redis_cache.conf") == "redis_cache"
    assert label_for("redis-server /w/b/config/redis_queue.conf") == "redis_queue"
    assert label_for("/env/bin/python /env/bin/honcho start") is None
    assert _normalize_procfile_key("worker_short") == "worker:short"

    # honcho prefix parse -> per-process view.
    combined = "10:00:01 web    | GET /\n10:00:02 worker | job done\n10:00:03 web    | GET /x\n"
    assert per_process_log_lines(combined, "web") == ["GET /", "GET /x"]

    # The capper hard-bounds the log and keeps the most recent output.
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "bench-start.log")
        cap = 200
        payload = "".join(f"line-{i:04d} some honcho output here\n" for i in range(400))
        subprocess.run(
            [sys.executable, "-c", _LOG_CAPPER_SRC, log, str(cap)],
            input=payload,
            text=True,
            check=True,
        )
        assert os.path.getsize(log) <= cap, os.path.getsize(log)
        tail = open(log).read()
        assert "line-0399" in tail, "capper must keep the most recent output"

    print("supervision self-check OK")


if __name__ == "__main__":
    _self_check()
