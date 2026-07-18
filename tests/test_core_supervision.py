"""``core.supervision`` substrate coverage against faked ``ps``/exec I/O.

Covers supervisord discovery + label mapping + the bench-keying (no cross-bench
mis-attribution), the Procfile parse (raw programs + normalized labels), the
supervisor marker (records the config path detection keys on), supervisord config
generation, the idempotent + fail-closed ``pip install supervisor`` bootstrap, the
detached supervisord launch, the ``supervisorctl`` state parse + single-program
restart, and the per-process combined-log synthesis - all with a programmable fake
container so no Docker is needed.
"""

from __future__ import annotations

import json
import os

import pytest

from caffeinated_whale_cli.core import supervision
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH = "/workspace/frappe-bench"
_CFG = supervision._config_path(BENCH)

# A realistic single-bench supervisord stack: supervisord pid 100 launched with
# ``-c <bench>/logs/.cwcli-supervisor.conf`` (self-describing), children by ppid.
_PS_SINGLE = f"""\
1 0 99999 0.0 1000 /sbin/init
100 1 500 0.1 2000 /env/bin/python /env/bin/supervisord -c {_CFG}
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

# A bench running under honcho (bench's own ``bench start``), which cwcli never
# launched: honcho pid 200 (cwd == BENCH), its Procfile children by ppid. NO
# supervisord line anywhere - this is exactly how a pre-v3 instance looks.
_PS_HONCHO = """\
1 0 99999 0.0 1000 /sbin/init
200 1 500 0.1 2000 /env/bin/python /env/bin/honcho start
201 200 499 0.5 80000 /env/bin/python /env/bin/bench serve --port 8000
202 200 499 0.2 60000 node /workspace/frappe-bench/apps/frappe/socketio.js
203 200 499 0.1 50000 /env/bin/python /env/bin/bench schedule
204 200 499 0.1 50000 /env/bin/python /env/bin/bench watch
205 200 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default
206 200 499 0.0 3000 redis-server /workspace/frappe-bench/config/redis_cache.conf
207 200 499 0.0 3000 redis-server /workspace/frappe-bench/config/redis_queue.conf
"""

# supervisorctl status for the single-bench stack (every program RUNNING).
_CTL_SINGLE = """\
redis_cache   RUNNING   pid 106, uptime 0:05:00
redis_queue   RUNNING   pid 107, uptime 0:05:00
web   RUNNING   pid 101, uptime 0:05:00
socketio   RUNNING   pid 102, uptime 0:05:00
watch   RUNNING   pid 104, uptime 0:05:00
schedule   RUNNING   pid 103, uptime 0:05:00
worker_default   RUNNING   pid 105, uptime 0:05:00
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
        supervisor_present=True,
        install_succeeds=True,
        ctl_status=None,
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
        self.supervisor_present = supervisor_present
        self.install_succeeds = install_succeeds
        self.ctl_status = ctl_status
        self.writes: dict[str, str] = {}
        self.launches: list[str] = []
        self.killed: list[str] = []
        self.restarts: list[str] = []
        self.pip_installed = False
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
        if head == "id":
            # `id -u/-g frappe` probe: report the host's own ids so core.start's
            # host-uid alignment is a clean no-op in these fakes.
            return (0, (str(os.getuid()) if "-u" in cmd else str(os.getgid())).encode())
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
            # ["python3","-c", prog, path, payload] -> a file write (marker/config/launcher).
            path, payload = cmd[3], cmd[4]
            self.writes[path] = payload
            return (0, b"")
        if head == "bash":
            return self._exec_bash(cmd[2], detach)
        if head == "sh":
            script = cmd[2]
            if "readlink" in script:
                lines = "".join(f"{pid}\t{cwd}\n" for pid, cwd in self.cwds.items())
                return (0, lines.encode())
            if "Procfile" in script:
                return (0, self.procfile.encode())
            if "kill" in script:
                # Simulate the supervisord stack dying: subsequent ps shows no supervisord.
                self.killed.append(script)
                self.ps = "1 0 5 0.0 1000 /sbin/init\n"
                self.cwds = {}
                return (0, b"")
            if "tail" in script:
                return (0, b"")
            return (0, b"")
        return (0, b"")

    def _exec_bash(self, script, detach):
        if "import supervisor" in script:
            return (0, b"") if self.supervisor_present else (1, b"")
        if "pip install supervisor" in script:
            if self.install_succeeds:
                self.pip_installed = True
                self.supervisor_present = True
                return (0, b"Successfully installed supervisor-4.3.0")
            return (1, b"ERROR: Could not install supervisor")
        if "supervisor.supervisorctl" in script:
            if " restart " in script:
                program = script.split(" restart ", 1)[1].strip().strip("'\"")
                self.restarts.append(program)
                return (0, f"{program}: stopped\n{program}: started\n".encode())
            body = self.ctl_status if self.ctl_status is not None else _CTL_SINGLE
            return (0, body.encode())
        if "supervisor.supervisord" in script:
            self.launches.append(script)
            return (None, None) if detach else (0, b"")
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
        # supervisord itself is the supervisor, never a per-process entry.
        assert "supervisord" not in labels

    def test_real_bench_helper_cmdlines_map_to_labels(self):
        # The load-bearing regression guard: once the ``bench`` wrapper execs away,
        # the live process is ``python -m frappe.utils.bench_helper frappe <cmd>`` -
        # NOT ``bench <cmd>``. web/schedule/watch must still map (else a serving web
        # reads as down, the false-down this fixes).
        ps = (
            f"1 0 99999 0.0 1000 /sbin/init\n"
            f"100 1 500 0.1 2000 /env/bin/python -m supervisor.supervisord -c {_CFG}\n"
            "101 100 499 0.5 80000 /env/bin/python -m frappe.utils.bench_helper frappe serve"
            " --port 8000\n"
            "102 100 499 0.2 60000 /home/frappe/.nvm/node/bin/node apps/frappe/socketio.js\n"
            "103 100 499 0.1 50000 /env/bin/python -m frappe.utils.bench_helper frappe schedule\n"
            "104 100 499 0.1 50000 /env/bin/python -m frappe.utils.bench_helper frappe watch\n"
            "105 100 499 0.3 70000 bash -c   bench worker 1>> logs/worker.log\n"
            "106 105 499 0.3 70000 /env/bin/python -m frappe.utils.bench_helper frappe worker\n"
        )
        c = FakeContainer(ps=ps)
        snap = supervision.discover_stack(c, BENCH)
        assert snap.supervisor_up is True
        by_label = {p.label: p for p in snap.processes}
        assert {"web", "socketio", "schedule", "watch", "worker"} <= set(by_label)
        assert by_label["web"].up is True and by_label["web"].pid == 101

    def test_no_supervisord_means_supervisor_down(self):
        c = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        snap = supervision.discover_stack(c, BENCH)
        assert snap.supervisor_up is False
        assert snap.processes == []

    def test_discovery_is_keyed_to_the_resolved_bench(self):
        # Two supervisord stacks: pid 100 (bench A) and pid 200 (bench B).
        other = "/workspace/other-bench"
        ps = _PS_SINGLE + (
            f"200 1 300 0.1 2000 /env/bin/python /env/bin/supervisord "
            f"-c {supervision._config_path(other)}\n"
            "201 200 299 0.4 80000 /env/bin/python /env/bin/bench serve --port 8001\n"
        )
        c = FakeContainer(ps=ps, cwds={})

        snap_a = supervision.discover_stack(c, BENCH)
        assert snap_a.supervisor_pid == 100
        assert all(p.pid != 201 for p in snap_a.processes)

        snap_b = supervision.discover_stack(c, other)
        assert snap_b.supervisor_pid == 200
        assert {p.pid for p in snap_b.processes} == {201}

    def test_supervisord_keyed_by_cwd_fallback(self):
        # supervisord without a -c arg is keyed by /proc/<pid>/cwd == bench.
        ps = (
            "1 0 5 0.0 1000 /sbin/init\n"
            "300 1 100 0.1 2000 /env/bin/python /env/bin/supervisord\n"
            "301 300 99 0.4 80000 /env/bin/python /env/bin/bench serve\n"
        )
        c = FakeContainer(ps=ps, cwds={300: BENCH})
        snap = supervision.discover_stack(c, BENCH)
        assert snap.supervisor_up is True
        assert snap.supervisor_pid == 300


