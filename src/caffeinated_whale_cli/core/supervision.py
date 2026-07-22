"""``core.supervision`` - the shared in-container process-supervision substrate.

cwcli is a short-lived CLI reaching long-lived in-container processes only
through ``docker exec``, so it can never be the live PARENT of the bench stack;
it owns the READ side plus discrete mutations. bench's own dev launcher runs the
Procfile under ``honcho``, which is all-or-nothing (one child dies, it tears the
rest down and never restarts one), so per-process restart and auto-heal are
impossible under it. This module instead launches **supervisord** inside the
container over the SAME dev Procfile commands (openspec ``add-per-process-supervisor``,
reversing ``migrate-start-status-core`` D1), which gives per-process restart,
``autorestart`` self-heal, crash-loop ``FATAL`` surfacing, and per-process log
files, while keeping cwcli stateless between calls (it cold-re-discovers the
supervisord on every invocation, exactly as it did honcho).

What lives here:

- :func:`discover_stack` - ONE ``ps`` in the frappe container mapping each PID to
  its Procfile label from its self-describing cmdline, keyed to a RESOLVED bench
  path so a multi-bench instance never mis-attributes another bench's processes.
- :func:`discover_unsupervised_stack` - the ``status`` FALLBACK for an instance
  cwcli's supervisord did not start (pre-v3, or a plain ``bench start``): the same
  ``ps``/``label_for`` machinery walks the honcho manager's tree instead, so the
  truly-running processes are reported up rather than a false all-down. Detection
  is READ-ONLY - it never launches supervisord (that is ``cwcli start``'s job).
- :func:`expected_labels` / :func:`procfile_programs` - the live ``Procfile``
  parse (which labels/programs SHOULD run), normalized and raw.
- :func:`read_marker` / :func:`write_marker` - the minimal supervisor marker that
  distinguishes "started, supervisor now down" (marker present, supervisord
  absent) from "never started" (no marker), and records the config path detection
  keys on.
- :func:`ensure_supervisor_installed` / :func:`launch` / :func:`stop_supervisor` -
  the idempotent ``pip install supervisor`` bootstrap (fail-closed), the detached
  supervisord launch over a generated config, and the PID-based teardown.
- :func:`supervisorctl_states` / :func:`restart_program` - supervisord's
  authoritative per-program state (RUNNING/STARTING/BACKOFF/EXITED/FATAL/STOPPED)
  and the single-program restart primitive.
- :func:`process_log_path` / :func:`logs_dir` - the per-process log files
  (supervisord ``stdout_logfile`` + built-in rotation, replacing honcho's
  combined-stream capper); ``commands/logs.py`` tails one or all of them (the
  multi-file tail is the combined view).

No ``rich``/``questionary``/``typer`` (a unit test enforces the ban), and the
frappe ``Container`` object stays INTERNAL - it is passed in for exec calls and is
never returned across a boundary; every return is plain serializable data.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# All cwcli supervision state lives under the bench's own ``logs/`` dir, which
# sits on the frappe_docker workspace volume, so it survives a container restart.
_MARKER_NAME = ".cwcli-supervisor.json"
_CONFIG_NAME = ".cwcli-supervisor.conf"
_LAUNCHER_NAME = ".cwcli-run.sh"
_SOCK_NAME = ".cwcli-supervisor.sock"
_SUPERVISORD_LOG_NAME = ".cwcli-supervisord.log"
_SUPERVISORD_PID_NAME = ".cwcli-supervisord.pid"
# Per-program supervisord logs are named ``<program>.supervisor.log`` under logs/.
_PROC_LOG_SUFFIX = ".supervisor.log"

SUPERVISOR = "supervisord"

# Per-program supervisord log rotation (built into supervisord, no cwcli daemon).
_PROC_LOG_MAXBYTES = "5MB"
_PROC_LOG_BACKUPS = 1

# The in-container program launcher (openspec ``add-per-process-supervisor`` D2/D7).
# supervisord execs its ``command`` directly (no shell), but the frappe dev Procfile
# lines rely on shell semantics (PATH resolution, redirections like
# ``bench worker 1>> logs/worker.log``, and honcho's ``.env`` auto-load). This tiny
# launcher restores honcho-equivalent behavior for ONE program: source ``.env`` if
# present, pull that program's command line out of the live Procfile, and ``exec``
# it (so the final process replaces the launcher and supervisord tracks the real
# PID). Keeping the command in the Procfile - not baked into the INI - sidesteps all
# supervisord config quoting. Deliberately single-quote-free so it can be written to
# the container with a list-form exec (no shell quoting). Runs with cwd == the bench
# (supervisord's per-program ``directory=``).
_LAUNCHER_SRC = (
    "#!/usr/bin/env bash\n"
    "# cwcli per-program launcher (honcho-equivalent env). Usage: <this> <program>\n"
    "set -a\n"
    "[ -f .env ] && . ./.env 2>/dev/null || true\n"
    "set +a\n"
    'name="$1"\n'
    'line=$(grep -E "^[[:space:]]*${name}:" Procfile | head -n1)\n'
    "cmd=${line#*:}\n"
    'exec bash -c "$cmd"\n'
)

# A tiny writer used to drop a controlled file into the container via a LIST-form
# exec, so neither the destination path nor the payload passes through a shell.
_WRITE_FILE_PROG = (
    "import sys, os\n"
    "p = sys.argv[1]\n"
    "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
    'open(p, "w").write(sys.argv[2])\n'
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessHealth:
    """Per-process liveness + resources + supervisord state (serializable, no live obj)."""

    label: str
    up: bool
    pid: int | None = None
    uptime_s: int | None = None
    cpu_pct: float | None = None
    rss_kb: int | None = None
    state: str | None = None  # supervisord state: RUNNING/STARTING/BACKOFF/EXITED/FATAL/STOPPED


@dataclass(frozen=True, slots=True, kw_only=True)
class StackSnapshot:
    """The discovered supervisord stack for one resolved bench."""

    supervisor_up: bool
    supervisor_pid: int | None
    processes: list[ProcessHealth]


@dataclass(frozen=True, slots=True, kw_only=True)
class UnsupervisedStack:
    """A bench whose Procfile runs under a NON-cwcli manager (honcho / ``bench start``).

    The fallback for an instance cwcli's supervisord did not start - a pre-v3
    instance, or one launched with a plain ``bench start`` - so ``status`` reports
    the truly-running processes instead of a false all-down. ``manager_up`` is True
    iff a honcho/bench-start manager rooted at this bench is live; ``processes`` is
    the label-mapped live children (up/pid/uptime from ``ps`` only - there is no
    supervisord ``state`` to read here).
    """

    manager_up: bool
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


def logs_dir(bench_path: str) -> str:
    """The bench ``logs/`` dir where per-process supervisord logs + cwcli state live."""
    return f"{bench_path}/logs"


def process_log_path(bench_path: str, program: str) -> str:
    """The per-program supervisord ``stdout_logfile`` path for ``program``."""
    return f"{bench_path}/logs/{program}{_PROC_LOG_SUFFIX}"


def _marker_path(bench_path: str) -> str:
    return f"{bench_path}/logs/{_MARKER_NAME}"


def _config_path(bench_path: str) -> str:
    return f"{bench_path}/logs/{_CONFIG_NAME}"


def _launcher_path(bench_path: str) -> str:
    return f"{bench_path}/logs/{_LAUNCHER_NAME}"


def _sock_path(bench_path: str) -> str:
    return f"{bench_path}/logs/{_SOCK_NAME}"


def _venv_python(bench_path: str) -> str:
    """The bench virtualenv's python (supervisor is installed + run through it)."""
    return f"{bench_path}/env/bin/python"


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


