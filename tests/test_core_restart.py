"""``core.restart_process`` branch coverage against faked I/O.

Every fork: a single-program restart (siblings untouched), a restart of a
down/FATAL program (``old_pid`` None), the unknown-label ``NEEDS_CHOICE`` listing
valid labels, the multi-bench ``NEEDS_CHOICE`` sharing start/status's selector, an
explicit ``bench_path`` honored verbatim, the not-started / container-down
``NOT_RUNNING`` raises, and the missing-project / docker-unreachable hard errors.
"""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import restart as core_restart
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests.test_core_supervision import _CTL_SINGLE, _PS_SINGLE, BENCH, FakeContainer


@pytest.fixture
def wire(monkeypatch):
    def _wire(frappe, *, benches=None):
        containers = [frappe] if frappe is not None else []
        monkeypatch.setattr(core_restart, "get_project_containers", lambda name: containers)
        monkeypatch.setattr(
            resolvers.db_utils,
            "get_cached_project_data",
            lambda name: {"bench_instances": benches} if benches is not None else None,
        )

    return _wire


class TestSingleProgram:
    def test_restart_one_leaves_siblings_untouched(self, wire):
        c = FakeContainer()  # supervisord up at BENCH, all RUNNING
        wire(c, benches=[{"path": BENCH}])
        result = core_restart.restart_process("proj", "web")
        assert result.status is Status.OK
        out = result.data
        assert out.label == "web"
        assert out.old_pid == 101
        assert out.new_pid == 101
        assert out.supervisor_state == "RUNNING"
        # ONLY web was restarted; every sibling was left alone.
        assert c.restarts == ["web"]

    def test_restart_down_program_reports_old_pid_none(self, wire):
        # worker not in ps and FATAL in supervisorctl -> old_pid None, then restarted.
        ps = _PS_SINGLE.replace(
            "105 100 499 0.3 70000 /env/bin/python /env/bin/bench worker --queue default\n", ""
        )
        ctl = _CTL_SINGLE.replace(
            "worker_default   RUNNING   pid 105, uptime 0:05:00\n",
            "worker_default   FATAL   Exited too quickly\n",
        )
        c = FakeContainer(ps=ps, ctl_status=ctl)
        wire(c, benches=[{"path": BENCH}])
        result = core_restart.restart_process("proj", "worker:default")
        assert result.status is Status.OK
        assert result.data.old_pid is None
        assert result.data.label == "worker:default"
        assert c.restarts == ["worker_default"]

    def test_explicit_bench_path_is_used_verbatim(self, wire):
        c = FakeContainer()
        # multi-bench cache present, but bench_path must win verbatim.
        wire(c, benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_restart.restart_process("proj", "web", bench_path=BENCH)
        assert result.status is Status.OK
        assert result.data.bench_path == BENCH


class TestChoices:
    def test_unknown_process_returns_select_process(self, wire):
        c = FakeContainer()
        wire(c, benches=[{"path": BENCH}])
        result = core_restart.restart_process("proj", "wroker")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_process"
        labels = {o["label"] for o in result.choice.options}
        assert "web" in labels and "worker:default" in labels
        # Nothing was restarted on an unresolved choice.
        assert c.restarts == []

    def test_multi_bench_returns_select_bench(self, wire):
        c = FakeContainer()
        wire(c, benches=[{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = core_restart.restart_process("proj", "web")
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert c.restarts == []


class TestNotRunning:
    def test_bench_not_started_raises_not_running(self, wire):
        c = FakeContainer(ps="1 0 5 0.0 1000 /sbin/init\n", cwds={})
        wire(c, benches=[{"path": BENCH}])
        with pytest.raises(CwcliError) as exc:
            core_restart.restart_process("proj", "web")
        assert exc.value.kind is ErrorKind.NOT_RUNNING
        assert exc.value.code == "supervisor.not_running"

    def test_container_down_raises_not_running(self, wire):
        c = FakeContainer()
        c.status = "exited"
        wire(c, benches=[{"path": BENCH}])
        with pytest.raises(CwcliError) as exc:
            core_restart.restart_process("proj", "web")
        assert exc.value.kind is ErrorKind.NOT_RUNNING
        assert exc.value.code == "container.not_running"


class TestHardErrors:
    def test_missing_project_raises_not_found(self, wire):
        wire(None)
        with pytest.raises(CwcliError) as exc:
            core_restart.restart_process("ghost", "web")
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_docker_unreachable_raises_docker(self, monkeypatch):
        monkeypatch.setattr(core_restart, "get_project_containers", lambda name: None)
        with pytest.raises(CwcliError) as exc:
            core_restart.restart_process("proj", "web")
        assert exc.value.kind is ErrorKind.DOCKER
