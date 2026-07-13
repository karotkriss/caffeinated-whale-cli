"""``core.supervision`` substrate coverage against faked ``ps``/Procfile/exec I/O.

Covers discovery + label mapping + the bench-keying (no cross-bench
mis-attribution), the expected-label Procfile parse, the supervisor marker
read/write (present vs absent), the web probe, the launch command shape, and the
honcho-prefix per-process view - all with a programmable fake container so no
Docker is needed (task 1.6).
"""

from __future__ import annotations

import json

from caffeinated_whale_cli.core import supervision

BENCH = "/workspace/frappe-bench"

# A realistic single-bench honcho stack (honcho pid 100, cwd == BENCH, children by ppid).
_PS_SINGLE = """\
1 0 99999 0.0 1000 /sbin/init
100 1 500 0.1 2000 /env/bin/python /env/bin/honcho start
101 100 499 0.5 80000 /env/bin/python /env/bin/bench serve --port 8000
102 100 499 0.2 60000 node /workspace/frappe-bench/apps/frappe/socketio.js
103 100 499 0.1 50000 /env/bin/python /env/bin/bench schedule
104 100 499 0.1 50000 /env/bin/python /env/bin/bench watch
105 100 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default
106 100 499 0.0 3000 redis-server /workspace/frappe-bench/config/redis_cache.conf
107 100 499 0.0 3000 redis-server /workspace/frappe-bench/config/redis_queue.conf
"""

_PROCFILE = """\
redis_cache: redis-server config/redis_cache.conf
redis_queue: redis-server config/redis_queue.conf
web: bench serve --port 8000
socketio: node apps/frappe/socketio.js
watch: bench watch
schedule: bench schedule
worker_default: bench worker --queue default
"""


class FakeContainer:
    """Programmable frappe container: routes exec_run (list/str) by command shape."""

    def __init__(
        self,
        *,
        ps=_PS_SINGLE,
        cwds=None,
        procfile=_PROCFILE,
        marker=None,
        web_code="200",
        web_ok=True,
    ):
        self.status = "running"
        self.name = "cwe2e-proj-frappe-1"
        self.labels = {"com.docker.compose.service": "frappe"}
        self.ps = ps
        self.cwds = cwds if cwds is not None else {100: BENCH}
        self.procfile = procfile
        self.marker = marker
        self.web_code = web_code
        self.web_ok = web_ok
        self.writes: dict[str, str] = {}
        self.launches: list[str] = []
        self.killed: list[str] = []
        self.calls: list = []

    def reload(self):
        pass

    def start(self):
        pass

    def exec_run(self, cmd, detach=False, workdir=None, environment=None):
        self.calls.append(cmd)
        if isinstance(cmd, list):
            return self._exec_list(cmd, detach)
        return (0, b"")

    def _exec_list(self, cmd, detach):
        head = cmd[0]
        if head == "ps":
            return (0, self.ps.encode())
        if head == "cat":
            path = cmd[1]
            if path.endswith(supervision._MARKER_NAME):
                if self.marker is None:
                    return (1, b"")
                return (0, json.dumps(self.marker).encode())
            return (0, b"")
        if head == "curl":
            return (0, self.web_code.encode()) if self.web_ok else (7, b"")
        if head == "python3":
            # ["python3","-c", prog, path, payload] -> a file write (marker/capper).
            path, payload = cmd[3], cmd[4]
            self.writes[path] = payload
            return (0, b"")
        if head == "bash":
            # ["bash","-c", launch] detached.
            self.launches.append(cmd[2])
            return (None, None) if detach else (0, b"")
        if head == "sh":
            script = cmd[2]
            if "readlink" in script:
                lines = "".join(f"{pid}\t{cwd}\n" for pid, cwd in self.cwds.items())
                return (0, lines.encode())
            if "Procfile" in script:
                return (0, self.procfile.encode())
            if "kill" in script:
                # Simulate the honcho stack dying: subsequent ps shows no honcho.
                self.killed.append(script)
                self.ps = "1 0 5 0.0 1000 /sbin/init\n"
                self.cwds = {}
                return (0, b"")
            return (0, b"")
        return (0, b"")