def _is_supervisord(args: str) -> bool:
    """A supervisord supervisor process (its config ``-c`` arg keys it to a bench)."""
    return "supervisord" in args


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


def _config_from_args(args: str) -> str | None:
    """If supervisord was launched ``-c <path>``, return ``<path>``; else None."""
    tokens = args.split()
    for i, tok in enumerate(tokens):
        if tok in ("-c", "--configuration") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--configuration="):
            return tok.split("=", 1)[1]
    return None


def _supervisord_pids_for_bench(container, rows: list[_PsRow], bench_path: str) -> list[int]:
    """The supervisord PID(s) whose bench is ``bench_path`` (keyed by ``-c`` config or cwd)."""
    candidates = [r for r in rows if _is_supervisord(r.args)]
    if not candidates:
        return []

    want = _config_path(bench_path)
    matched: list[int] = []
    need_cwd: list[int] = []
    for r in candidates:
        # supervisord launched with ``-c <bench>/logs/.cwcli-supervisor.conf`` is
        # self-describing; its config path encodes the exact bench.
        cfg = _config_from_args(r.args)
        if cfg is not None:
            if _same_path(cfg, want):
                matched.append(r.pid)
        else:
            need_cwd.append(r.pid)

    if need_cwd:
        cwds = _resolve_cwds(container, need_cwd)
        for pid in need_cwd:
            if _same_path(cwds.get(pid), bench_path):
                matched.append(pid)
    return matched


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
    """Map a self-describing cmdline to its Procfile label (supervisor/launcher excluded).

    A Procfile line's ``bench <cmd>`` resolves at runtime to the actual worker
    process ``python -m frappe.utils.bench_helper frappe <cmd>``, so once the
    ``bench``/``sh -c`` wrapper execs away the live cmdline reads ``frappe serve``
    /``frappe schedule``/``frappe watch``/``frappe worker`` - NOT ``bench serve``.
    Match BOTH forms, or a genuinely-running web/watch/schedule reads as down.
    """
    if "supervisord" in args:
        return None
    if _LAUNCHER_NAME in args:
        # The launcher bash normally execs away, but guard the transient case.
        return None
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
    if "bench schedule" in args or "frappe schedule" in args:
        return "schedule"
    if "bench watch" in args or "frappe watch" in args:
        return "watch"
    if "bench serve" in args or "frappe serve" in args or "gunicorn" in args:
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
    """Discover the supervisord stack for ``bench_path`` from one ``ps`` (keyed to the bench).

    Returns ``supervisor_up`` (a supervisord for this bench is live), its pid, and
    the label-mapped live child processes with uptime/CPU/RSS. On a multi-bench
    instance this reports ONLY the requested bench's processes. Per-program
    supervisord STATE (RUNNING/BACKOFF/FATAL) is read separately via
    :func:`supervisorctl_states`; discovery here is the cheap ``ps`` liveness read.
    """
    rows = _ps_rows(container)
    sup_pids = _supervisord_pids_for_bench(container, rows, bench_path)
    if not sup_pids:
        return StackSnapshot(supervisor_up=False, supervisor_pid=None, processes=[])

    tree = _descendants(rows, set(sup_pids))
    processes: list[ProcessHealth] = []
    for r in rows:
        if r.pid not in tree or r.pid in sup_pids:
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
    return StackSnapshot(supervisor_up=True, supervisor_pid=sup_pids[0], processes=processes)


