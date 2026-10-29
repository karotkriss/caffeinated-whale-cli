"""Durable detached jobs: persistence, identity checks, and core dispatch."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli import jobs
from caffeinated_whale_cli.core import supervision
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError
from caffeinated_whale_cli.core.update import UpdatePreflight
from caffeinated_whale_cli.core.update_preflight import DATA_LOSS, PreflightFinding

_real_discover_stack = supervision.discover_stack


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "cwcli"))
    jobs._RETRY_SECRETS.clear()


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


def _finish(job_id, state="failed", **changes):
    jobs._merge_state(job_id, state=state, finished_at=jobs._now(), **changes)


def test_retry_starts_a_new_job_with_the_same_inputs_and_secrets(fake_spawn):
    first = jobs.create_job(
        "init",
        {"project": "new-site"},
        {"port": 28000, "install_erpnext": False},
        secret_values={"admin_password": "top-secret", "db_root_password": "db-secret"},
    )
    _finish(first["id"])
    assert jobs.read_job(first["id"])["retryable"] is True

    again = jobs.retry_job(first["id"], ui_context={"token": "daemon-token"})

    assert again["id"] != first["id"]
    assert (again["verb"], again["target"], again["args"]) == (
        "init",
        {"project": "new-site"},
        {"port": 28000, "install_erpnext": False},
    )
    env = fake_spawn[-1][1]["env"]
    assert env["CWCLI_JOB_ADMIN_PASSWORD"] == "top-secret"
    assert env["CWCLI_JOB_DB_ROOT_PASSWORD"] == "db-secret"
    assert "top-secret" not in (jobs.job_dir(again["id"]) / "spec.json").read_text()
    # The retry is itself retryable once it fails, with the same held secrets.
    _finish(again["id"])
    assert jobs.retry_job(again["id"])["id"] not in {first["id"], again["id"]}
    assert fake_spawn[-1][1]["env"]["CWCLI_JOB_ADMIN_PASSWORD"] == "top-secret"


def test_retry_never_carries_consent_over(fake_spawn):
    first = jobs.create_job("rm-site", {"project": "p", "site": "s.localhost"}, {}, consent=True)
    _finish(first["id"])

    again = jobs.retry_job(first["id"])

    assert again["consent"] is False


@pytest.mark.parametrize(
    "verb, args",
    [("apps-checkout", {"ref": "main", "reset": True}), ("apps-update", {"force": True})],
)
def test_retry_refuses_a_job_whose_page_confirmation_it_would_skip(fake_spawn, verb, args):
    first = jobs.create_job(verb, {"project": "p", "app": "erpnext"}, args)
    _finish(first["id"])

    assert jobs.read_job(first["id"])["retryable"] is False
    with pytest.raises(CwcliError) as error:
        jobs.retry_job(first["id"])

    assert error.value.code == "job.retry_guarded"


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "needs_choice", "unknown"])
def test_only_a_failed_job_can_be_retried(fake_spawn, state):
    first = jobs.create_job("backup", {"project": "p"}, {})
    _finish(first["id"], state=state)

    assert jobs.read_job(first["id"])["retryable"] is False
    assert jobs.read_job(first["id"])["retry_refusal"] is None
    with pytest.raises(CwcliError) as error:
        jobs.retry_job(first["id"])

    assert error.value.code == "job.retry_not_failed"


def test_an_interrupted_job_has_no_retry_only_the_check_first_note(fake_spawn):
    first = jobs.create_job("apps-install", {"project": "p", "app": "erpnext"}, {})
    _finish(first["id"], state="interrupted")

    record = jobs.read_job(first["id"])
    assert record["retryable"] is False
    assert "may still be running" in record["retry_refusal"]
    with pytest.raises(CwcliError) as error:
        jobs.retry_job(first["id"])

    assert error.value.code == "job.retry_interrupted"
    assert len(fake_spawn) == 1


@pytest.mark.parametrize("key", ["unknown_apps", "unknown_migrations", "unknown_builds"])
def test_an_update_with_a_step_that_may_still_be_running_has_no_retry(fake_spawn, key):
    first = jobs.create_job("apps-update", {"project": "p", "app": "erpnext"}, {})
    _finish(first["id"], result={"failed_apps": [], key: ["erpnext"]})

    record = jobs.read_job(first["id"])
    assert record["retryable"] is False
    assert "may still be running" in record["retry_refusal"]
    with pytest.raises(CwcliError) as error:
        jobs.retry_job(first["id"])

    assert error.value.code == "job.retry_unknown_outcome"
    assert len(fake_spawn) == 1


@pytest.mark.parametrize("code", ["exec.stream_lost", "exec.exit_code_unknown"])
@pytest.mark.parametrize("verb", ["migrate", "init", "apps-install"])
def test_a_job_whose_container_command_may_still_be_running_has_no_retry(fake_spawn, verb, code):
    first = jobs.create_job(
        verb,
        {"project": "p", "site": "s", "app": "erpnext"},
        {},
        secret_values={"admin_password": "x"},
    )
    _finish(first["id"], error={"type": "docker", "code": code, "message": "lost"})

    record = jobs.read_job(first["id"])
    assert record["retryable"] is False
    assert "may still be running" in record["retry_refusal"]
    with pytest.raises(CwcliError) as error:
        jobs.retry_job(first["id"])

    assert error.value.code == "job.retry_unknown_outcome"
    assert len(fake_spawn) == 1


def test_an_update_whose_steps_definitely_failed_can_be_retried(fake_spawn):
    first = jobs.create_job("apps-update", {"project": "p", "app": "erpnext"}, {})
    _finish(first["id"], result={"failed_apps": ["erpnext"], "unknown_apps": []})

    assert jobs.read_job(first["id"])["retryable"] is True
    assert jobs.retry_job(first["id"])["verb"] == "apps-update"


def test_retrying_an_init_whose_passwords_are_gone_says_so(fake_spawn):
    first = jobs.create_job(
        "init", {"project": "p"}, {}, secret_values={"admin_password": "top-secret"}
    )
    _finish(first["id"])
    jobs._RETRY_SECRETS.clear()  # what a daemon restart does

    with pytest.raises(CwcliError) as error:
        jobs.retry_job(first["id"])

    assert error.value.code == "job.retry_secrets_missing"
    assert len(fake_spawn) == 1


def test_a_retried_removal_confirmed_by_name_keeps_the_typed_password(fake_spawn):
    first = jobs.create_job(
        "rm-site",
        {"project": "p", "site": "s.localhost"},
        {},
        consent=True,
        secret_values={"db_root_password": "typed-root"},
    )
    _finish(first["id"])
    retried = jobs.retry_job(first["id"])
    _finish(retried["id"], state="needs_choice", choice={"param": "consent"})

    confirmed = jobs.continue_job(
        retried["id"], {"project": "p", "site": "s.localhost"}, {}, consent=True
    )

    assert confirmed["consent"] is True
    assert fake_spawn[-1][1]["env"]["CWCLI_JOB_DB_ROOT_PASSWORD"] == "typed-root"
    assert "typed-root" not in (jobs.job_dir(confirmed["id"]) / "spec.json").read_text()
    # The confirmed run fails; answering the same waiting job again still has the password.
    _finish(confirmed["id"])
    again = jobs.continue_job(
        retried["id"], {"project": "p", "site": "s.localhost"}, {}, consent=True
    )
    assert fake_spawn[-1][1]["env"]["CWCLI_JOB_DB_ROOT_PASSWORD"] == "typed-root"
    _finish(again["id"], state="succeeded")
    jobs.read_job(again["id"])
    assert again["id"] not in jobs._RETRY_SECRETS


def test_an_init_that_asks_to_reuse_its_bench_continues_with_its_passwords(fake_spawn):
    first = jobs.create_job(
        "init",
        {"project": "p"},
        {"port": 28000},
        secret_values={"admin_password": "admin-x", "db_root_password": "root-x"},
    )
    _finish(first["id"], state="needs_choice", choice={"param": "reuse_bench"})

    reused = jobs.continue_job(first["id"], {"project": "p"}, {"port": 28000, "reuse_bench": True})

    assert (reused["verb"], reused["args"]) == ("init", {"port": 28000, "reuse_bench": True})
    env = fake_spawn[-1][1]["env"]
    assert env["CWCLI_JOB_ADMIN_PASSWORD"] == "admin-x"
    assert env["CWCLI_JOB_DB_ROOT_PASSWORD"] == "root-x"


def test_continuing_refuses_a_job_that_is_not_waiting_or_changes_instance(fake_spawn):
    first = jobs.create_job("rm-site", {"project": "p", "site": "s"}, {})
    _finish(first["id"])
    with pytest.raises(CwcliError) as error:
        jobs.continue_job(first["id"], {"project": "p", "site": "s"}, {}, consent=True)
    assert error.value.code == "job.continue_not_pending"

    _finish(first["id"], state="needs_choice")
    with pytest.raises(CwcliError) as error:
        jobs.continue_job(first["id"], {"project": "other", "site": "s"}, {}, consent=True)
    assert error.value.code == "job.continue_project_changed"
    assert len(fake_spawn) == 1


def test_a_job_that_succeeded_no_longer_holds_its_secrets(fake_spawn):
    first = jobs.create_job("init", {"project": "p"}, {}, secret_values={"admin_password": "x"})
    assert jobs._RETRY_SECRETS[first["id"]] == {"admin_password": "x"}
    _finish(first["id"], state="succeeded")

    jobs.read_job(first["id"])

    assert first["id"] not in jobs._RETRY_SECRETS


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


def _wire_instance_restart(monkeypatch, frappe_status, serving, unsupervised=()):
    calls = []
    frappe = _Container("frappe", frappe_status)
    monkeypatch.setattr(
        jobs.core_resolvers,
        "cached_benches",
        lambda project: [{"path": f"/workspace/b{i}"} for i in range(3)],
    )
    monkeypatch.setattr(jobs.core_docker, "get_frappe_container", lambda project: frappe)

    def discover(container, path, required=False):
        assert required is True
        calls.append(("probe", path))
        return SimpleNamespace(supervisor_up=path in serving)

    monkeypatch.setattr(jobs.core_supervision, "discover_stack", discover)

    def discover_unsupervised(container, path, required=False):
        assert required is True
        return SimpleNamespace(manager_up=path in unsupervised)

    monkeypatch.setattr(jobs.core_supervision, "discover_unsupervised_stack", discover_unsupervised)
    monkeypatch.setattr(
        jobs.core_stop,
        "stop",
        lambda project, **kwargs: calls.append("stop") or Result(status=Status.OK),
    )
    monkeypatch.setattr(jobs.core_start, "resolve_project_containers", lambda project: [frappe])
    monkeypatch.setattr(
        jobs,
        "_start_containers_only",
        lambda project, containers: calls.append("containers") or [],
    )
    monkeypatch.setattr(
        jobs.core_start,
        "start",
        lambda project, **kwargs: calls.append(kwargs["bench_path"]) or Result(status=Status.OK),
    )
    monkeypatch.setattr(
        jobs.core_resolvers, "resolve_bench", lambda *a: pytest.fail("asked which bench")
    )
    return calls


def test_instance_restart_brings_back_only_the_benches_that_were_serving(monkeypatch):
    calls = _wire_instance_restart(monkeypatch, "running", {"/workspace/b0", "/workspace/b2"})

    result = jobs._restart_instance({"project": "p"}, lambda event: None)

    assert [c for c in calls if not isinstance(c, tuple)] == [
        "stop",
        "/workspace/b0",
        "/workspace/b2",
    ]
    assert calls.index("stop") > max(i for i, c in enumerate(calls) if isinstance(c, tuple))
    assert result.data["restarted_benches"] == ["/workspace/b0", "/workspace/b2"]
    assert jobs._result_ok(result) is True


def test_instance_restart_brings_back_a_bench_serving_under_bench_start(monkeypatch):
    calls = _wire_instance_restart(monkeypatch, "running", set(), unsupervised={"/workspace/b1"})

    result = jobs._restart_instance({"project": "p"}, lambda event: None)

    assert [c for c in calls if not isinstance(c, tuple)] == ["stop", "/workspace/b1"]
    assert result.data["restarted_benches"] == ["/workspace/b1"]


def test_instance_restart_stops_nothing_when_the_process_table_is_unreadable(monkeypatch):
    calls = _wire_instance_restart(monkeypatch, "running", {"/workspace/b0"})
    monkeypatch.setattr(jobs.core_supervision, "discover_stack", _real_discover_stack)
    frappe = jobs.core_docker.get_frappe_container("p")
    frappe.exec_run = lambda cmd, **kwargs: (1, b"exec failed")

    with pytest.raises(CwcliError) as error:
        jobs._restart_instance({"project": "p"}, lambda event: None)

    assert error.value.code == "supervisor.process_state_unknown"
    assert calls == []


@pytest.mark.parametrize("frappe_status", ["running", "exited"])
def test_instance_restart_with_nothing_serving_starts_only_the_containers(
    monkeypatch, frappe_status
):
    calls = _wire_instance_restart(monkeypatch, frappe_status, set())

    result = jobs._restart_instance({"project": "p"}, lambda event: None)

    assert [c for c in calls if not isinstance(c, tuple)] == ["stop", "containers"]
    assert result.status is Status.OK
    assert result.data["restarted_benches"] == []


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


def test_init_answers_confirm_start_with_one_capped_auto_start_retry(fake_spawn, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "check_start_port_conflicts", lambda project: calls.append("ports"))
    monkeypatch.setattr(
        jobs.core_init,
        "init_instance",
        lambda project, **kwargs: calls.append(("instance", kwargs["auto_start"]))
        or Result(status=Status.OK),
    )
    monkeypatch.setattr(
        jobs.core_init,
        "init_bench",
        lambda project, **kwargs: calls.append(("bench", kwargs["auto_start"]))
        or Result(status=Status.NEEDS_CHOICE),
    )

    jobs._run_init({"project": "p"}, {"auto_start": True}, {"admin_password": "x"}, lambda e: None)

    assert calls == ["ports", ("instance", True), ("bench", True)]


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
    monkeypatch.setattr(jobs, "_safety_backups", lambda *a, **k: None)  # pinned on its own below
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


def _finished_job(job_id: str, created_at: str, state: str = "succeeded") -> None:
    directory = jobs.job_dir(job_id)
    spec = {"id": job_id, "verb": "backup", "target": {"project": "p"}, "args": {}}
    jobs._write_json(directory / "spec.json", {**spec, "created_at": created_at})
    jobs._write_json(
        directory / "state.json", {"id": job_id, "state": state, "result": {"ok": True}}
    )


def test_an_interrupt_request_file_unwinds_the_worker_through_its_cleanup(fake_spawn, monkeypatch):
    record = jobs.create_job("migrate", {"project": "p", "site": "s"}, {})
    cleaned = []

    def dispatch(*_args):
        jobs._json_path(record["id"], jobs._INTERRUPT_REQUEST).touch()
        try:
            for _ in range(500):
                time.sleep(0.01)
        finally:
            cleaned.append("maintenance cleared")
        pytest.fail("the interrupt request never reached the worker")

    monkeypatch.setattr(jobs, "_dispatch", dispatch)

    assert jobs.run_job(record["id"]) == 130
    assert cleaned == ["maintenance cleared"]
    assert jobs._json_path(record["id"], jobs._CLEANUP_STARTED).exists()
    assert jobs.read_job(record["id"])["state"] == "interrupted"


class _Clock:
    """A fake monotonic clock that only moves when the code under test sleeps."""

    def __init__(self, monkeypatch, on_tick=lambda now: None):
        self.now = 0.0
        self.on_tick = on_tick
        monkeypatch.setattr(jobs.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(jobs.time, "sleep", self.sleep)

    def sleep(self, seconds):
        self.now += seconds
        self.on_tick(self.now)


def test_the_cleanup_allowance_counts_from_cleanup_start_not_the_request(fake_spawn, monkeypatch):
    record = jobs.create_job("migrate", {"project": "p", "site": "s"}, {})
    marker = jobs._json_path(record["id"], jobs._CLEANUP_STARTED)
    clock = _Clock(monkeypatch, lambda now: now >= 200 and marker.touch())
    killed = []
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: killed.append(clock.now))

    jobs._terminate_after_grace(record["id"], jobs._state(record["id"]))

    assert killed == [200 + jobs.INTERRUPT_CLEANUP_S]


def test_a_worker_that_never_starts_cleanup_is_terminated_and_marked_unknown(
    fake_spawn, monkeypatch
):
    record = jobs.create_job("migrate", {"project": "p", "site": "s"}, {})
    clock = _Clock(monkeypatch)
    killed = []
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: killed.append(clock.now))

    jobs._terminate_after_grace(record["id"], jobs._state(record["id"]))

    assert killed == [jobs.INTERRUPT_DELIVERY_S]
    state = jobs._state(record["id"])
    assert state["state"] == "unknown"
    assert state["error"]["code"] == "job.cleanup_not_started"
    assert "cleanup may not have run" in state["error"]["message"]


def test_a_worker_that_finishes_cleanup_in_time_is_not_terminated(fake_spawn, monkeypatch):
    record = jobs.create_job("migrate", {"project": "p", "site": "s"}, {})
    jobs._json_path(record["id"], jobs._CLEANUP_STARTED).touch()
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(jobs, "_process_is_job", lambda state: clock.now < 30)
    monkeypatch.setattr(jobs.os, "kill", lambda *args: pytest.fail("cleanup was cut short"))

    jobs._terminate_after_grace(record["id"], jobs._state(record["id"]))


def test_a_second_interrupt_leaves_an_interrupting_job_alone(fake_spawn, monkeypatch):
    record = jobs.create_job("migrate", {"project": "p", "site": "s"}, {})
    signalled = []
    monkeypatch.setattr(jobs.os, "killpg", lambda pid, sig: signalled.append(pid))
    first = jobs.interrupt_job(record["id"])

    second = jobs.interrupt_job(record["id"])

    assert signalled == [4242]
    assert second["interrupt_requested_at"] == first["interrupt_requested_at"]
    assert second["state"] == "interrupting"


def test_creating_a_job_prunes_finished_history_but_keeps_active_jobs(fake_spawn):
    for n in range(jobs.FINISHED_RETENTION + 2):
        _finished_job(f"20260101{n:06d}-old", f"2026-01-01T00:00:{n:06d}Z")
    active = jobs.create_job("backup", {"project": "busy"}, {})

    jobs.create_job("backup", {"project": "other"}, {})

    ids = {row["id"] for row in jobs.list_jobs()}
    assert active["id"] in ids
    assert "20260101000000-old" not in ids and "20260101000001-old" not in ids
    assert "20260101000002-old" in ids


def test_the_list_omits_result_payloads_that_the_job_read_still_carries(fake_spawn):
    _finished_job("20260101000000-done", "2026-01-01T00:00:00Z")

    assert "result" not in jobs.list_jobs()[0]
    assert jobs.read_job("20260101000000-done")["result"] == {"ok": True}


def test_event_reads_after_a_cursor_return_only_newer_lines(fake_spawn):
    record = jobs.create_job("backup", {"project": "p"}, {})
    for seq in (1, 2, 3):
        jobs._append_event(record["id"], seq, {"type": "event", "message": str(seq)})

    events = jobs.read_job(record["id"], include_events=True, after=2)["events"]

    assert [event["seq"] for event in events] == [3]


def test_the_watcher_rereads_a_finished_job_only_when_its_state_changes(fake_spawn, monkeypatch):
    _finished_job("20260101000000-done", "2026-01-01T00:00:00Z")
    reads = []
    real_read = jobs.read_job
    monkeypatch.setattr(
        jobs, "read_job", lambda job_id, **kw: reads.append(job_id) or real_read(job_id, **kw)
    )
    published = []

    class Ticks:
        remaining = 3

        def wait(self, _interval):
            self.remaining -= 1
            return self.remaining < 0

    jobs.watch_loop(published.append, Ticks())

    assert reads == ["20260101000000-done"]
    assert [row["id"] for row in published] == ["20260101000000-done"]


# ------------------------------------------------- Phase 3: the guarded destructive verbs


def _outcome(**overrides):
    from caffeinated_whale_cli.core.rm import RemovalOutcome

    base = dict(
        project="p",
        found=True,
        orphan=False,
        containers_removed=3,
        volumes_removed=1,
        dir_removed=True,
        network_removed=True,
        backup_ok=True,
    )
    return RemovalOutcome(**{**base, **overrides})


def test_the_destructive_verbs_are_admitted_as_jobs(fake_spawn):
    for verb in ("rm", "rm-site", "rm-bench"):
        assert verb in jobs.ALLOWED_VERBS
    # Consent is a spec field: the page sets it only after a typed confirmation.
    record = jobs.create_job("rm", {"project": "p"}, {}, consent=True)
    assert record["consent"] is True


def test_rm_without_consent_ends_as_a_choice_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(jobs.core_rm, "remove", lambda *a, **k: pytest.fail("removed"))
    monkeypatch.setattr(
        jobs.rm_cmd, "_project_run_state", lambda project: pytest.fail("probed before consent")
    )

    result = jobs._dispatch("rm", {"project": "p"}, {}, False, {}, lambda e: None)

    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_remove"
    assert result.choice.param == "consent"
    assert "named volumes" in result.choice.prompt


def test_rm_refuses_a_stopped_project_before_the_core_when_volumes_would_go(monkeypatch):
    """`axi rm`'s rule: the verified backup needs a running container and this
    surface never starts one, so the refusal lands BEFORE core.remove creates an
    archive it cannot fill. Keeping the volumes needs no backup, so it proceeds."""
    calls = []
    monkeypatch.setattr(jobs.rm_cmd, "_project_run_state", lambda project: "stopped")
    monkeypatch.setattr(
        jobs.core_rm,
        "remove",
        lambda project, **kwargs: calls.append(kwargs)
        or Result(status=Status.OK, data=_outcome(volumes_removed=0)),
    )

    with pytest.raises(CwcliError) as error:
        jobs._dispatch("rm", {"project": "p"}, {}, True, {}, lambda e: None)
    assert error.value.code == "rm.not_running"
    assert calls == []

    jobs._dispatch("rm", {"project": "p"}, {"keep_volumes": True}, True, {}, lambda e: None)
    assert calls[0]["remove_volumes"] is False
    assert calls[0]["no_backup"] is False


def test_rm_runs_the_core_with_consent_and_keeps_the_backup_unless_force_is_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs.rm_cmd, "_project_run_state", lambda project: "running")
    monkeypatch.setattr(
        jobs.core_rm,
        "remove",
        lambda project, **kwargs: calls.append(kwargs) or Result(status=Status.OK, data=_outcome()),
    )

    emit = lambda e: None  # noqa: E731 - identity matters below
    result = jobs._dispatch("rm", {"project": "p"}, {}, True, {}, emit)

    # The default keeps the verified backup gate.
    assert calls == [{"remove_volumes": True, "no_backup": False, "on_event": emit}]
    assert jobs._result_ok(result) is True
    # The page's Force checkbox (captain's rule 2026-10-27, behind a typed confirmation and
    # a red warning) is the one way past it, and only when sent explicitly.
    jobs._dispatch("rm", {"project": "p"}, {"no_backup": True}, True, {}, emit)
    assert calls[-1]["no_backup"] is True


def test_a_gate_blocked_removal_is_a_failed_job_naming_the_terminal_escape_hatch(monkeypatch):
    monkeypatch.setattr(jobs.rm_cmd, "_project_run_state", lambda project: "running")
    monkeypatch.setattr(
        jobs.core_rm,
        "remove",
        lambda project, **kwargs: Result(
            status=Status.WARNING,
            data=_outcome(backup_ok=False, dir_removed=False, failures=["backup of site x failed"]),
        ),
    )

    result = jobs._dispatch("rm", {"project": "p"}, {}, True, {}, lambda e: None)

    assert jobs._result_ok(result) is False
    assert [w.code for w in result.warnings] == ["rm.backup_gate"]
    assert "cwcli rm p --no-backup" in result.warnings[0].text


def test_a_not_found_project_removal_is_an_ok_no_op_with_a_warning(monkeypatch):
    monkeypatch.setattr(jobs.rm_cmd, "_project_run_state", lambda project: "orphan")
    monkeypatch.setattr(
        jobs.core_rm,
        "remove",
        lambda project, **kwargs: Result(
            status=Status.WARNING,
            data=_outcome(found=False, containers_removed=0, volumes_removed=0, dir_removed=False),
        ),
    )

    result = jobs._dispatch("rm", {"project": "p"}, {}, True, {}, lambda e: None)

    assert jobs._result_ok(result) is True
    assert [w.code for w in result.warnings] == ["rm.not_found"]


def test_rm_site_forwards_consent_and_the_root_password_from_the_secret_only(monkeypatch):
    calls = []
    monkeypatch.setattr(
        jobs.core_rm_site,
        "drop_site",
        lambda project, site, **kwargs: calls.append((project, site, kwargs))
        or Result(status=Status.OK),
    )

    jobs._dispatch(
        "rm-site",
        {"project": "p", "site": "s.localhost", "bench": "1"},
        {"db_root_password": "in-args-is-ignored"},
        True,
        {"db_root_password": "s3cret"},
        lambda e: None,
    )
    jobs._dispatch(
        "rm-site", {"project": "p", "site": "t.localhost"}, {}, False, {}, lambda e: None
    )

    (project, site, kwargs), (_p2, _s2, second) = calls
    assert (project, site) == ("p", "s.localhost")
    assert kwargs["bench"] == "1"
    assert kwargs["consent"] is True
    assert kwargs["db_root_password"] == "s3cret"
    assert second["consent"] is False
    # Blank: None, so the core uses the instance's remembered password, else '123'.
    assert second["db_root_password"] is None

    with pytest.raises(CwcliError) as error:
        jobs._dispatch("rm-site", {"project": "p"}, {}, True, {}, lambda e: None)
    assert error.value.code == "site.required"


def test_rm_bench_forwards_the_selector_consent_and_secret(monkeypatch):
    calls = _wire_rm_bench(monkeypatch, [_Container("frappe", "running")])

    emit = lambda e: None  # noqa: E731 - identity matters below
    jobs._dispatch(
        "rm-bench", {"project": "p", "bench": "lab"}, {}, True, {"db_root_password": "x"}, emit
    )

    assert calls["remove"] == [
        {
            "bench": "lab",
            "auto_start": True,
            "consent": True,
            "db_root_password": "x",
            "on_event": emit,
        }
    ]


def test_the_db_root_password_rides_the_environment_never_the_spec(fake_spawn):
    record = jobs.create_job(
        "rm-site",
        {"project": "p", "site": "s.localhost"},
        {},
        consent=True,
        secret_values={"db_root_password": "s3cret"},
    )

    spec = json.loads((jobs.job_dir(record["id"]) / "spec.json").read_text())
    assert "s3cret" not in json.dumps(spec)
    _argv, kwargs = fake_spawn[0]
    assert kwargs["env"]["CWCLI_JOB_DB_ROOT_PASSWORD"] == "s3cret"


def test_apps_checkout_forwards_reset_only_as_the_json_boolean_true(monkeypatch):
    calls = []
    monkeypatch.setattr(
        jobs.core_apps,
        "checkout_app",
        lambda project, app, ref, **kwargs: calls.append(kwargs["reset"])
        or Result(status=Status.OK, data=None),
    )
    monkeypatch.setattr(jobs, "_refresh_after_apps", lambda project, result, emit: result)

    for reset in (True, "yes", 1, None):
        jobs._dispatch(
            "apps-checkout",
            {"project": "p", "app": "erpnext"},
            {"ref": "develop", "reset": reset},
            False,
            {},
            lambda e: None,
        )

    assert calls == [True, False, False, False]


def test_a_failed_job_records_its_plain_cause(fake_spawn, monkeypatch):
    record = jobs.create_job("rm-site", {"project": "p", "site": "s.localhost"}, {})
    denied = "(1045, \"Access denied for user 'root'@'x'\")"

    def dispatch(_verb, _target, _args, _consent, _secrets, emit):
        raise CwcliError(
            jobs.ErrorKind.PRECONDITION,
            "rm_site.failed",
            "drop-site exited 1",
            detail={"output": denied},
        )

    monkeypatch.setattr(jobs, "_dispatch", dispatch)

    assert jobs.run_job(record["id"]) == 1
    failed = jobs.read_job(record["id"])
    assert failed["cause"]["code"] == "db.access_denied"
    assert failed["cause"]["title"] == "MariaDB rejected the root password"


@pytest.mark.parametrize(
    "stderr",
    [
        b"error getting credentials - err: exit status 1, out: ``",
        b'error getting credentials - err: exec: "docker-credential-x": executable file not found',
        b"error getting credentials - err: exit status 1, out: `The keychain is locked`",
    ],
)
def test_a_credential_helper_failure_states_its_cause_once(fake_spawn, monkeypatch, stderr):
    # The job page prints cause.text before the error message unless the message
    # already contains it, so a reworded cause would be read twice.
    import subprocess
    from types import SimpleNamespace

    from caffeinated_whale_cli.core import init as core_init

    record = jobs.create_job("init", {"project": "p"}, {})

    def failing_run(cmd, cwd=None, capture_output=False, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=stderr)

    def dispatch(_verb, _target, _args, _consent, _secrets, emit):
        core_init._run_host_command(
            ["docker", "compose", "pull"],
            cwd=".",
            phase="pull",
            emit=lambda _e: None,
            retry_credential_errors=True,
        )

    monkeypatch.setattr(subprocess, "run", failing_run)
    monkeypatch.setattr(core_init.time, "sleep", lambda _s: None)
    monkeypatch.setattr(jobs, "_dispatch", dispatch)

    assert jobs.run_job(record["id"]) == 1
    failed = jobs.read_job(record["id"])
    assert failed["cause"]["code"].startswith("docker.credential_helper")
    assert failed["cause"]["text"] in failed["error"]["message"]


@dataclass(frozen=True)
class _StepOutput:
    stream: str
    text: str


_BENIGN_EARLIER_OUTPUT = (
    "Error 111 connecting to redis-cache:6379. Retrying...\n"
    "Temporary failure in name resolution, retrying pip download\n"
)


def test_an_earlier_steps_benign_output_never_becomes_a_failed_jobs_cause(fake_spawn, monkeypatch):
    record = jobs.create_job("init", {"project": "p"}, {})

    def dispatch(_verb, _target, _args, _consent, _secrets, emit):
        emit(_StepOutput(stream="stderr", text=_BENIGN_EARLIER_OUTPUT))
        raise CwcliError(
            jobs.ErrorKind.PRECONDITION,
            "init.exec_failed",
            "Command failed with exit code 1: bench migrate",
            detail={"output": "KeyError: 'patch_field'"},
        )

    monkeypatch.setattr(jobs, "_dispatch", dispatch)

    assert jobs.run_job(record["id"]) == 1
    assert jobs.read_job(record["id"])["cause"] is None


def test_a_failed_report_records_no_cause_from_earlier_output(fake_spawn, monkeypatch):
    def dispatch_with(ok):
        def dispatch(_verb, _target, _args, _consent, _secrets, emit):
            emit(_StepOutput(stream="stderr", text=_BENIGN_EARLIER_OUTPUT))
            return Result(status=Status.WARNING, data={"ok": ok})

        return dispatch

    failed = jobs.create_job("backup", {"project": "p"}, {})
    monkeypatch.setattr(jobs, "_dispatch", dispatch_with(False))
    assert jobs.run_job(failed["id"]) == 1
    assert jobs.read_job(failed["id"])["cause"] is None

    succeeded = jobs.create_job("backup", {"project": "q"}, {})
    monkeypatch.setattr(jobs, "_dispatch", dispatch_with(True))
    assert jobs.run_job(succeeded["id"]) == 0
    assert jobs.read_job(succeeded["id"])["cause"] is None


def test_a_core_update_takes_the_safety_backup_first_and_a_failed_one_stops_it(
    fake_spawn, monkeypatch
):
    """Captain decision 5 (2026-10-27): the ONE automatic backup is the one before a core
    update, taken by cwcli's store before `core.update` runs; a set that does not verify
    stops the update before anything moves. A non-core update takes none."""
    order = []
    monkeypatch.setattr(
        jobs,
        "_bench_sites_from_cache",
        lambda project, bench: ("/workspace/frappe-bench", ["a.localhost", "b.localhost"]),
    )

    class _Set:
        def __init__(self, site, ok):
            self.key, self.verified, self.checks, self.site = f"p/{site}/x", ok, [], site

    def capture(project, *, site, bench, with_files, reason, on_event):
        order.append(("capture", site, reason))
        return Result(status=Status.OK, data=_Set(site, ok=True))

    def update(project, apps, **kwargs):
        order.append(("update", tuple(apps)))
        return Result(status=Status.OK, data={"ok": True})

    monkeypatch.setattr(jobs.core_backup_store, "capture", capture)
    monkeypatch.setattr(jobs.core_update, "update", update)
    record = jobs.create_job("apps-update", {"project": "p1", "bench": "0"}, {"apps": ["frappe"]})
    jobs._dispatch(
        "apps-update", record["target"], record["args"], True, {}, jobs._Recorder(record["id"], [])
    )
    assert order == [
        ("capture", "a.localhost", "before-core-update"),
        ("capture", "b.localhost", "before-core-update"),
        ("update", ("frappe",)),
    ]

    order.clear()
    record = jobs.create_job("apps-update", {"project": "p2", "bench": "0"}, {"apps": ["hrms"]})
    jobs._dispatch(
        "apps-update", record["target"], record["args"], True, {}, jobs._Recorder(record["id"], [])
    )
    assert order == [("update", ("hrms",))]

    order.clear()
    monkeypatch.setattr(
        jobs.core_backup_store,
        "capture",
        lambda project, **k: Result(status=Status.WARNING, data=_Set(k["site"], ok=False)),
    )
    record = jobs.create_job("apps-update", {"project": "p3", "bench": "0"}, {"apps": ["erpnext"]})
    with pytest.raises(CwcliError) as exc:
        jobs._dispatch(
            "apps-update",
            record["target"],
            record["args"],
            True,
            {},
            jobs._Recorder(record["id"], []),
        )
    assert exc.value.code == "backups.safety_failed"
    assert order == []  # the update never ran


def test_a_self_update_worker_with_the_daemon_context_schedules_the_ui_restart(
    fake_spawn, monkeypatch
):
    jobs.create_job(
        "self-update",
        {},
        {},
        ui_context={"pid": 99, "start": "start-99", "port": 8765, "token": "daemon-token"},
    )
    for name, value in fake_spawn[0][1]["env"].items():
        if name.startswith("CWCLI_JOB_UI_"):
            monkeypatch.setenv(name, value)

    jobs._schedule_ui_restart()

    assert fake_spawn[-1][0][-1] == "restart-ui"


class _Container:
    def __init__(self, name, status):
        self.name, self.status, self.calls = name, status, []
        self.labels = {"com.docker.compose.service": name}

    def start(self):
        self.calls.append("start")

    def reload(self):
        self.calls.append("reload")
        self.status = "running"


class _Emit:
    def __init__(self):
        self.lifecycles = []

    def __call__(self, event):
        pass

    def lifecycle(self, kind, message, **data):
        self.lifecycles.append((kind, message))


def _wire_rm_bench(monkeypatch, containers, db_ready=(True, ""), removal=None):
    calls = {"preflight": [], "networks": [], "remove": [], "order": [], "stops": []}
    monkeypatch.setattr(
        jobs.core_stop,
        "stop",
        lambda project, **k: calls["stops"].append(project) or Result(status=Status.OK),
    )
    monkeypatch.setattr(
        jobs.rm_cmd,
        "_wait_for_db_ready",
        lambda container: calls["order"].append(("db_wait", container.name)) or db_ready,
    )
    monkeypatch.setattr(jobs, "check_start_port_conflicts", calls["preflight"].append)
    monkeypatch.setattr(jobs.core_start, "get_project_containers", lambda project: containers)
    monkeypatch.setattr(
        jobs.core_start,
        "recreate_missing_project_networks",
        lambda project, stopped: calls["networks"].append([c.name for c in stopped]) or [],
    )
    monkeypatch.setattr(jobs.core_start, "start", lambda *a, **k: pytest.fail("started a bench"))
    monkeypatch.setattr(
        jobs.core_rm_bench,
        "remove_bench",
        lambda project, **kwargs: calls["remove"].append(kwargs)
        or calls["order"].append("remove")
        or (removal() if removal else Result(status=Status.OK)),
    )
    return calls


def test_rm_bench_on_a_stopped_instance_starts_only_its_containers(monkeypatch):
    """Starting the bench would make remove_bench refuse it as running."""
    containers = [_Container("frappe", "exited"), _Container("db", "exited")]
    calls = _wire_rm_bench(monkeypatch, containers)

    jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, lambda e: None)

    assert calls["preflight"] == ["p"]
    assert calls["networks"] == [["frappe", "db"]]
    assert [c.calls for c in containers] == [["start", "reload"], ["start", "reload"]]
    # A cold start returns before MariaDB serves; the backup waits for it.
    assert calls["order"] == [("db_wait", "frappe"), "remove"]
    [kwargs] = calls["remove"]
    assert kwargs["bench"] == "1"
    assert kwargs["auto_start"] is True
    assert kwargs["consent"] is True


def test_rm_bench_on_a_running_instance_starts_nothing(monkeypatch):
    containers = [_Container("frappe", "running"), _Container("db", "running")]
    calls = _wire_rm_bench(monkeypatch, containers)

    jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, lambda e: None)

    assert calls["networks"] == []
    assert [c.calls for c in containers] == [[], []]
    assert calls["order"] == ["remove"]
    assert calls["stops"] == []


def test_rm_bench_removes_nothing_when_the_cold_started_database_never_serves(monkeypatch):
    containers = [_Container("frappe", "exited"), _Container("db", "exited")]
    calls = _wire_rm_bench(monkeypatch, containers, db_ready=(False, "Connection refused"))

    with pytest.raises(CwcliError) as error:
        jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())

    assert error.value.code == "start.db_not_ready"
    assert "Connection refused" in error.value.message
    assert calls["remove"] == []
    assert calls["stops"] == ["p"]


def _refused():
    raise CwcliError(jobs.ErrorKind.NOT_FOUND, "bench.not_found", "no such bench")


@pytest.mark.parametrize(
    "removal",
    [
        _refused,
        lambda: Result(status=Status.NEEDS_CHOICE),
        lambda: Result(status=Status.WARNING, data={"ok": False}),
    ],
)
def test_an_incomplete_removal_stops_the_containers_it_started(monkeypatch, removal):
    containers = [_Container("frappe", "exited"), _Container("db", "exited")]
    calls = _wire_rm_bench(monkeypatch, containers, removal=removal)

    try:
        jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())
    except CwcliError:
        pass

    assert calls["stops"] == ["p"]


@pytest.mark.parametrize(
    "removal", [_refused, lambda: Result(status=Status.WARNING, data={"ok": False})]
)
def test_an_incomplete_removal_never_stops_an_instance_that_was_running(monkeypatch, removal):
    containers = [_Container("frappe", "running"), _Container("db", "running")]
    calls = _wire_rm_bench(monkeypatch, containers, removal=removal)

    try:
        jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())
    except CwcliError:
        pass

    assert calls["stops"] == []


def test_a_container_start_that_fails_partway_stops_what_it_started(monkeypatch):
    class _Broken(_Container):
        def start(self):
            raise RuntimeError("docker refused")

    containers = [_Container("frappe", "exited"), _Broken("db", "exited")]
    calls = _wire_rm_bench(monkeypatch, containers)

    with pytest.raises(RuntimeError):
        jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())

    assert containers[0].calls == ["start", "reload"]
    assert calls["remove"] == []
    assert calls["stops"] == ["p"]


def test_a_completed_removal_leaves_the_started_containers_alone(monkeypatch):
    containers = [_Container("frappe", "exited"), _Container("db", "exited")]
    calls = _wire_rm_bench(monkeypatch, containers)

    jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())

    assert calls["stops"] == []


def test_a_failed_stop_again_is_a_warning_never_an_error(monkeypatch):
    containers = [_Container("frappe", "exited"), _Container("db", "exited")]
    _wire_rm_bench(
        monkeypatch, containers, removal=lambda: Result(status=Status.WARNING, data={"ok": False})
    )

    def boom(project, **k):
        raise RuntimeError("daemon gone")

    monkeypatch.setattr(jobs.core_stop, "stop", boom)
    result = jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())

    assert [w.code for w in result.warnings] == ["rm_bench.stop_failed"]


def test_a_containers_only_start_never_starts_a_bench(monkeypatch):
    """The page's start gate, with no bench to name, never asks which bench."""
    containers = [_Container("frappe", "exited"), _Container("db", "exited")]
    calls = _wire_rm_bench(monkeypatch, containers)

    result = jobs._dispatch(
        "start", {"project": "p"}, {"containers_only": True}, False, {}, _Emit()
    )

    assert calls["preflight"] == ["p"]
    assert [c.calls for c in containers] == [["start", "reload"], ["start", "reload"]]
    assert calls["order"] == [("db_wait", "frappe")]
    assert result.status is Status.OK
    assert jobs._result_ok(result) is True


