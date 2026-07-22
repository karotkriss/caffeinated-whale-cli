"""``cwcli axi rm`` - the agent surface for cwcli's only destructive verb.

The verb was DEFERRED (``migrate-rm-core`` design Decision 5) and shipped on the
captain's 2026-07-21 approval. That approval overturned WHETHER an agent may hold
the capability; it waived no safety property, so what this file is really about is
the two properties the human frontend owns and this surface therefore had to
DECIDE rather than inherit:

- ``--yes`` grants consent ONLY, never the auto-start the human verb's ``--yes``
  also grants. That fusion is the one ``core.remove`` was migrated to keep out of
  the core, and re-creating it here would mean an agent that asked to delete an
  instance had thereby started one.
- There is no ``--no-backup``. It is the C1 gate's off switch and has no named
  beneficiary on this surface (the ``axi apps install`` ``--force`` and
  ``axi migrate`` ``--skip-maintenance`` rulings).

Everything else - the early fail-closed gate, the verified per-bench copy-out, the
failures-driven exit code - is ``core.remove`` unchanged, covered by
``test_rm_safety.py`` / ``test_rm_truth.py`` / ``test_rm_stopped.py``.

The remaining thing this file guards is that every refusal SAYS WHAT TO DO. A
destructive verb that answers only "no" pushes its caller to the raw human command,
which is the path this verb exists to replace.
"""

from __future__ import annotations

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import rm as rm_mod
from caffeinated_whale_cli.core import rm as core_rm
from caffeinated_whale_cli.core.envelope import Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


def _outcome(**kwargs):
    defaults = dict(
        project="proj",
        found=True,
        orphan=False,
        containers_removed=4,
        volumes_removed=2,
        dir_removed=True,
        network_removed=True,
        backup_ok=True,
        failures=[],
    )
    defaults.update(kwargs)
    return core_rm.RemovalOutcome(**defaults)


@pytest.fixture()
def running(monkeypatch):
    monkeypatch.setattr(rm_mod, "_project_run_state", lambda _p: "running")


def _rm(**kwargs):
    params = {"yes": True, "volumes": True}
    params.update(kwargs)
    return axi_mod.axi_rm("proj", **params)


def _stub_remove(monkeypatch, result, seen=None):
    def fake(project, *, remove_volumes, no_backup, on_event):
        if seen is not None:
            seen.update(project=project, remove_volumes=remove_volumes, no_backup=no_backup)
        return result

    monkeypatch.setattr(core_rm, "remove", fake)


# ------------------------------------------------------------------------- consent


