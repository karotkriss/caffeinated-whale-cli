"""Tests for ``cwcli rm``'s start -> back up -> delete flow for a STOPPED project.

A live ``bench backup`` needs a running frappe + mariadb, so a stopped project on
the data-destroying path (``--volumes`` without ``--no-backup``) used to be deleted
with NO backup. It is now transiently STARTED, backed up, and only then deleted; if
it cannot be started or the backup fails, the removal aborts, ALL data is kept, and
the project is returned to its stopped state (a non-zero exit).

These tests pin:
- the run-state classifier (`_project_run_state`),
- the MariaDB-readiness wait (`_wait_for_db_ready`),
- the transient start (`_transient_start_for_backup`) incl. its failure returns,
- the return-to-stopped helper (`_stop_after_transient_start`),
- and the CLI orchestration in `rm()` (start-then-delete, abort-and-stop-back).
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import rm
from caffeinated_whale_cli.commands import start as start_mod
from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core import stop as core_stop
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


def _frappe(status="running"):
    c = MagicMock()
    c.status = status
    c.labels = {"com.docker.compose.service": "frappe"}
    return c


def _db(status="running"):
    c = MagicMock()
    c.status = status
    c.labels = {"com.docker.compose.service": "mariadb"}
    return c


# --------------------------------------------------------------------------- #
# _project_run_state
# --------------------------------------------------------------------------- #


class TestProjectRunState:
    def test_running(self, monkeypatch):
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [_frappe("running"), _db()])
        assert rm._project_run_state("p") == "running"

    def test_stopped(self, monkeypatch):
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [_frappe("exited"), _db()])
        assert rm._project_run_state("p") == "stopped"

    def test_orphan(self, monkeypatch):
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [])
        assert rm._project_run_state("p") == "orphan"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(rm, "get_project_containers", lambda n: None)
        assert rm._project_run_state("p") == "error"

    def test_no_frappe_service_is_stopped(self, monkeypatch):
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [_db()])
        assert rm._project_run_state("p") == "stopped"


# --------------------------------------------------------------------------- #
# _wait_for_db_ready
# --------------------------------------------------------------------------- #


class TestWaitForDbReady:
    def test_ready_immediately(self):
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        assert rm._wait_for_db_ready(container, attempts=5, delay=0) is True
        assert container.exec_run.call_count == 1

    def test_becomes_ready_after_retries(self, monkeypatch):
        monkeypatch.setattr(rm.time, "sleep", lambda *_a: None)
        container = MagicMock()
        container.exec_run.side_effect = [(1, b""), (1, b""), (0, b"")]
        assert rm._wait_for_db_ready(container, attempts=5, delay=0) is True
        assert container.exec_run.call_count == 3

    def test_never_ready_fails_closed(self, monkeypatch):
        monkeypatch.setattr(rm.time, "sleep", lambda *_a: None)
        container = MagicMock()
        container.exec_run.return_value = (1, b"")
        assert rm._wait_for_db_ready(container, attempts=4, delay=0) is False
        assert container.exec_run.call_count == 4

    def test_exec_exception_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(rm.time, "sleep", lambda *_a: None)
        container = MagicMock()
        container.exec_run.side_effect = RuntimeError("boom")
        assert rm._wait_for_db_ready(container, attempts=2, delay=0) is False


# --------------------------------------------------------------------------- #
# _transient_start_for_backup
# --------------------------------------------------------------------------- #


class TestTransientStart:
    def _no_port_conflict(self, monkeypatch):
        monkeypatch.setattr(start_mod, "_check_port_conflicts", lambda *a, **k: True)

    def test_starts_stopped_containers_and_waits_for_db(self, monkeypatch):
        self._no_port_conflict(monkeypatch)
        frappe, db = _frappe("exited"), _db("exited")
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [frappe, db])
        monkeypatch.setattr(rm, "_wait_for_db_ready", lambda *a, **k: True)

        ok, started = rm._transient_start_for_backup("p")

        assert (ok, started) == (True, True)
        frappe.start.assert_called_once()
        db.start.assert_called_once()

    def test_already_running_container_not_restarted(self, monkeypatch):
        self._no_port_conflict(monkeypatch)
        frappe, db = _frappe("running"), _db("exited")
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [frappe, db])
        monkeypatch.setattr(rm, "_wait_for_db_ready", lambda *a, **k: True)

        ok, started = rm._transient_start_for_backup("p")

        assert ok is True
        frappe.start.assert_not_called()  # already running
        db.start.assert_called_once()

    def test_db_never_ready_returns_not_ok(self, monkeypatch, capsys):
        self._no_port_conflict(monkeypatch)
        frappe = _frappe("exited")
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [frappe])
        monkeypatch.setattr(rm, "_wait_for_db_ready", lambda *a, **k: False)

        ok, started = rm._transient_start_for_backup("p")

        assert (ok, started) == (False, True)
        assert "did not become ready" in capsys.readouterr().err.lower()

    def test_no_frappe_container_returns_not_ok(self, monkeypatch):
        self._no_port_conflict(monkeypatch)
        db = _db("exited")
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [db])
        monkeypatch.setattr(rm, "_wait_for_db_ready", lambda *a, **k: True)

        ok, started = rm._transient_start_for_backup("p")

        assert ok is False
        assert started is True

    def test_no_containers_returns_not_ok(self, monkeypatch):
        self._no_port_conflict(monkeypatch)
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [])
        ok, started = rm._transient_start_for_backup("p")
        assert (ok, started) == (False, False)

    def test_container_start_error_returns_not_ok(self, monkeypatch, capsys):
        self._no_port_conflict(monkeypatch)
        frappe = _frappe("exited")
        frappe.start.side_effect = RuntimeError("image gone")
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [frappe])

        ok, started = rm._transient_start_for_backup("p")

        assert ok is False
        assert "could not start" in capsys.readouterr().err.lower()

    def test_port_conflict_propagates(self, monkeypatch):
        def _boom(*a, **k):
            raise typer.Exit(code=1)

        monkeypatch.setattr(start_mod, "_check_port_conflicts", _boom)
        monkeypatch.setattr(rm, "get_project_containers", lambda n: [_frappe("exited")])
        with pytest.raises(typer.Exit):
            rm._transient_start_for_backup("p")


# --------------------------------------------------------------------------- #
# _stop_after_transient_start
# --------------------------------------------------------------------------- #


class TestStopAfterTransientStart:
    def test_calls_core_stop(self, monkeypatch):
        called = MagicMock()
        monkeypatch.setattr(core_stop, "stop", called)
        rm._stop_after_transient_start("p")
        called.assert_called_once()

    def test_stop_failure_is_a_warning_not_a_crash(self, monkeypatch, capsys):
        def _boom(*a, **k):
            raise RuntimeError("nope")

        monkeypatch.setattr(core_stop, "stop", _boom)
        rm._stop_after_transient_start("p")  # must not raise
        assert "could not stop" in capsys.readouterr().err.lower()

    def test_typed_not_found_is_a_warning_not_a_crash(self, monkeypatch, capsys):
        """`core.stop`'s NOT_FOUND replaced `_stop_project`'s None sentinel. This
        path is best-effort by design (the data is already kept by the time it
        runs), so the typed error must degrade to the same warning, never escape
        as a traceback."""

        def _not_found(*a, **k):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'p' not found.")

        monkeypatch.setattr(core_stop, "stop", _not_found)
        rm._stop_after_transient_start("p")  # must not raise
        assert "could not stop" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# rm() CLI orchestration
# --------------------------------------------------------------------------- #


def _clean_result():
    # core.remove now returns a typed Result[RemovalOutcome]; the orchestration
    # tests patch rm.core_rm.remove to return these.
    return Result(
        status=Status.OK,
        data=core_rm.RemovalOutcome(
            project="proj",
            found=True,
            orphan=False,
            containers_removed=1,
            volumes_removed=1,
            dir_removed=True,
            backup_ok=True,
            failures=[],
        ),
    )


def _failed_result():
    return Result(
        status=Status.WARNING,
        data=core_rm.RemovalOutcome(
            project="proj",
            found=True,
            orphan=False,
            containers_removed=0,
            volumes_removed=0,
            dir_removed=False,
            backup_ok=False,
            failures=["a verified database backup could not be created for 'proj'"],
        ),
    )


class TestRmOrchestration:
    def _base(self, monkeypatch):
        # No recache, non-piped stdin, and a stable "stopped" classification.
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(rm, "_frappe_container_running", lambda n: False)
        monkeypatch.setattr(rm.cache, "recache_project", MagicMock())

    def _run(self, **over):
        params = dict(
            ctx=MagicMock(),
            verbose=False,
            volumes=True,
            no_backup=False,
            yes=True,
            project_name=["proj"],
        )
        params.update(over)
        return rm.rm(**params)

    def test_stopped_project_started_then_removed(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        start = MagicMock(return_value=(True, True))
        remove = MagicMock(return_value=_clean_result())
        stop_back = MagicMock()
        monkeypatch.setattr(rm, "_transient_start_for_backup", start)
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_stop_after_transient_start", stop_back)

        self._run()  # no exception -> success

        start.assert_called_once()
        remove.assert_called_once()
        stop_back.assert_not_called()  # a clean removal deletes the containers

    def test_not_found_during_return_to_stopped_still_fails_closed(self, monkeypatch):
        """The `None` sentinel -> `CwcliError(NOT_FOUND)` re-point must not weaken the gate.

        `rm` is where a wrong assumption costs real data, so this proves the
        property rather than reasoning about it: when the transient start fails AND
        the return-to-stopped then hits a typed NOT_FOUND (the project vanished
        under us), `rm` must STILL delete nothing and STILL exit non-zero. The typed
        error must not escape, and must not be mistaken for a successful gate.
        """
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        monkeypatch.setattr(rm, "_transient_start_for_backup", lambda *a, **k: (False, True))
        remove = MagicMock(return_value=_clean_result())
        monkeypatch.setattr(rm.core_rm, "remove", remove)

        def _not_found(*a, **k):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'proj' not found.")

        monkeypatch.setattr(core_stop, "stop", _not_found)

        with pytest.raises(typer.Exit) as exc:
            self._run()

        assert exc.value.exit_code == 1  # honest non-zero
        remove.assert_not_called()  # NOTHING deleted: the gate still fails closed

    def test_failed_start_aborts_and_stops_back(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        start = MagicMock(return_value=(False, True))  # started but not ok
        remove = MagicMock(return_value=_clean_result())
        stop_back = MagicMock()
        monkeypatch.setattr(rm, "_transient_start_for_backup", start)
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_stop_after_transient_start", stop_back)

        with pytest.raises(typer.Exit) as exc:
            self._run()

        assert exc.value.exit_code == 1
        remove.assert_not_called()  # nothing deleted
        stop_back.assert_called_once()  # returned to stopped state

    def test_aborted_removal_stops_back_after_start(self, monkeypatch):
        """Start succeeded but the backup gate aborted removal -> stop it back."""
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        start = MagicMock(return_value=(True, True))
        remove = MagicMock(return_value=_failed_result())
        stop_back = MagicMock()
        monkeypatch.setattr(rm, "_transient_start_for_backup", start)
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_stop_after_transient_start", stop_back)

        with pytest.raises(typer.Exit) as exc:
            self._run()

        assert exc.value.exit_code == 1
        remove.assert_called_once()
        stop_back.assert_called_once()

    def test_running_project_not_started(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "running")
        start = MagicMock()
        remove = MagicMock(return_value=_clean_result())
        monkeypatch.setattr(rm, "_transient_start_for_backup", start)
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_stop_after_transient_start", MagicMock())

        self._run()

        start.assert_not_called()  # already running - no transient start
        remove.assert_called_once()

    def test_no_backup_skips_transient_start(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        start = MagicMock()
        remove = MagicMock(return_value=_clean_result())
        monkeypatch.setattr(rm, "_transient_start_for_backup", start)
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_stop_after_transient_start", MagicMock())

        self._run(no_backup=True)

        start.assert_not_called()
        remove.assert_called_once()

    def test_no_volumes_skips_transient_start(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        start = MagicMock()
        remove = MagicMock(return_value=_clean_result())
        monkeypatch.setattr(rm, "_transient_start_for_backup", start)
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_stop_after_transient_start", MagicMock())

        self._run(volumes=False)

        start.assert_not_called()
        remove.assert_called_once()

    def test_pre_confirm_discloses_stopped_start(self, monkeypatch, capsys):
        """Interactive: the confirm prompt discloses the stopped -> start-to-backup
        plan BEFORE the confirm, and declining deletes nothing."""
        monkeypatch.setattr(rm.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(rm, "_frappe_container_running", lambda n: False)
        monkeypatch.setattr(rm.cache, "recache_project", MagicMock())
        monkeypatch.setattr(rm, "_project_run_state", lambda n: "stopped")
        remove = MagicMock(return_value=_clean_result())
        monkeypatch.setattr(rm.core_rm, "remove", remove)
        monkeypatch.setattr(rm, "_transient_start_for_backup", MagicMock())

        # Decline the confirm.
        fake_confirm = MagicMock()
        fake_confirm.ask.return_value = False
        monkeypatch.setattr(rm.questionary, "confirm", MagicMock(return_value=fake_confirm))

        with pytest.raises(typer.Exit) as exc:
            self._run(yes=False)

        assert exc.value.exit_code == 0  # cancelled
        out = capsys.readouterr().out.lower()
        assert "started to take a backup" in out
        remove.assert_not_called()
