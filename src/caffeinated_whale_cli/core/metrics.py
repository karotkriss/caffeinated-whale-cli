"""Performance history for the web UI: a rolling in-memory window of real
measurements, sampled at the fleet's probe interval, never a fabricated line.

Per RUNNING instance, each tick:

* every project container (frappe, mariadb, redis-cache, redis-queue) through
  the Docker SDK's one-shot ``stats`` (~40 ms each): CPU percent (computed from
  the cumulative counters between two ticks, the way ``docker stats`` does),
  memory bytes, and network / block I/O as bytes per second from the deltas;
* every bench's processes from the fleet's own fused probe (``ProcessHealth``:
  CPU percent and RSS, which the health delta deliberately never publishes
  because they change every tick - here they are the whole point);
* the RQ queue depths (``default``/``short``/``long``) with one ``redis-cli``
  exec on the redis-queue container.

Plus the host each tick (load, CPU count, memory total / available) so a fleet
overview can show headroom. A STOPPED instance is simply not sampled: its series
ends where it stopped and resumes when it starts, which is the honest shape.

The store is bounded (``max_points`` per series, one hour at 2.5 s) and lives in
the daemon process only; the page fetches the window once and appends from the
``metrics`` SSE event.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from . import docker as core_docker

QUEUES = ("default", "short", "long")


def _read_host() -> dict[str, Any]:
    out: dict[str, Any] = {"cpus": os.cpu_count() or 1}
    try:
        load1, load5, _ = os.getloadavg()
        out["load1"], out["load5"] = round(load1, 2), round(load5, 2)
    except OSError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    out["mem_total" if key == "MemTotal" else "mem_available"] = (
                        int(rest.split()[0]) * 1024
                    )
    except (OSError, ValueError):
        pass
    return out


def _role(container_name: str, project: str) -> str:
    """``test555-redis-queue-1`` -> ``redis-queue``: the compose service name."""
    name = container_name
    if name.startswith(project + "-"):
        name = name[len(project) + 1 :]
    return name.rsplit("-", 1)[0] if name[-1:].isdigit() and "-" in name else name


class MetricsStore:
    def __init__(self, max_points: int = 1440) -> None:
        self._lock = threading.Lock()
        self._max = max_points
        self._projects: dict[str, deque] = {}
        self._host: deque = deque(maxlen=max_points)
        self._raw: dict[str, dict[str, Any]] = {}  # last cumulative counters per container
        self.tick = 0  # the sampler's loop counter; every point of one pass carries it

    # ------------------------------------------------------------- sampling

    def sample(self, project: str, benches: list) -> dict[str, Any] | None:
        """One tick for one running instance; returns the point recorded (or None)."""
        now = time.time()
        containers: dict[str, dict[str, Any]] = {}
        try:
            project_containers = core_docker.get_project_containers(project) or []
        except Exception:
            return None
        queue_container = None
        for c in project_containers:
            if getattr(c, "status", "") != "running":
                continue
            role = _role(c.name, project)
            if role == "redis-queue":
                queue_container = c
            try:
                s = c.stats(stream=False, one_shot=True)
            except Exception:
                continue
            containers[role] = self._container_point(c.name, s, now)
        point: dict[str, Any] = {
            "ts": now,
            "tick": self.tick,
            "containers": containers,
            "benches": {},
        }
        for b in benches or []:
            procs = {}
            for p in getattr(b, "processes", []) or []:
                if getattr(p, "up", False) and getattr(p, "cpu_pct", None) is not None:
                    procs[p.label] = {"cpu_pct": p.cpu_pct, "rss_kb": p.rss_kb}
            point["benches"][str(getattr(b, "index", "0"))] = {"processes": procs}
        if queue_container is not None:
            point["queues"] = self._queue_depths(queue_container)
        with self._lock:
            self._projects.setdefault(project, deque(maxlen=self._max)).append(point)
        return point

    def sample_host(self) -> dict[str, Any]:
        self.tick += 1
        point = {"ts": time.time(), "tick": self.tick, **_read_host()}
        with self._lock:
            self._host.append(point)
        return point

    def _container_point(self, name: str, s: dict, now: float) -> dict[str, Any]:
        cpu = s.get("cpu_stats") or {}
        total = (cpu.get("cpu_usage") or {}).get("total_usage")
        system = cpu.get("system_cpu_usage")
        online = (
            cpu.get("online_cpus")
            or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
            or 1
        )
        mem = s.get("memory_stats") or {}
        usage = mem.get("usage")
        cache = (mem.get("stats") or {}).get("inactive_file") or 0
        rx = sum(v.get("rx_bytes", 0) for v in (s.get("networks") or {}).values())
        tx = sum(v.get("tx_bytes", 0) for v in (s.get("networks") or {}).values())
        read = write = 0
        for e in (s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
            if e.get("op", "").lower() == "read":
                read += e.get("value", 0)
            elif e.get("op", "").lower() == "write":
                write += e.get("value", 0)
        raw = {
            "ts": now,
            "total": total,
            "system": system,
            "rx": rx,
            "tx": tx,
            "read": read,
            "write": write,
        }
        prev = self._raw.get(name)
        self._raw[name] = raw
        out: dict[str, Any] = {
            "mem_bytes": (usage - cache) if usage is not None else None,
            "mem_limit": mem.get("limit"),
            "pids": (s.get("pids_stats") or {}).get("current"),
        }
        if prev and prev["ts"] < now:
            dt = now - prev["ts"]
            if (
                total is not None
                and system is not None
                and prev["total"] is not None
                and prev["system"] is not None
                and system > prev["system"]
            ):
                out["cpu_pct"] = round(
                    (total - prev["total"]) / (system - prev["system"]) * online * 100, 2
                )
            out["net_rx_bps"] = max(0.0, (rx - prev["rx"]) / dt)
            out["net_tx_bps"] = max(0.0, (tx - prev["tx"]) / dt)
            out["blk_read_bps"] = max(0.0, (read - prev["read"]) / dt)
            out["blk_write_bps"] = max(0.0, (write - prev["write"]) / dt)
        return out

    @staticmethod
    def _queue_depths(container) -> dict[str, int] | None:
        cmd = ["sh", "-c", " ".join(f"redis-cli llen rq:queue:{q};" for q in QUEUES)]
        try:
            code, out = container.exec_run(cmd)
        except Exception:
            return None
        if code != 0:
            return None
        values = [v for v in out.decode("utf-8", "replace").split() if v.isdigit()]
        if len(values) != len(QUEUES):
            return None
        return dict(zip(QUEUES, map(int, values), strict=True))

    # -------------------------------------------------------------- reading

    def series(self, project: str, since: float = 0.0) -> list[dict[str, Any]]:
        with self._lock:
            return [p for p in self._projects.get(project, ()) if p["ts"] >= since]

    def host_series(self, since: float = 0.0) -> list[dict[str, Any]]:
        with self._lock:
            return [p for p in self._host if p["ts"] >= since]

    def forget(self, project: str) -> None:
        with self._lock:
            self._projects.pop(project, None)
            for name in [n for n in self._raw if n.startswith(project + "-")]:
                self._raw.pop(name, None)


def metrics_loop(
    fleet, store: MetricsStore, publish, stop: threading.Event, interval: float, *, on_error=None
) -> None:
    """Sample every running instance (and the host) each ``interval`` seconds and
    publish one ``metrics`` frame carrying the new points."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            frame: dict[str, Any] = {"host": store.sample_host(), "projects": {}}
            # Docker unreachable: the rows are what it said before it went away, and
            # sampling them would only fail once per instance per tick.
            for project in [] if fleet.docker_down() else fleet.running_projects():
                state = fleet.get(project)
                if state is None or not state.container_running:
                    continue
                point = store.sample(project, getattr(state, "benches", []))
                if point is not None:
                    frame["projects"][project] = point
            publish(frame)
        except Exception as e:  # the sampler must never take the daemon down
            if on_error is not None:
                on_error(e)
        stop.wait(max(0.5, interval - (time.monotonic() - started)))