@pytest.mark.parametrize(
    ("containers", "code"), [(None, "docker.unreachable"), ([], "project.not_found")]
)
@pytest.mark.parametrize(("verb", "args"), [("start", {"containers_only": True}), ("rm-bench", {})])
def test_a_containers_only_start_fails_without_docker_or_the_project(
    monkeypatch, containers, code, verb, args
):
    calls = _wire_rm_bench(monkeypatch, containers)

    with pytest.raises(CwcliError) as error:
        jobs._dispatch(verb, {"project": "p", "bench": "1"}, args, True, {}, _Emit())

    assert error.value.code == code
    assert calls["preflight"] == []
    assert calls["remove"] == []


def test_a_removal_never_stops_an_instance_whose_frappe_container_was_running(monkeypatch):
    containers = [_Container("frappe", "running"), _Container("db", "exited")]
    calls = _wire_rm_bench(
        monkeypatch, containers, removal=lambda: Result(status=Status.WARNING, data={"ok": False})
    )

    jobs._dispatch("rm-bench", {"project": "p", "bench": "1"}, {}, True, {}, _Emit())

    assert containers[1].calls == ["start", "reload"]
    assert calls["stops"] == []


def test_a_containers_only_start_reports_whether_the_frappe_container_was_stopped(monkeypatch):
    containers = [_Container("frappe", "running"), _Container("db", "exited")]
    _wire_rm_bench(monkeypatch, containers)

    result = jobs._dispatch(
        "start", {"project": "p"}, {"containers_only": True}, False, {}, _Emit()
    )

    assert result.data == {"containers_started": False}