def _is_process_manager(args: str) -> bool:
    """A NON-cwcli Procfile manager (honcho, which is what ``bench start`` runs)."""
    return "honcho" in args


def _manager_pids_for_bench(container, rows: list[_PsRow], bench_path: str) -> list[int]:
    """PIDs of a honcho / ``bench start`` manager rooted at ``bench_path`` (keyed by cwd).

    honcho (bench's own dev launcher) runs with cwd == the bench dir and has no
    cwcli config/socket to key on, so a multi-bench instance is disambiguated by
    ``/proc/<pid>/cwd`` - exactly the cwd fallback the supervisord keying already uses.
    """
    candidates = [r for r in rows if _is_process_manager(r.args)]
    if not candidates:
        return []
    cwds = _resolve_cwds(container, [r.pid for r in candidates])
    return [r.pid for r in candidates if _same_path(cwds.get(r.pid), bench_path)]


def discover_unsupervised_stack(container, bench_path: str) -> UnsupervisedStack:
    """Discover a bench's stack when it runs under honcho / ``bench start``, not cwcli.

    The fallback used by ``status`` when :func:`discover_stack` finds no cwcli
    supervisord: locate the honcho manager rooted at this bench (keyed by cwd) and
    label-map its live process tree from the SAME ``ps`` machinery. Every reported
    process is genuinely up (its ``ps`` presence is the truth); there is no
    supervisord ``state`` in this mode, so ``state`` stays ``None``. Returns
    ``manager_up=False`` (no processes) when no such manager is running for the bench.
    """
    rows = _ps_rows(container)
    mgr_pids = _manager_pids_for_bench(container, rows, bench_path)
    if not mgr_pids:
        return UnsupervisedStack(manager_up=False, processes=[])

    tree = _descendants(rows, set(mgr_pids))
    processes: list[ProcessHealth] = []
    for r in rows:
        if r.pid not in tree or r.pid in mgr_pids:
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
    return UnsupervisedStack(manager_up=True, processes=processes)


