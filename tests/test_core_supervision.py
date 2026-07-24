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

import inspect
import json
import os
import re

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
        configs=None,
        markers=None,
    ):
        # ``marker`` is the single-bench answer for every bench; ``markers`` is the
        # per-bench mapping a multi-bench test needs. Two parameters rather than one
        # overloaded value, because the marker IS a dict, so a dict cannot signal
        # "keyed by bench" the way it can for ``ctl_status``/``web_code``.
        self.markers = markers
        # Each bench's ``sites/common_site_config.json``, keyed by bench path - the
        # ONLY authority on which port a bench serves (bench's own make_ports writes
        # it). ``None`` for a bench means the read fails, which is how a test drives
        # the fail-honest "port unknown, never probed" path. A dict WITHOUT
        # ``webserver_port`` drives the defaulted-vs-explicit distinction.
        self.configs = (
            configs
            if configs is not None
            else {BENCH: {"webserver_port": 8000, "socketio_port": 9000}}
        )
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

    @staticmethod
    def _per_key(value, key):
        """A scalar answer, or a per-bench/per-port one when a dict is supplied.

        ``ctl_status`` and ``web_code`` are single-bench facts in the original fakes
        and stay scalars there. A MULTI-bench test needs each bench (or each web port)
        to answer differently - that is the whole point of the defect being guarded -
        so each also accepts a dict. (The marker uses ``markers=`` instead: it is
        itself a dict, so a dict cannot signal "keyed by bench" here.)
        """
        return value.get(key) if isinstance(value, dict) else value

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
            if path.endswith("/sites/common_site_config.json"):
                bench = path[: -len("/sites/common_site_config.json")]
                config = self.configs.get(bench)
                if config is None:
                    return (1, b"")
                return (0, json.dumps(config).encode())
            if path.endswith(supervision._MARKER_NAME):
                bench = path[: -len(f"/logs/{supervision._MARKER_NAME}")]
                marker = self.markers.get(bench) if self.markers is not None else self.marker
                if marker is None:
                    return (1, b"")
                return (0, json.dumps(marker).encode())
            return (0, b"")
        if head == "curl":
            port = int(cmd[-1].rsplit(":", 1)[1])
            code = self._per_key(self.web_code, port)
            if not self.web_ok or code is None:
                return (7, b"")
            return (0, code.encode())
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
        if supervision._MARK_PS in script:
            # The fused single-exec probe. Answered here rather than in a second
            # fake so a caller that mixes the fused read with the ordinary config
            # /marker reads (``core.status(fused=True)``) needs only ONE container.
            return self._exec_fused(script)
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
            # The script carries the bench's own control socket, so a dict-valued
            # ctl_status can answer per bench.
            bench = None
            if isinstance(self.ctl_status, dict):
                bench = next((b for b in self.ctl_status if b in script), None)
            body = self._per_key(self.ctl_status, bench)
            return (0, (body if body is not None else _CTL_SINGLE).encode())
        if "supervisor.supervisord" in script:
            self.launches.append(script)
            return (None, None) if detach else (0, b"")
        return (0, b"")

    def _exec_fused(self, script):
        """Stitch the fused script's marked sections exactly as a real shell would."""
        bench = (
            next((b for b in (self.ctl_status or {}) if b in script), None)
            if isinstance(self.ctl_status, dict)
            else None
        )
        ctl = self._per_key(self.ctl_status, bench)
        out = [
            supervision._MARK_PS,
            self.ps,
            f"{supervision._PS_RC_PREFIX}0",
            supervision._MARK_SUPCTL,
            ctl if ctl is not None else _CTL_SINGLE,
        ]
        if supervision._MARK_WEB in script:
            port = int(re.search(r"http://localhost:(\d+)", script).group(1))
            code = self._per_key(self.web_code, port)
            out += [supervision._MARK_WEB, "" if (not self.web_ok or code is None) else code]
        return (0, "\n".join(out).encode())


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

    def test_an_unreadable_process_check_fails_closed(self):
        c = FakeContainer()
        original = c.exec_run

        def fail_ps(cmd, **kwargs):
            if cmd[0] == "ps":
                return (1, b"ps unavailable")
            return original(cmd, **kwargs)

        c.exec_run = fail_ps

        with pytest.raises(CwcliError) as exc:
            supervision.stop_supervisor(c, BENCH, timeout=1)

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "supervisor.process_state_unknown"

    def test_a_supervisor_still_alive_after_sigkill_fails_closed(self, monkeypatch):
        c = FakeContainer()
        original = c.exec_run

        def fail_signals(cmd, **kwargs):
            if cmd[0] == "sh" and "kill" in cmd[2]:
                c.calls.append(cmd)
                return (1, b"signal failed")
            return original(cmd, **kwargs)

        c.exec_run = fail_signals
        monkeypatch.setattr(supervision.time, "sleep", lambda _: None)
        ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        monkeypatch.setattr(supervision.time, "time", lambda: next(ticks, 5.0))

        with pytest.raises(CwcliError) as exc:
            supervision.stop_supervisor(c, BENCH, timeout=1)

        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "supervisor.stop_failed"


