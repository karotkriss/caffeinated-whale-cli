"""Agent-surface decisions for ``cwcli axi rm-site``."""

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core import rm_site as core_rm_site


def test_missing_yes_is_usage_before_the_core_is_reached(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(core_rm_site, "drop_site", lambda *args, **kwargs: called.append(True))

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_rm_site("missing-project", "missing.localhost", yes=False)

    assert exc.value.exit_code == 2
    assert called == []
    output = capsys.readouterr().out
    assert "error:" in output
    assert "--yes" in output