class TestUnsupervisedFallback:
    """``discover_unsupervised_stack`` - the honcho / ``bench start`` fallback."""

    def test_honcho_processes_map_to_labels_up(self):
        # A bench under honcho (no cwcli supervisord) still reports every live
        # process UP with its real pid/uptime - the regression this fixes.
        c = FakeContainer(ps=_PS_HONCHO, cwds={200: BENCH})
        stack = supervision.discover_unsupervised_stack(c, BENCH)
        assert stack.manager_up is True
        by_label = {p.label: p for p in stack.processes}
        assert set(by_label) == {
            "web",
            "socketio",
            "schedule",
            "watch",
            "worker:default",
            "redis_cache",
            "redis_queue",
        }
        web = by_label["web"]
        assert web.up is True and web.pid == 201 and web.uptime_s == 499
        # No supervisord state exists in this mode - up is the ps truth.
        assert web.state is None
        # honcho itself (the manager) is never a per-process entry.
        assert "honcho" not in by_label

    def test_no_manager_means_manager_down(self):
        # cwcli supervisord absent AND no honcho -> genuinely nothing to report.
        c = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        stack = supervision.discover_unsupervised_stack(c, BENCH)
        assert stack.manager_up is False
        assert stack.processes == []

    def test_keyed_to_the_resolved_bench_by_cwd(self):
        # honcho for another bench (cwd != BENCH) must NOT be attributed here.
        c = FakeContainer(ps=_PS_HONCHO, cwds={200: "/workspace/other-bench"})
        stack = supervision.discover_unsupervised_stack(c, BENCH)
        assert stack.manager_up is False


