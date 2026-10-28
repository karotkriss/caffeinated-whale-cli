"""`cwcli backups s3 fetch` prints each fetched set key on one unbroken stdout line.

A set key is the handle the next command (`backups restore <key>`) takes, so a
non-TTY run (Rich falls back to 80 columns) must not hard-wrap it mid-line.
"""

from types import SimpleNamespace

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import backups as backups_mod

KEY = "cwe2e-gha40719521055-16-shared-main/development.localhost/20261028_213905-b0"


def test_a_long_fetched_key_and_its_verdict_stay_on_one_line(monkeypatch):
    monkeypatch.setattr(
        backups_mod.core_backup_store,
        "offsite_fetch",
        lambda project, key, on_event=None: SimpleNamespace(key=key, verified=True),
    )
    result = CliRunner().invoke(
        backups_mod.app, ["s3", "fetch", "proj", KEY], env={"COLUMNS": "80"}
    )
    assert result.exit_code == 0, result.output
    assert f"{KEY}  verified" in result.stdout.splitlines()