class TestDiscovery:
    def test_live_processes_map_to_labels_with_resources(self):
        c = FakeContainer()
        snap = supervision.discover_stack(c, BENCH)
        assert snap.supervisor_up is True
        assert snap.supervisor_pid == 100
        labels = {p.label for p in snap.processes}
        assert labels == {
            "web",
            "socketio",
            "schedule",
            "watch",
            "worker:default",
            "redis_cache",
            "redis_queue",
        }
        web = next(p for p in snap.processes if p.label == "web")
        assert web.up is True
        assert web.pid == 101
        assert web.uptime_s == 499
        assert web.cpu_pct == 0.5
        assert web.rss_kb == 80000
        # honcho itself is the supervisor, never a per-process entry.
        assert "honcho" not in labels

    def test_no_honcho_means_supervisor_down(self):
        c = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        snap = supervision.discover_stack(c, BENCH)
        assert snap.supervisor_up is False
        assert snap.processes == []

    def test_discovery_is_keyed_to_the_resolved_bench(self):
        # Two honcho stacks: pid 100 (bench A) and pid 200 (bench B).
        ps = _PS_SINGLE + (
            "200 1 300 0.1 2000 /env/bin/python /env/bin/honcho start\n"
            "201 200 299 0.4 80000 /env/bin/python /env/bin/bench serve --port 8001\n"
        )
        cwds = {100: BENCH, 200: "/workspace/other-bench"}
        c = FakeContainer(ps=ps, cwds=cwds)

        snap_a = supervision.discover_stack(c, BENCH)
        assert snap_a.supervisor_pid == 100
        assert all(p.pid != 201 for p in snap_a.processes)

        snap_b = supervision.discover_stack(c, "/workspace/other-bench")
        assert snap_b.supervisor_pid == 200
        assert {p.pid for p in snap_b.processes} == {201}

    def test_honcho_keyed_by_explicit_procfile_arg(self):
        # honcho launched with -f <bench>/Procfile is self-describing (no readlink).
        ps = (
            "1 0 5 0.0 1000 /sbin/init\n"
            f"300 1 100 0.1 2000 /env/bin/python /env/bin/honcho start -f {BENCH}/Procfile\n"
            "301 300 99 0.4 80000 /env/bin/python /env/bin/bench serve\n"
        )
        c = FakeContainer(ps=ps, cwds={})  # no cwd resolution needed
        snap = supervision.discover_stack(c, BENCH)
        assert snap.supervisor_up is True
        assert snap.supervisor_pid == 300


class TestStopSupervisor:
    def test_terminates_the_bench_honcho_by_pid(self):
        c = FakeContainer()  # honcho pid 100 for BENCH
        assert supervision.stop_supervisor(c, BENCH, timeout=2) is True
        assert c.killed, "a kill signal must be sent"
        assert "kill -TERM 100" in c.killed[0]
        # After the kill the stack is gone.
        assert supervision.discover_stack(c, BENCH).supervisor_up is False

    def test_no_supervisor_is_a_noop(self):
        c = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        assert supervision.stop_supervisor(c, BENCH, timeout=2) is False
        assert c.killed == []


class TestExpectedLabels:
    def test_expected_reflects_the_procfile(self):
        c = FakeContainer()
        assert supervision.expected_labels(c, BENCH) == [
            "redis_cache",
            "redis_queue",
            "web",
            "socketio",
            "watch",
            "schedule",
            "worker:default",
        ]

    def test_missing_procfile_is_empty(self):
        c = FakeContainer()
        c.procfile = ""
        assert supervision.expected_labels(c, BENCH) == []


class TestMarker:
    def test_absent_marker_reads_none(self):
        assert supervision.read_marker(FakeContainer(marker=None), BENCH) is None

    def test_present_marker_round_trips(self):
        marker = {"supervisor": "honcho", "started_at": "t", "log_path": "p"}
        assert supervision.read_marker(FakeContainer(marker=marker), BENCH) == marker

    def test_write_marker_records_supervisor_and_log_path(self):
        c = FakeContainer()
        log_path = supervision.write_marker(c, BENCH)
        assert log_path == supervision.bench_start_log_path(BENCH)
        written = json.loads(c.writes[supervision._marker_path(BENCH)])
        assert written["supervisor"] == "honcho"
        assert written["log_path"] == log_path
        assert written["started_at"]


class TestLaunch:
    def test_launch_writes_capper_and_starts_honcho_piped_to_it(self):
        c = FakeContainer()
        log_path = supervision.launch(c, BENCH)
        assert log_path == supervision.bench_start_log_path(BENCH)
        # capper dropped into the bench.
        assert supervision._capper_path(BENCH) in c.writes
        assert "os.replace" in c.writes[supervision._capper_path(BENCH)]
        # the launch cds into the bench and pipes bench start through the capper.
        assert len(c.launches) == 1
        cmd = c.launches[0]
        assert cmd.startswith(f"cd {BENCH}")
        assert "bench start" in cmd
        assert "python3" in cmd and supervision._LOG_NAME in cmd


class TestWebProbe:
    def test_web_code_when_answering(self):
        assert supervision.web_http_code(FakeContainer(web_code="200")) == "200"

    def test_web_none_when_curl_fails(self):
        assert supervision.web_http_code(FakeContainer(web_ok=False)) is None


class TestPerProcessLog:
    def test_selects_a_label_from_the_combined_stream(self):
        combined = (
            "10:00:01 web    | GET / 200\n"
            "10:00:02 worker | processed job\n"
            "10:00:03 web    | GET /app 200\n"
        )
        assert supervision.per_process_log_lines(combined, "web") == [
            "GET / 200",
            "GET /app 200",
        ]