class TestStopSupervisor:
    def test_terminates_the_bench_supervisord_by_pid(self):
        c = FakeContainer()  # supervisord pid 100 for BENCH
        assert supervision.stop_supervisor(c, BENCH, timeout=2) is True
        assert c.killed, "a kill signal must be sent"
        assert "kill -TERM 100" in c.killed[0]
        # After the kill the stack is gone.
        assert supervision.discover_stack(c, BENCH).supervisor_up is False

    def test_no_supervisor_is_a_noop(self):
        c = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        assert supervision.stop_supervisor(c, BENCH, timeout=2) is False
        assert c.killed == []


class TestProcfile:
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

    def test_raw_programs_keep_the_procfile_keys(self):
        c = FakeContainer()
        assert supervision.procfile_programs(c, BENCH) == [
            "redis_cache",
            "redis_queue",
            "web",
            "socketio",
            "watch",
            "schedule",
            "worker_default",
        ]

    def test_missing_procfile_is_empty(self):
        c = FakeContainer()
        c.procfile = ""
        assert supervision.expected_labels(c, BENCH) == []
        assert supervision.procfile_programs(c, BENCH) == []


class TestProgramForLabel:
    def test_accepts_label_and_raw_key(self):
        programs = ["web", "worker_default", "redis_cache"]
        assert supervision.program_for_label(programs, "web") == "web"
        assert supervision.program_for_label(programs, "worker:default") == "worker_default"
        assert supervision.program_for_label(programs, "worker_default") == "worker_default"

    def test_unknown_label_is_none(self):
        assert supervision.program_for_label(["web", "worker_default"], "nope") is None


class TestMarker:
    def test_absent_marker_reads_none(self):
        assert supervision.read_marker(FakeContainer(marker=None), BENCH) is None

    def test_present_marker_round_trips(self):
        marker = {"supervisor": "supervisord", "started_at": "t", "config_path": _CFG}
        assert supervision.read_marker(FakeContainer(marker=marker), BENCH) == marker

    def test_write_marker_records_supervisor_and_config_path(self):
        c = FakeContainer()
        log_dir = supervision.write_marker(c, BENCH)
        assert log_dir == supervision.logs_dir(BENCH)
        written = json.loads(c.writes[supervision._marker_path(BENCH)])
        assert written["supervisor"] == "supervisord"
        assert written["config_path"] == _CFG
        assert written["log_path"] == log_dir
        assert written["started_at"]