def stop_supervisor(container, bench_path: str, *, timeout: float = 15.0) -> bool:
    """Terminate the supervisord supervisor for ``bench_path`` by its DISCOVERED PID.

    ``SIGTERM`` to supervisord makes it shut its programs down cleanly (unlike
    honcho's panic teardown). Waits (bounded) for the tree to actually exit before
    returning, so a caller relaunching does not race a still-running supervisord on
    the container ports; escalates to ``SIGKILL`` if it overstays. Keyed to the
    bench's supervisord PID. Returns True if a supervisor was found and signalled.
    """
    pids = _supervisord_pids_for_bench(container, _ps_rows(container), bench_path)
    if not pids:
        return False
    joined = " ".join(str(p) for p in pids)
    container.exec_run(["sh", "-c", f"kill -TERM {joined} 2>/dev/null || true"])

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _supervisord_pids_for_bench(container, _ps_rows(container), bench_path):
            return True
        time.sleep(0.5)

    container.exec_run(["sh", "-c", f"kill -KILL {joined} 2>/dev/null || true"])
    return True


# ------------------------------------------------------------------ Procfile parse


def expected_labels(container, bench_path: str) -> list[str]:
    """The labels a bench's live ``Procfile`` defines (the expected-to-run set, normalized)."""
    return [_normalize_procfile_key(k) for k in procfile_programs(container, bench_path)]


def procfile_programs(container, bench_path: str) -> list[str]:
    """The RAW Procfile keys (supervisord program names) in file order."""
    quoted = shlex.quote(f"{bench_path}/Procfile")
    exit_code, output = container.exec_run(["sh", "-c", f"cat {quoted}"])
    if exit_code not in (0, None):
        return []
    programs: list[str] = []
    for line in _decode(output).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        if key and key not in programs:
            programs.append(key)
    return programs


def _normalize_procfile_key(key: str) -> str:
    """Align a Procfile key with discovery's labels (``worker_<q>`` -> ``worker:<q>``)."""
    if key.startswith("worker_"):
        return "worker:" + key[len("worker_") :]
    return key


def program_for_label(programs: list[str], label: str) -> str | None:
    """Resolve a user ``--process`` label to a supervisord program name (or None).

    Accepts either the discovery label (``worker:default``, what ``status`` shows)
    or the raw Procfile key (``worker_default``). Matches against the live program
    set so an unknown/ambiguous label yields None (the caller returns NEEDS_CHOICE).
    """
    for program in programs:
        if label == program or label == _normalize_procfile_key(program):
            return program
    return None


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
    """Write the launch marker (``supervisor``/``started_at``/``config_path``/``log_path``).

    Returns the bench ``logs/`` dir (where the per-process logs live).
    """
    log_dir = logs_dir(bench_path)
    payload = json.dumps(
        {
            "supervisor": SUPERVISOR,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "config_path": _config_path(bench_path),
            "log_path": log_dir,
        }
    )
    container.exec_run(["python3", "-c", _WRITE_FILE_PROG, _marker_path(bench_path), payload])
    return log_dir


# -------------------------------------------------------------------------- launch


def web_http_code(container, *, port: int, site: str | None = None) -> str | None:
    """The web server's HTTP code on ``port``, or None if unreachable.

    ``port`` is keyword-only with NO DEFAULT, deliberately. One instance holds many
    benches, each serving the port bench's own ``make_ports`` assigned it, so there
    is no universal web port to fall back on - this probe once hardcoded 8000 and
    therefore measured bench 0's web server no matter which bench was being asked
    about (a healthy bench 1 read ``degraded``; a dead bench 1 read bench 0's live
    code). With no default the caller must name a port, so a future caller cannot
    re-inherit 8000 by omission, which is exactly how that defect arrived. Resolve
    the port with ``resolvers.resolve_assigned_ports(..., fill_defaults=False)``.

    ``site`` is the bench's site, sent as the ``Host`` header. Frappe is
    MULTI-TENANT: it routes by Host, so a request carrying none (``localhost``)
    names no site and Frappe correctly answers 404 - which is what a healthy bench
    used to report. That 404 was never a fault (``core.status`` counts any code as
    serving), but a health field whose normal value is an error code teaches its
    reader to discount it, and a reader who discounts this one discounts a genuine
    ``degraded`` too. With the site named, the probe is the request a real user
    makes and a healthy bench reads 200. Omitted (or unresolvable) keeps the old
    host-less request rather than guessing a site.
    """
    url = f"http://localhost:{port}"
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}"]
    if site:
        cmd += ["-H", f"Host: {site}"]
    exit_code, output = container.exec_run([*cmd, url])
    if exit_code not in (0, None):
        return None
    code = _decode(output).strip()
    return code or None


