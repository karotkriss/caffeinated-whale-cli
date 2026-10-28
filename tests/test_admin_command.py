"""``cwcli admin reset-password``: back to admin / admin, change forced; human CLI only."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from caffeinated_whale_cli.core import accounts
from caffeinated_whale_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    accounts.ensure_default(now=1.0)
    accounts.change_password("admin", "correct horse battery", now=1.0)


def test_the_human_verb_resets_and_says_so():
    result = runner.invoke(app, ["admin", "reset-password"])
    assert result.exit_code == 0, result.output
    assert "next sign-in must choose a new one" in " ".join(result.output.split())
    acct = accounts.verify("admin", "admin")
    assert acct is not None and acct.must_change is True
    assert accounts.verify("admin", "correct horse battery") is None


def test_agents_cannot_reset_the_account():
    result = runner.invoke(app, ["axi", "admin", "reset-password"])
    assert result.exit_code != 0
    assert accounts.verify("admin", "correct horse battery") is not None, "nothing changed"


def test_a_shared_install_without_root_refuses(monkeypatch):
    monkeypatch.setattr(accounts.shared_home, "shared_mode", lambda: True)
    monkeypatch.setattr(accounts.os, "geteuid", lambda: 1000, raising=False)
    assert runner.invoke(app, ["admin", "reset-password"]).exit_code == 1
    assert accounts.verify("admin", "correct horse battery") is not None, "nothing changed"
