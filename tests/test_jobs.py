"""Durable detached jobs: persistence, identity checks, and core dispatch."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from caffeinated_whale_cli import jobs
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.core.update import UpdatePreflight
from caffeinated_whale_cli.core.update_preflight import DATA_LOSS, PreflightFinding


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "cwcli"))


@pytest.fixture
def fake_spawn(monkeypatch):
    calls = []

    class Process:
        pid = 4242
        returncode = None

        def poll(self):
            return self.returncode

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    jobs._CHILDREN.clear()

    monkeypatch.setattr(jobs.subprocess, "Popen", spawn)
    monkeypatch.setattr(jobs.daemon_identity, "pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs.daemon_identity, "process_start_time", lambda pid: f"start-{pid}")
    return calls


def test_create_persists_no_secrets_and_passes_them_only_to_init_worker(fake_spawn):
    record = jobs.create_job(
        "init",
        {"project": "new-site"},
        {"version": "16"},
        secret_values={"admin_password": "top-secret", "db_root_password": "db-secret"},
        ui_context={"token": "daemon-token"},
    )

    directory = jobs.job_dir(record["id"])
    persisted = (directory / "spec.json").read_text()
    assert "top-secret" not in persisted
    assert "db-secret" not in persisted
    assert "daemon-token" not in fake_spawn[0][1]["env"].values()
    assert fake_spawn[0][1]["env"]["CWCLI_JOB_ADMIN_PASSWORD"] == "top-secret"
    assert fake_spawn[0][1]["env"]["CWCLI_JOB_DB_ROOT_PASSWORD"] == "db-secret"
    assert fake_spawn[0][1]["start_new_session"] is True
    assert record["state"] == "queued"


def test_a_secret_named_anywhere_in_the_spec_is_refused(fake_spawn):
    with pytest.raises(CwcliError) as error:
        jobs.create_job(
            "backup",
            {"project": "p"},
            {"nested": [{"api_token": "must-not-land"}]},
        )

    assert error.value.code == "job.secret_in_spec"
    assert fake_spawn == []


def test_only_one_mutating_job_per_project_can_be_active(fake_spawn):
    first = jobs.create_job("backup", {"project": "p", "site": "one.localhost"}, {})

    with pytest.raises(CwcliError) as error:
        jobs.create_job("migrate", {"project": "p", "site": "one.localhost"}, {})

    assert error.value.code == "job.project_busy"
    assert error.value.detail == {"job_id": first["id"]}


def test_a_vanished_process_becomes_unknown_instead_of_failed(fake_spawn, monkeypatch):
    record = jobs.create_job("backup", {"project": "p"}, {})
    state_path = jobs.job_dir(record["id"]) / "state.json"
    state = json.loads(state_path.read_text())
    state["created_epoch"] = time.time() - 10
    jobs._write_json(state_path, state)
    monkeypatch.setattr(jobs.daemon_identity, "pid_alive", lambda pid: False)

    refreshed = jobs.read_job(record["id"])

    assert refreshed["state"] == "unknown"
    assert refreshed["error"]["code"] == "job.process_vanished"
    assert "safe to retry" in refreshed["error"]["hint"]


def test_interrupt_signals_only_the_recorded_process_identity(fake_spawn, monkeypatch):
    record = jobs.create_job("backup", {"project": "p"}, {})
    signalled = []
    monkeypatch.setattr(jobs.daemon_identity, "pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs.os, "killpg", lambda pid, signal: signalled.append((pid, signal)))

    interrupted = jobs.interrupt_job(record["id"])

    assert interrupted["state"] == "interrupting"
    assert interrupted["interrupt_requested_at"]
    assert signalled == [(4242, jobs.signal.SIGTERM)]


def test_interrupt_refuses_a_reused_pid(fake_spawn, monkeypatch):
    record = jobs.create_job("backup", {"project": "p"}, {})
    monkeypatch.setattr(jobs.daemon_identity, "pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs.daemon_identity, "process_start_time", lambda pid: "different")
    monkeypatch.setattr(jobs.os, "killpg", lambda *args: pytest.fail("reused pid was signalled"))

    refreshed = jobs.interrupt_job(record["id"])

    assert refreshed["state"] == "unknown"


def test_interrupt_keeps_the_result_of_a_job_that_finished_meanwhile(fake_spawn, monkeypatch):
    record = jobs.create_job("backup", {"project": "p"}, {})
    refresh = jobs._refresh_unknown

    def worker_finishes_during_the_check(job_id, state):
        checked = refresh(job_id, state)
        jobs._merge_state(job_id, state="succeeded", result={"ok": True})
        return checked

    monkeypatch.setattr(jobs, "_refresh_unknown", worker_finishes_during_the_check)
    monkeypatch.setattr(jobs.os, "killpg", lambda *args: pytest.fail("finished job was signalled"))

    refreshed = jobs.interrupt_job(record["id"])

    assert refreshed["state"] == "succeeded"
    assert refreshed["result"] == {"ok": True}
    assert "interrupt_requested_at" not in refreshed


def test_interrupt_tolerates_a_process_that_exits_before_the_signal(fake_spawn, monkeypatch):
    record = jobs.create_job("backup", {"project": "p"}, {})

    def gone(pid, signal):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(jobs.os, "killpg", gone)

    assert jobs.interrupt_job(record["id"])["state"] == "interrupting"


def test_restart_asks_which_bench_before_stopping_anything(fake_spawn, monkeypatch):
    choice = Result(status=Status.NEEDS_CHOICE)
    monkeypatch.setattr(jobs.core_resolvers, "resolve_bench", lambda *args: choice)
    monkeypatch.setattr(jobs.core_stop, "stop", lambda *a, **k: pytest.fail("stopped first"))

    assert jobs._restart_instance({"project": "p"}, lambda event: None) is choice


def test_restart_starts_the_bench_it_resolved_before_the_stop(fake_spawn, monkeypatch):
    calls = []
    monkeypatch.setattr(
        jobs.core_resolvers,
        "resolve_bench",
        lambda project, bench, path: Result(status=Status.OK, data=f"/workspace/{bench}"),
    )
    monkeypatch.setattr(
        jobs.core_stop,
        "stop",
        lambda project, **kwargs: calls.append("stop") or Result(status=Status.OK),
    )
    monkeypatch.setattr(
        jobs.core_start,
        "start",
        lambda project, **kwargs: calls.append(kwargs["bench_path"]) or Result(status=Status.OK),
    )

    jobs._restart_instance({"project": "p", "bench": "b1"}, lambda event: None)

    assert calls == ["stop", "/workspace/b1"]


def test_run_job_records_typed_events_and_terminal_result(fake_spawn, monkeypatch):
    record = jobs.create_job("backup", {"project": "p"}, {})

    @dataclass(frozen=True)
    class Event:
        phase: str
        message: str

    def dispatch(_verb, _target, _args, _consent, _secrets, emit):
        emit(Event(phase="backup", message="Database dump created"))
        return Result(status=Status.OK, data={"ok": True, "archive": "site.sql.gz"})

    monkeypatch.setattr(jobs, "_dispatch", dispatch)

    assert jobs.run_job(record["id"]) == 0
    finished = jobs.read_job(record["id"], include_events=True)
    assert finished["state"] == "succeeded"
    assert finished["result"]["archive"] == "site.sql.gz"
    assert [event["type"] for event in finished["events"]] == [
        "job_started",
        "event",
        "job_finished",
    ]


def test_apps_update_uses_the_cli_preflight_and_records_its_findings(fake_spawn, monkeypatch):
    record = jobs.create_job(
        "apps-update",
        {"project": "p", "bench": "0"},
        {"apps": ["frappe"]},
    )
    seen = {}

    def update(project, apps, **kwargs):
        seen.update({"project": project, "apps": apps, **kwargs})
        kwargs["on_event"](
            UpdatePreflight(
                findings=[
                    PreflightFinding(
                        severity=DATA_LOSS,
                        code="git.unpushed",
                        app="frappe",
                        site=None,
                        message="One commit is not on the remote.",
                        remedy="Push the commit first.",
                    )
                ],
                summary="1 app(s), 1 site(s)",
                blocked=False,
            )
        )
        return Result(status=Status.OK, data={"ok": True})

    monkeypatch.setattr(jobs.core_update, "update", update)
    recorder = jobs._Recorder(record["id"], [])

    result = jobs._dispatch(
        "apps-update", {"project": "p", "bench": "0"}, {"apps": ["frappe"]}, False, {}, recorder
    )

    assert result.data == {"ok": True}
    assert seen["preflight"] is True
    events = jobs.read_job(record["id"], include_events=True)["events"]
    assert events[-1]["type"] == "update_preflight"
    assert events[-1]["data"]["findings"][0]["code"] == "git.unpushed"


def test_a_frozen_exe_spawns_its_own_binary_with_the_hidden_ui_job_flag(fake_spawn, monkeypatch):
    monkeypatch.setattr(jobs, "_is_frozen", lambda: True)
    monkeypatch.setattr(jobs.sys, "argv", ["C:\\cwcli\\cwcli.exe", "ui"])

    record = jobs.create_job("backup", {"project": "p"}, {})

    assert fake_spawn[0][0] == ["C:\\cwcli\\cwcli.exe", "ui", "--job", record["id"]]


def test_a_normal_install_spawns_the_jobs_module(fake_spawn, monkeypatch):
    monkeypatch.setattr(jobs, "_is_frozen", lambda: False)

    record = jobs.create_job("backup", {"project": "p"}, {})

    assert fake_spawn[0][0] == [
        jobs.sys.executable,
        "-m",
        "caffeinated_whale_cli.jobs",
        "run",
        record["id"],
    ]


def test_output_storms_do_not_rewrite_state_per_event(fake_spawn, monkeypatch):
    """A migrate repaints its progress bar hundreds of times; only the event log grows."""
    record = jobs.create_job("backup", {"project": "p"}, {})
    recorder = jobs._Recorder(record["id"], [])
    writes = []
    real_merge = jobs._merge_state
    monkeypatch.setattr(
        jobs, "_merge_state", lambda job_id, **kw: writes.append(kw) or real_merge(job_id, **kw)
    )
    clock = iter([10.0, 10.0, 10.1, 10.2, 10.3])
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(clock))

    @dataclass(frozen=True)
    class Output:
        text: str

    recorder.lifecycle("job_started", "started")  # lifecycle events always flush
    for i in range(3):
        recorder(Output(text=f"progress {i}%"))

    assert [w["event_count"] for w in writes] == [1]
    events = jobs.read_job(record["id"], include_events=True)["events"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[-1]["message"] == "progress 2%"
    # The trailing timer catches the count up once the storm goes quiet.
    assert recorder._pending is not None
    recorder._pending.cancel()
    recorder.flush()
    assert writes[-1]["event_count"] == 4


def test_the_daemon_reaps_exited_workers_so_they_stop_reading_as_alive(fake_spawn):
    first = jobs.create_job("backup", {"project": "p"}, {})
    second = jobs.create_job("backup", {"project": "q"}, {})
    jobs._CHILDREN[first["id"]].returncode = 0

    jobs.reap_children()

    assert set(jobs._CHILDREN) == {second["id"]}