class TestConfigGeneration:
    def test_one_program_section_per_procfile_key(self):
        cfg = supervision.render_config(["web", "worker_default"], BENCH)
        assert "[program:web]" in cfg
        assert "[program:worker_default]" in cfg
        assert f'command=bash "{supervision._launcher_path(BENCH)}" web' in cfg
        assert f"directory={BENCH}" in cfg
        assert f"stdout_logfile={supervision.process_log_path(BENCH, 'web')}" in cfg
        # unix socket + rpcinterface so supervisorctl can talk to it.
        assert "[unix_http_server]" in cfg
        assert "[rpcinterface:supervisor]" in cfg

    def test_autorestart_toggle(self):
        assert "autorestart=unexpected" in supervision.render_config(
            ["web"], BENCH, autorestart=True
        )
        assert "autorestart=false" in supervision.render_config(["web"], BENCH, autorestart=False)

    def test_group_kill_for_grandchildren(self):
        cfg = supervision.render_config(["web"], BENCH)
        assert "stopasgroup=true" in cfg
        assert "killasgroup=true" in cfg


class TestBootstrap:
    def test_already_present_is_a_noop(self):
        c = FakeContainer(supervisor_present=True)
        assert supervision.ensure_supervisor_installed(c, BENCH) is False
        assert c.pip_installed is False

    def test_installs_when_absent(self):
        c = FakeContainer(supervisor_present=False, install_succeeds=True)
        assert supervision.ensure_supervisor_installed(c, BENCH) is True
        assert c.pip_installed is True

    def test_failed_install_raises_precondition(self):
        c = FakeContainer(supervisor_present=False, install_succeeds=False)
        with pytest.raises(CwcliError) as exc:
            supervision.ensure_supervisor_installed(c, BENCH)
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "supervisor.install_failed"


class TestLaunch:
    def test_launch_writes_config_and_launcher_and_starts_supervisord(self):
        c = FakeContainer()
        log_dir = supervision.launch(c, BENCH)
        assert log_dir == supervision.logs_dir(BENCH)
        # launcher + config dropped into the bench.
        assert supervision._launcher_path(BENCH) in c.writes
        assert supervision._config_path(BENCH) in c.writes
        assert "[program:web]" in c.writes[supervision._config_path(BENCH)]
        # supervisord launched detached over the generated config.
        assert len(c.launches) == 1
        assert "supervisor.supervisord" in c.launches[0]
        assert supervision._config_path(BENCH) in c.launches[0]

    def test_launch_installs_supervisor_when_absent(self):
        c = FakeContainer(supervisor_present=False)
        supervision.launch(c, BENCH)
        assert c.pip_installed is True
        assert c.launches, "supervisord must still launch after installing"

    def test_launch_fails_closed_when_install_fails(self):
        c = FakeContainer(supervisor_present=False, install_succeeds=False)
        with pytest.raises(CwcliError):
            supervision.launch(c, BENCH)
        assert c.launches == [], "must not launch supervisord if the install failed"


class TestSupervisorctl:
    def test_status_parses_state_and_pid_by_raw_key(self):
        c = FakeContainer()
        states = supervision.supervisorctl_states(c, BENCH)
        assert states["web"] == ("RUNNING", 101)
        assert states["worker_default"] == ("RUNNING", 105)

    def test_status_captures_fatal(self):
        ctl = "web RUNNING pid 101, uptime 0:05:00\nworker_default FATAL Exited too quickly\n"
        c = FakeContainer(ctl_status=ctl)
        states = supervision.supervisorctl_states(c, BENCH)
        assert states["worker_default"] == ("FATAL", None)
        by_label = supervision.states_by_label(states)
        assert by_label["worker:default"] == ("FATAL", None)

    def test_restart_program_targets_one(self):
        c = FakeContainer()
        supervision.restart_program(c, BENCH, "web")
        assert c.restarts == ["web"]


class TestWebProbe:
    def test_web_code_when_answering(self):
        assert supervision.web_http_code(FakeContainer(web_code="200")) == "200"

    def test_web_none_when_curl_fails(self):
        assert supervision.web_http_code(FakeContainer(web_ok=False)) is None