def web_is_serving(container, *, port: int, site: str | None = None) -> bool:
    """True iff the web server answers on ``port`` (any HTTP code, even 404/5xx).

    Same "up" definition ``core.status`` uses (``web_code not in (None, '000')``):
    a bound port serving *any* code is up; ``None``/``000`` is unreachable.
    ``port`` is keyword-only with no default - see :func:`web_http_code`.
    """
    return web_http_code(container, port=port, site=site) not in (None, "000")


def wait_web_ready(container, *, port: int, timeout: float = 60.0, interval: float = 1.0) -> bool:
    """Poll ``port`` until the web server is serving, or ``timeout`` elapses.

    ``bench serve`` binds its port a beat AFTER supervisord reports its programs up,
    so a caller that declares "running" the instant the launch returns races the
    web port (a scripted ``cwcli start && cwcli status`` catches a transient
    ``degraded``). Blocking on this closes that race. Returns True as soon as the
    web answers (typically the first poll once bound), False on timeout. Bounded,
    and a no-op-fast when already serving.

    ``port`` is keyword-only with no default - see :func:`web_http_code`. A caller
    that cannot resolve the bench's port must SKIP the wait rather than guess, or it
    spends the whole timeout probing a sibling bench's server.
    """
    deadline = time.monotonic() + timeout
    while True:
        if web_is_serving(container, port=port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def supervisor_installed(container, bench_path: str) -> bool:
    """True iff ``supervisor`` is importable in the bench virtualenv."""
    py = _venv_python(bench_path)
    exit_code, _ = container.exec_run(
        ["bash", "-c", f"{shlex.quote(py)} -c 'import supervisor' 2>/dev/null"]
    )
    return exit_code in (0, None)


def ensure_supervisor_installed(container, bench_path: str) -> bool:
    """Idempotently ``pip install supervisor`` into the bench env; fail CLOSED.

    supervisord is not preinstalled in the frappe dev image. Installs it into the
    bench virtualenv on first supervise (skip if already importable). Returns True
    if it installed it now, False if it was already present. Raises ``CwcliError``
    on a failed install rather than silently falling back to honcho.
    """
    from .errors import CwcliError, ErrorKind

    if supervisor_installed(container, bench_path):
        return False
    py = _venv_python(bench_path)
    exit_code, output = container.exec_run(
        ["bash", "-c", f"{shlex.quote(py)} -m pip install supervisor"]
    )
    if exit_code not in (0, None) or not supervisor_installed(container, bench_path):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "supervisor.install_failed",
            "Could not install 'supervisor' into the bench environment "
            f"({bench_path}/env). A network connection is required on first supervise.",
            detail={"output": _decode(output)[-2000:]},
        )
    return True


def _ini_value(value: str) -> str:
    """Escape a value for a supervisord config line (guard against ``%`` interpolation)."""
    return value.replace("%", "%%")


