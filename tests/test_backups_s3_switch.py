"""``cwcli backups s3 configure`` meets another backup target that is on: it refuses
and names ``--switch``, and ``--switch`` is the explicit choice the core needs."""

from __future__ import annotations

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import backups
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


def test_the_s3_target_switches_only_when_asked(monkeypatch):
    asked = []

    def configure(integration_id, settings, secrets, *, enable, take_over, by):
        asked.append(take_over)
        if not take_over:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "integration.hook_taken",
                "Local directory backup target already provides the backup target.",
            )
        view = {"settings": {k: "x" for k in ("endpoint", "bucket", "prefix", "region")}}
        return None, {**view, "state": "on", "settings": {**view["settings"], "access_key": ""}}

    monkeypatch.setattr(backups.core_integrations, "configure", configure)
    runner = CliRunner()
    refused = runner.invoke(backups.app, ["s3", "configure", "--bucket", "b"])
    assert refused.exit_code == 1 and "pass --switch" in refused.output
    switched = runner.invoke(backups.app, ["s3", "configure", "--bucket", "b", "--switch"])
    assert switched.exit_code == 0, switched.output
    assert asked == [False, True]