class TestClearMarker:
    def test_removal_is_verified(self):
        c = FakeContainer()

        supervision.clear_marker(c, BENCH)

        assert ["rm", "-f", supervision._marker_path(BENCH)] in c.calls
        assert ["test", "!", "-e", supervision._marker_path(BENCH)] in c.calls

    def test_failed_removal_raises(self):
        c = FakeContainer()
        original = c.exec_run

        def fail_remove(cmd, **kwargs):
            if cmd[:2] == ["rm", "-f"]:
                return (1, b"permission denied")
            return original(cmd, **kwargs)

        c.exec_run = fail_remove

        with pytest.raises(CwcliError) as exc:
            supervision.clear_marker(c, BENCH)

        assert exc.value.code == "supervisor.marker_clear_failed"


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
        assert supervision.web_http_code(FakeContainer(web_code="200"), port=8000) == "200"

    def test_web_none_when_curl_fails(self):
        assert supervision.web_http_code(FakeContainer(web_ok=False), port=8000) is None

    def test_serving_true_for_any_http_code(self):
        # A bound port serving ANY code (even 404/5xx) is up (status's definition).
        assert supervision.web_is_serving(FakeContainer(web_code="404"), port=8000) is True
        assert supervision.web_is_serving(FakeContainer(web_code="200"), port=8000) is True

    def test_serving_false_when_unreachable_or_000(self):
        assert supervision.web_is_serving(FakeContainer(web_ok=False), port=8000) is False
        assert supervision.web_is_serving(FakeContainer(web_code="000"), port=8000) is False

    def test_wait_returns_immediately_when_serving(self):
        # First poll sees a serving port -> returns True without sleeping.
        assert supervision.wait_web_ready(FakeContainer(web_code="200"), port=8000) is True

    def test_wait_times_out_when_web_never_binds(self):
        # A web that never serves returns False within the bounded timeout (tiny
        # timeout so the test is fast; proves it does not hang).
        assert (
            supervision.wait_web_ready(
                FakeContainer(web_ok=False), port=8000, timeout=0.05, interval=0.01
            )
            is False
        )

    def test_wait_binds_after_a_few_polls(self):
        # Flips to serving mid-wait: proves the poll loop actually retries.
        c = FakeContainer(web_ok=False)
        polls = {"n": 0}
        orig = c.web_ok

        def flip(cmd, **kw):
            if cmd and cmd[0] == "curl":
                polls["n"] += 1
                if polls["n"] >= 3:
                    c.web_ok = True
            return type(c).exec_run(c, cmd, **kw)

        c.exec_run = flip  # type: ignore[method-assign]
        assert supervision.wait_web_ready(c, port=8000, timeout=5, interval=0.01) is True
        assert polls["n"] >= 3
        c.web_ok = orig

    def test_the_probe_asks_the_port_it_was_given(self):
        # The whole fix: the URL carries the CALLER's port, not a hardcoded 8000.
        # A bench past the first serves 8001, and probing 8000 measures a sibling.
        c = FakeContainer(web_code="200")
        supervision.web_http_code(c, port=8001)
        curl = next(cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl")
        assert "http://localhost:8001" in curl
        assert not any("8000" in part for part in curl)

    def test_the_probe_names_the_site_as_the_host_header(self):
        # Frappe is multi-tenant and routes by Host. A request naming no site is
        # answered 404 by a perfectly healthy bench, which is what `status` used to
        # print for every bench on every read. Naming the site makes the probe the
        # request a real user sends.
        c = FakeContainer(web_code="200")
        supervision.web_http_code(c, port=8001, site="two.localhost")
        curl = next(cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl")
        assert "-H" in curl
        assert "Host: two.localhost" in curl
        # The URL still targets localhost on the bench's own port - the Host header
        # is what selects the site, never the URL authority.
        assert "http://localhost:8001" in curl

    def test_no_site_keeps_the_old_host_less_request(self):
        # An unresolvable site probes as before rather than guessing one.
        c = FakeContainer(web_code="404")
        supervision.web_http_code(c, port=8000)
        curl = next(cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl")
        assert "-H" not in curl

    def test_none_of_the_three_probes_declares_a_default_port(self):
        # A "convenience" default is exactly how the bug arrived: web_http_code was
        # written for a single-bench world, and every later caller correctly passed
        # nothing. With no default the bug is unrepresentable, so re-adding one must
        # be a test failure rather than a silent re-arming.
        for fn in (
            supervision.web_http_code,
            supervision.web_is_serving,
            supervision.wait_web_ready,
        ):
            param = inspect.signature(fn).parameters["port"]
            assert param.default is inspect.Parameter.empty, fn.__name__
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__


class FusedFakeContainer:
    """A container whose ONE ``bash -c`` exec answers the fused-probe script by
    stitching together canned ``ps``/``supervisorctl``/``curl`` output - exactly as
    a real shell running that same script would produce. This exercises the real
    marker-split + parse path, not a shortcut: a wrong marker or a wrong section
    order in ``_fused_script``/``fused_probe`` would break these tests too.
    """

    def __init__(
        self,
        *,
        ps=_PS_SINGLE,
        ctl=_CTL_SINGLE,
        web_code="200",
        web_ok=True,
        exit_code=0,
        include_ps_marker=True,
        include_ps_rc=True,
        ps_rc=0,
        include_supctl_marker=True,
        include_web_marker=True,
    ):
        self.ps = ps
        self.ctl = ctl
        self.web_code = web_code
        self.web_ok = web_ok
        self.curl_output = web_code if web_ok else "000"
        self.exit_code = exit_code
        self.include_ps_marker = include_ps_marker
        self.include_ps_rc = include_ps_rc
        self.ps_rc = ps_rc
        self.include_supctl_marker = include_supctl_marker
        self.include_web_marker = include_web_marker
        self.calls: list = []

    def exec_run(self, cmd):
        self.calls.append(cmd)
        assert cmd[:2] == ["bash", "-c"], "the fused probe must be one bash -c exec"
        script = cmd[2]
        out = []
        if self.include_ps_marker:
            out += [supervision._MARK_PS, self.ps]
        if self.include_ps_rc:
            out += [f"{supervision._PS_RC_PREFIX}{self.ps_rc}"]
        if self.include_supctl_marker:
            out += [supervision._MARK_SUPCTL, self.ctl]
        if supervision._MARK_WEB in script and self.include_web_marker:
            published_code = self.curl_output if self.web_ok else ""
            out += [supervision._MARK_WEB, published_code]
        return (self.exit_code, "\n".join(out).encode())


class TestFusedProbe:
    """``fused_probe`` - process liveness+state and the web check in ONE exec."""

    def test_one_exec_returns_processes_state_and_web(self):
        c = FusedFakeContainer()
        probe = supervision.fused_probe(c, BENCH, web_port=8000, web_site="x.localhost")

        assert len(c.calls) == 1, "the whole probe must be exactly one docker exec"
        assert probe.supervisor_up is True
        assert probe.supervisor_pid == 100
        labels = {p.label for p in probe.processes}
        assert labels == {
            "web",
            "socketio",
            "schedule",
            "watch",
            "worker:default",
            "redis_cache",
            "redis_queue",
        }
        web = next(p for p in probe.processes if p.label == "web")
        assert web.up is True
        assert web.pid == 101
        assert web.uptime_s == 499
        assert web.state == "RUNNING"
        assert probe.web_http_code == "200"
        assert probe.web_probed is True

    def test_the_web_request_carries_the_given_port_and_site(self):
        c = FusedFakeContainer()
        supervision.fused_probe(c, BENCH, web_port=8001, web_site="two.localhost")
        script = c.calls[0][2]
        assert "http://localhost:8001" in script
        assert "Host: two.localhost" in script
        assert "--connect-timeout 2" in script
        assert "--max-time 5" in script

    def test_a_fatal_program_with_no_live_pid_is_reported_down_with_its_state(self):
        # worker_default crash-looped: supervisorctl still lists it (FATAL), ps has
        # no live row for it at all. supervisorctl's own enumeration is what makes
        # this reportable without a separate Procfile read.
        ps_without_worker = "\n".join(
            line for line in _PS_SINGLE.splitlines() if "bench worker" not in line
        )
        ctl = _CTL_SINGLE + "worker_default   FATAL   Exited too quickly\n"
        # Replace the single stale RUNNING line for worker_default with the FATAL one.
        ctl = "\n".join(line for line in ctl.splitlines() if "worker_default   RUNNING" not in line)
        c = FusedFakeContainer(ps=ps_without_worker, ctl=ctl, exit_code=3)
        probe = supervision.fused_probe(c, BENCH, web_port=None)
        worker = next(p for p in probe.processes if p.label == "worker:default")
        assert worker.up is False
        assert worker.state == "FATAL"

    def test_no_web_port_skips_the_web_section_and_is_honest_none(self):
        c = FusedFakeContainer()
        probe = supervision.fused_probe(c, BENCH, web_port=None)
        assert probe.web_http_code is None
        assert probe.web_probed is False
        assert supervision._MARK_WEB not in c.calls[0][2]

    def test_probe_web_false_skips_the_web_section_even_with_a_port(self):
        c = FusedFakeContainer()
        probe = supervision.fused_probe(c, BENCH, web_port=8000, probe_web=False)
        assert probe.web_http_code is None
        assert probe.web_probed is False
        assert supervision._MARK_WEB not in c.calls[0][2]

    def test_curl_failure_is_honest_none_not_a_fabricated_code(self):
        c = FusedFakeContainer(web_ok=False)
        probe = supervision.fused_probe(c, BENCH, web_port=8000)
        assert c.curl_output == "000"
        assert probe.web_http_code is None
        assert probe.web_probed is True  # a check WAS attempted; it just failed

    @pytest.mark.parametrize(
        "container",
        [
            FusedFakeContainer(include_ps_marker=False),
            FusedFakeContainer(include_ps_rc=False),
            FusedFakeContainer(ps_rc=1),
            FusedFakeContainer(include_supctl_marker=False),
            FusedFakeContainer(include_web_marker=False),
            FusedFakeContainer(ps="not parseable"),
        ],
    )
    def test_an_unverifiable_fused_read_fails_closed(self, container):
        with pytest.raises(CwcliError) as exc:
            supervision.fused_probe(container, BENCH, web_port=8000)
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "supervisor.process_state_unknown"

    def test_empty_supervisor_states_with_a_live_supervisor_fail_closed(self):
        c = FusedFakeContainer(ctl="unix:///tmp/supervisor.sock refused connection\n")
        with pytest.raises(CwcliError) as exc:
            supervision.fused_probe(c, BENCH, web_port=8000)
        assert exc.value.kind is ErrorKind.PRECONDITION
        assert exc.value.code == "supervisor.process_state_unknown"

    def test_a_supervisord_without_a_config_is_ignored_without_a_second_exec(self):
        ps = _PS_SINGLE.replace(f" -c {_CFG}", "")
        c = FusedFakeContainer(ps=ps)
        probe = supervision.fused_probe(c, BENCH, web_port=None)
        assert len(c.calls) == 1
        assert probe.supervisor_up is False

    def test_no_supervisor_found_reports_down_but_the_web_answer_still_lands(self):
        # supervisord absent for this bench: process state is honestly unknown/down,
        # but the web curl is an independent section of the same exec and still
        # answers - "no supervisor" must not silently swallow the web result too.
        c = FusedFakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n")
        probe = supervision.fused_probe(c, BENCH, web_port=8000)
        assert probe.supervisor_up is False
        assert probe.supervisor_pid is None
        assert probe.processes == []
        assert probe.web_http_code == "200"
        assert probe.web_probed is True

    def test_the_script_carries_no_mutating_commands(self):
        # Static, structural read-only guarantee: the exact script sent to the
        # container is built entirely from read-only primitives (ps, supervisorctl
        # status, curl) - never restart/stop/kill/rm, regardless of container state.
        script = supervision._fused_script(BENCH, web_port=8000, web_site="x.localhost")
        for mutating in ("restart", "stop", " kill ", "rm -f", "supervisord -c"):
            assert mutating not in script, f"unexpected mutating token: {mutating!r}"
        assert "supervisorctl" in script
        assert " status" in script
        assert "curl" in script

    def test_read_only_across_many_cycles_state_is_unchanged(self):
        # The unit-level proxy for read-only-ness: many probe cycles against the
        # same fake never mutate its recorded ps/ctl/web fixtures or the container's
        # own state - only the real-bench E2E proof can confirm no live PID/state
        # changes, but this at least proves the probe never WRITES anything locally.
        c = FusedFakeContainer()
        before = (c.ps, c.ctl, c.web_code)
        for _ in range(20):
            supervision.fused_probe(c, BENCH, web_port=8000, web_site="x.localhost")
        assert (c.ps, c.ctl, c.web_code) == before
        assert len(c.calls) == 20