def render_config(programs: list[str], bench_path: str, *, autorestart: bool = True) -> str:
    """Generate the supervisord config for a bench from its Procfile program names.

    One ``[program:<name>]`` per Procfile key (its command run via the launcher, so
    PATH/redirections/``.env`` behave as under honcho). ``autorestart=unexpected``
    (the default) self-heals a crash but not a clean exit; ``startretries`` bounds
    the retries so a crash-loop reaches supervisord's visible ``FATAL`` state;
    ``stopasgroup``/``killasgroup`` clean up grandchildren. Per-program
    ``stdout_logfile`` + built-in rotation replace honcho's combined-stream capper.
    """
    launcher = _ini_value(_launcher_path(bench_path))
    directory = _ini_value(bench_path)
    autorestart_val = "unexpected" if autorestart else "false"

    lines = [
        "[unix_http_server]",
        f"file={_ini_value(_sock_path(bench_path))}",
        "chmod=0700",
        "",
        "[supervisord]",
        f"logfile={_ini_value(f'{bench_path}/logs/{_SUPERVISORD_LOG_NAME}')}",
        f"pidfile={_ini_value(f'{bench_path}/logs/{_SUPERVISORD_PID_NAME}')}",
        f"childlogdir={_ini_value(logs_dir(bench_path))}",
        "nodaemon=false",
        "",
        "[rpcinterface:supervisor]",
        "supervisor.rpcinterface_factory = " "supervisor.rpcinterface:make_main_rpcinterface",
        "",
        "[supervisorctl]",
        f"serverurl=unix://{_ini_value(_sock_path(bench_path))}",
        "",
    ]
    for program in programs:
        lines += [
            f"[program:{program}]",
            f'command=bash "{launcher}" {program}',
            f"directory={directory}",
            "autostart=true",
            f"autorestart={autorestart_val}",
            "startretries=3",
            "startsecs=3",
            "stopasgroup=true",
            "killasgroup=true",
            "redirect_stderr=true",
            f"stdout_logfile={_ini_value(process_log_path(bench_path, program))}",
            f"stdout_logfile_maxbytes={_PROC_LOG_MAXBYTES}",
            f"stdout_logfile_backups={_PROC_LOG_BACKUPS}",
            "",
        ]
    return "\n".join(lines) + "\n"


def launch(container, bench_path: str, *, autorestart: bool = True) -> str:
    """Install supervisor if needed, write the config + launcher, launch supervisord detached.

    Returns the bench ``logs/`` dir (where per-process logs land). Raises
    ``CwcliError`` if supervisor cannot be installed. The supervisord process
    daemonizes itself (``nodaemon=false``); the exec is detached and returns at once.
    """
    ensure_supervisor_installed(container, bench_path)

    programs = procfile_programs(container, bench_path)
    config = render_config(programs, bench_path, autorestart=autorestart)

    # Drop the launcher + config into the bench (list-form exec: no shell quoting).
    container.exec_run(
        ["python3", "-c", _WRITE_FILE_PROG, _launcher_path(bench_path), _LAUNCHER_SRC]
    )
    container.exec_run(["python3", "-c", _WRITE_FILE_PROG, _config_path(bench_path), config])

    py = shlex.quote(_venv_python(bench_path))
    cfg = shlex.quote(_config_path(bench_path))
    b = shlex.quote(bench_path)
    cmd = f"cd {b} && mkdir -p logs && {py} -m supervisor.supervisord -c {cfg}"
    container.exec_run(["bash", "-c", cmd], detach=True)
    return logs_dir(bench_path)


# ----------------------------------------------------------------- supervisorctl


def _supervisorctl(container, bench_path: str, *args: str) -> tuple[int | None, str]:
    """Run ``supervisorctl -c <config> <args...>`` in the bench env; return (code, text)."""
    py = shlex.quote(_venv_python(bench_path))
    cfg = shlex.quote(_config_path(bench_path))
    quoted_args = " ".join(shlex.quote(a) for a in args)
    cmd = f"{py} -m supervisor.supervisorctl -c {cfg} {quoted_args}"
    exit_code, output = container.exec_run(["bash", "-c", cmd])
    return exit_code, _decode(output)