def test_without_yes_nothing_is_removed_and_the_refusal_names_the_flag(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(core_rm, "remove", lambda *a, **k: called.append(1))

    with pytest.raises(typer.Exit) as exc:
        _rm(yes=False)

    assert exc.value.exit_code == 2  # USAGE
    assert not called  # the core was never reached
    out = capsys.readouterr().out
    assert "error:" in out
    assert "--yes" in out  # the way out is named, not merely refused


def test_yes_grants_consent_and_never_starts_anything(monkeypatch, running, capsys):
    """The human verb's --yes ALSO auto-starts a stopped project; this one does not.

    Asserted at the call boundary: nothing on this path may reach a container
    start, so an agent that asked to delete an instance can never have started one.
    """
    import inspect

    seen: dict = {}
    _stub_remove(monkeypatch, Result(status=Status.OK, data=_outcome()), seen)

    with pytest.raises(typer.Exit) as exc:
        _rm()

    assert exc.value.exit_code == 0
    source = inspect.getsource(axi_mod.axi_rm)
    assert "auto_start" not in source
    assert "ensure_containers_running" not in source
    assert "core_start" not in source


# --------------------------------------------------------- the backup gate has no hatch


def test_the_core_is_always_called_with_the_backup_gate_on(monkeypatch, running):
    seen: dict = {}
    _stub_remove(monkeypatch, Result(status=Status.OK, data=_outcome()), seen)

    with pytest.raises(typer.Exit):
        _rm()

    assert seen["no_backup"] is False


def test_a_stopped_project_is_refused_before_the_core_is_reached(monkeypatch, capsys):
    """No auto-start means a live `bench backup` is impossible while stopped, and
    deleting anyway is exactly what the gate exists to prevent. Refuse EARLY, and
    name all three ways out rather than dead-ending the caller."""
    monkeypatch.setattr(rm_mod, "_project_run_state", lambda _p: "stopped")
    called = []
    monkeypatch.setattr(core_rm, "remove", lambda *a, **k: called.append(1))

    with pytest.raises(typer.Exit) as exc:
        _rm()

    assert exc.value.exit_code == 1  # NOT_RUNNING, an operational error
    assert not called
    out = capsys.readouterr().out
    assert "cwcli start proj" in out
    assert "--no-volumes" in out
    assert "--no-backup" in out  # where the escape hatch actually lives


def test_a_stopped_project_is_removable_when_no_data_is_destroyed(monkeypatch):
    """--no-volumes destroys nothing data-bearing, so the backup gate does not
    apply and the not-running refusal must not fire either."""
    monkeypatch.setattr(rm_mod, "_project_run_state", lambda _p: "stopped")
    seen: dict = {}
    _stub_remove(monkeypatch, Result(status=Status.OK, data=_outcome(volumes_removed=0)), seen)

    with pytest.raises(typer.Exit) as exc:
        _rm(volumes=False)

    assert exc.value.exit_code == 0
    assert seen["remove_volumes"] is False


def test_an_orphan_is_left_to_the_core_to_classify(monkeypatch):
    """An orphan may still hold data-bearing volumes OR be genuinely gone. The core
    distinguishes those; reporting both as "not running" would be a lie."""
    monkeypatch.setattr(rm_mod, "_project_run_state", lambda _p: "orphan")
    seen: dict = {}
    _stub_remove(monkeypatch, Result(status=Status.WARNING, data=_outcome(found=False)), seen)

    with pytest.raises(typer.Exit) as exc:
        _rm()

    assert exc.value.exit_code == 0  # nothing to remove is a no-op, not a failure
    assert seen["project"] == "proj"


# -------------------------------------------------------------------- exit codes


def test_a_blocked_gate_exits_one_and_redirects_to_the_human_hatch(monkeypatch, running, capsys):
    """The core's own hint names --no-backup, which does not exist on this surface;
    the report must say where that hatch actually is."""
    blocked = _outcome(
        containers_removed=0,
        volumes_removed=0,
        dir_removed=False,
        backup_ok=False,
        failures=["a verified database backup could not be created for 'proj'"],
    )
    _stub_remove(monkeypatch, Result(status=Status.WARNING, data=blocked))

    with pytest.raises(typer.Exit) as exc:
        _rm()

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "Nothing was deleted" in out
    assert "cwcli rm proj --no-backup" in out


def test_a_partial_removal_exits_one_on_a_warning_shaped_envelope(monkeypatch, running, capsys):
    """The exit code reads outcome.failures, NEVER result.status: a partial removal
    is a WARNING-shaped envelope, and WARNING maps to exit 0 everywhere else - so a
    status-driven code would report success for an instance that is still half there."""
    partial = _outcome(dir_removed=False, failures=["failed to remove container 'proj-frappe-1'"])
    _stub_remove(monkeypatch, Result(status=Status.WARNING, data=partial))

    with pytest.raises(typer.Exit) as exc:
        _rm()

    assert exc.value.exit_code == 1
    assert "ok" not in capsys.readouterr().out.split("\n")[0]


def test_a_docker_failure_surfaces_as_a_typed_toon_error(monkeypatch, running, capsys):
    def boom(*_a, **_k):
        raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker.")

    monkeypatch.setattr(core_rm, "remove", boom)

    with pytest.raises(typer.Exit) as exc:
        _rm()

    assert exc.value.exit_code == 1
    assert "error: Could not connect to Docker." in capsys.readouterr().out


# ------------------------------------------------------------------------ narration


def test_stdout_stays_toon_while_progress_goes_to_stderr(monkeypatch, running, capsys):
    def fake(project, *, remove_volumes, no_backup, on_event):
        on_event(core_rm.RmStep(label="Backing up databases...", style="cyan"))
        on_event(core_rm.RmNotice(text="  backed up a.localhost"))
        on_event(core_rm.RmWarning(text="something odd", hint="try this"))
        on_event(core_rm.RmTrace(text="verbose-only diagnostic"))
        return Result(status=Status.OK, data=_outcome())

    monkeypatch.setattr(core_rm, "remove", fake)

    with pytest.raises(typer.Exit):
        _rm()

    captured = capsys.readouterr()
    assert "Backing up databases..." in captured.err
    assert "backed up a.localhost" in captured.err
    assert "Warning: something odd" in captured.err
    # RmTrace is the human verb's --verbose diagnostic; this surface has no --verbose.
    assert "verbose-only diagnostic" not in captured.err
    assert "verbose-only diagnostic" not in captured.out
    assert captured.out.startswith("project: proj")
