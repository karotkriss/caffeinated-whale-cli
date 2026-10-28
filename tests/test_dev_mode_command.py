"""``cwcli dev-mode``: a set whose cache clear failed must not exit 0."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import dev_mode as cmd
from caffeinated_whale_cli.core import dev_mode as core
from caffeinated_whale_cli.core.envelope import Result, Status


def _state(on: bool) -> core.DevModeState:
    return core.DevModeState(
        project="p",
        bench_path="/workspace/frappe-bench",
        site="s.localhost",
        site_value=on,
        common_value=None,
        effective=on,
        source="site",
        verified=True,
    )


@pytest.mark.parametrize("cleared, code", [(True, 0), (False, 1)])
def test_exit_code_follows_the_cache_clear(monkeypatch, cleared, code):
    monkeypatch.setattr(cmd, "ensure_containers_running", lambda *a, **k: None)
    monkeypatch.setattr(cmd, "resolve_bench_path", lambda *a, **k: "/workspace/frappe-bench")
    outcome = core.DevModeOutcome(
        before=_state(False), after=_state(True), scope="site", on=True, cache_cleared=cleared
    )
    monkeypatch.setattr(
        cmd.core_dev_mode,
        "set_dev_mode",
        lambda *a, **k: Result(status=Status.OK if cleared else Status.WARNING, data=outcome),
    )
    app = typer.Typer()
    app.command()(cmd.dev_mode)
    result = CliRunner().invoke(app, ["p", "s.localhost", "on", "--yes"])
    assert result.exit_code == code