def supervisorctl_states(container, bench_path: str) -> dict[str, tuple[str, int | None]]:
    """Per-program supervisord state + PID keyed by the RAW program name (Procfile key).

    Parses ``supervisorctl status`` lines like ``web RUNNING pid 123, uptime ...``
    or ``worker_default FATAL Exited too quickly``. Keyed by the raw program name
    supervisord prints (the source of truth for what is actually supervised);
    callers normalize to the discovery label (``worker_default`` ->
    ``worker:default``) via :func:`_normalize_procfile_key` where they need it.
    """
    exit_code, text = _supervisorctl(container, bench_path, "status")
    # supervisorctl exits non-zero when any program is not RUNNING; still parse the
    # body (the state tokens are what we want), so do not bail on the exit code.
    states: dict[str, tuple[str, int | None]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        program, state = parts[0], parts[1]
        pid: int | None = None
        # "... pid 123, uptime ..." -> capture the pid when present.
        for i, tok in enumerate(parts):
            if tok == "pid" and i + 1 < len(parts):
                pid = _int_or_none(parts[i + 1].rstrip(","))
                break
        states[program] = (state, pid)
    return states


def states_by_label(states: dict[str, tuple[str, int | None]]) -> dict[str, tuple[str, int | None]]:
    """Re-key raw-program supervisord states to discovery labels (for the status merge)."""
    return {_normalize_procfile_key(k): v for k, v in states.items()}


def restart_program(container, bench_path: str, program: str) -> tuple[int | None, str]:
    """Restart ONE supervisord program (siblings untouched); return (code, text)."""
    return _supervisorctl(container, bench_path, "restart", program)


def clear_marker(container, bench_path: str) -> None:
    """Remove the launch marker, so the bench reads as never-started, not crashed.

    The marker is what distinguishes "started, supervisor now down" (``degraded``)
    from "never started" (``online``), so a DELIBERATE stop must clear it: leaving it
    behind makes ``cwcli stop --bench`` produce a permanently ``degraded`` instance
    with nothing wrong with it, which is a health signal crying wolf. Only the
    per-bench stop clears it - a supervisord that died on its own leaves it in place,
    which is exactly the state it exists to report.
    """
    container.exec_run(["rm", "-f", _marker_path(bench_path)])


def _self_check() -> None:
    """Runnable check: labels map, config generates, program resolution, ctl parse."""
    # Label mapping covers every Procfile process shape (supervisor/launcher excluded).
    assert label_for("/env/bin/python /env/bin/bench serve --port 8000") == "web"
    assert label_for("node /apps/frappe/socketio.js") == "socketio"
    assert label_for("/env/bin/bench schedule") == "schedule"
    assert label_for("/env/bin/bench watch") == "watch"
    assert label_for("/env/bin/bench worker") == "worker"
    assert label_for("/env/bin/bench worker --queue short") == "worker:short"
    # The real runtime form once the ``bench`` wrapper execs into bench_helper.
    assert label_for("/env/bin/python -m frappe.utils.bench_helper frappe serve --port 8000") == (
        "web"
    )
    assert label_for("/env/bin/python -m frappe.utils.bench_helper frappe schedule") == "schedule"
    assert label_for("/env/bin/python -m frappe.utils.bench_helper frappe watch") == "watch"
    assert label_for("/env/bin/python -m frappe.utils.bench_helper frappe worker") == "worker"
    assert label_for("redis-server /w/b/config/redis_cache.conf") == "redis_cache"
    assert label_for("redis-server /w/b/config/redis_queue.conf") == "redis_queue"
    assert (
        label_for("/env/bin/python /env/bin/supervisord -c /w/b/logs/.cwcli-supervisor.conf")
        is None
    )
    assert _normalize_procfile_key("worker_short") == "worker:short"

    # Config generation: one program section, autorestart toggle, per-program log.
    cfg = render_config(["web", "worker_short"], "/w/b", autorestart=True)
    assert "[program:web]" in cfg
    assert "[program:worker_short]" in cfg
    assert "autorestart=unexpected" in cfg
    assert 'command=bash "/w/b/logs/.cwcli-run.sh" web' in cfg
    assert "stdout_logfile=/w/b/logs/web.supervisor.log" in cfg
    off = render_config(["web"], "/w/b", autorestart=False)
    assert "autorestart=false" in off

    # Program-for-label accepts both the discovery label and the raw key.
    programs = ["web", "worker_short", "redis_cache"]
    assert program_for_label(programs, "web") == "web"
    assert program_for_label(programs, "worker:short") == "worker_short"
    assert program_for_label(programs, "worker_short") == "worker_short"
    assert program_for_label(programs, "nope") is None

    # -c config detection.
    assert _config_from_args("python supervisord -c /w/b/logs/.cwcli-supervisor.conf") == (
        "/w/b/logs/.cwcli-supervisor.conf"
    )

    # honcho / bench-start is a manager (the status fallback root); supervisord/leaves are not.
    assert _is_process_manager("/env/bin/python /env/bin/honcho start") is True
    assert _is_process_manager("/env/bin/python /env/bin/bench serve") is False

    # supervisorctl status parse -> normalized-label states.
    class _C:
        def exec_run(self, cmd, detach=False):
            text = "web   RUNNING   pid 123, uptime 0:05:00\nworker_short   FATAL   Exited too quickly\n"
            return (3, text.encode())

    states = supervisorctl_states(_C(), "/w/b")
    assert states["web"] == ("RUNNING", 123)
    assert states["worker_short"] == ("FATAL", None)
    by_label = states_by_label(states)
    assert by_label["worker:short"] == ("FATAL", None)

    print("supervision self-check OK")


if __name__ == "__main__":
    _self_check()
