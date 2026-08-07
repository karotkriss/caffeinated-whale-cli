"""The CLI ``backup`` command's confirm_start retry loop is capped at one re-invoke.

``core.backup`` returns a ``confirm_start`` NEEDS_CHOICE when the frappe container
is down; the CLI reacts by starting it and re-invoking. If the start never takes
(crash-loop / teardown race) the loop must fail closed after ONE retry, not spin
forever. These drive the undecorated command function (bypassing the decorator's
live Docker ping) with a fake core.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import backup as backup_mod
from caffeinated_whale_cli.core.backup import BackupOutcome
from caffeinated_whale_cli.core.envelope import Choice, Result, Status

_CONFIRM_START = Result(
    status=Status.NEEDS_CHOICE,
    choice=Choice(kind="confirm_start", param="auto_start", prompt="Start it?", default="true"),
)


def _wire(monkeypatch, *, backup_side_effects):
    """Stub the interactive prologue + config, and script core.backup's returns."""
    ensure_calls = []
    monkeypatch.setattr(
        backup_mod,
        "ensure_containers_running",
        lambda *a, **k: ensure_calls.append(True) or True,
    )
    monkeypatch.setattr(
        backup_mod, "resolve_bench_path_with_fallback", lambda *a, **k: "/workspace/frappe-bench"
    )
    monkeypatch.setattr(backup_mod.config_utils, "get_show_tips", lambda: False)

    core_calls = {"n": 0}

    def fake_backup(*a, **k):
        i = core_calls["n"]
        core_calls["n"] += 1
        return backup_side_effects[min(i, len(backup_side_effects) - 1)]

    monkeypatch.setattr(backup_mod.core_backup, "backup", fake_backup)
    return ensure_calls, core_calls


def _run():
    # __wrapped__ skips @handle_docker_errors' live Docker ping; site is passed so
    # the default-site block is skipped. Params are positional per the Typer signature.
    backup_mod.backup.__wrapped__(
        "proj",  # project_name
        "s.localhost",  # site
        None,  # bench
        None,  # bench_path
        False,  # with_files
        False,  # yes
        False,  # verbose
    )


def test_second_confirm_start_fails_closed(monkeypatch):
    """A start that never takes stops after ONE retry with a non-zero exit, not a spin."""
    _, core_calls = _wire(monkeypatch, backup_side_effects=[_CONFIRM_START])
    with pytest.raises(typer.Exit) as exc:
        _run()
    assert exc.value.exit_code == 1
    # Exactly two invocations: the initial call + one retry after the attempted start.
    assert core_calls["n"] == 2


def test_confirm_start_then_success_proceeds(monkeypatch):
    """When the start does take, the single retry completes the backup normally."""
    ok = Result(
        status=Status.OK,
        data=BackupOutcome(
            site="s.localhost",
            bench_path="/workspace/frappe-bench",
            artifact_path="/workspace/frappe-bench/sites/s.localhost/private/backups/x.sql.gz",
            included_files=False,
        ),
    )
    _, core_calls = _wire(monkeypatch, backup_side_effects=[_CONFIRM_START, ok])
    _run()  # no exception: success
    assert core_calls["n"] == 2
