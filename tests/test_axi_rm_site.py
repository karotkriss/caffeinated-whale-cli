"""Frontend decisions for ``cwcli axi rm-site`` (and the human ``cwcli rm-site``)."""

import pytest
import typer

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import rm_site as rm_site_cmd
from caffeinated_whale_cli.core import rm_site as core_rm_site
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

BENCH_ERROR = "Backup failed for Site a.localhost. Database or site_config.json may be corrupted"


def _backup_failed(*args, **kwargs):
    raise CwcliError(
        ErrorKind.PRECONDITION,
        "rm_site.backup_failed",
        "Could not backup site 'a.localhost'; nothing was dropped.",
        hint="... or drop the site deliberately without a backup with '... --no-backup'.",
        detail={"output": f"Backup command output: {BENCH_ERROR}"},
    )


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


def test_axi_backup_failure_exits_one_with_bench_s_own_error(monkeypatch, capsys):
    monkeypatch.setattr(core_rm_site, "drop_site", _backup_failed)

    with pytest.raises(typer.Exit) as exc:
        axi_mod.axi_rm_site("proj", "a.localhost", bench=None, db_root_password="123", yes=True)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "nothing was dropped" in out
    assert BENCH_ERROR in out
    assert "--no-backup" in out


def test_human_backup_failure_exits_one_with_bench_s_own_error(monkeypatch, capsys):
    monkeypatch.setattr(rm_site_cmd, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(rm_site_cmd, "resolve_bench_path", lambda *a, **k: "/workspace/b")
    monkeypatch.setattr(core_rm_site, "drop_site", _backup_failed)

    with pytest.raises(typer.Exit) as exc:
        rm_site_cmd.rm_site(
            "proj",
            "a.localhost",
            bench=None,
            bench_path=None,
            db_root_password="123",
            yes=True,
            verbose=False,
        )

    assert exc.value.exit_code == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "nothing was dropped" in err
    assert BENCH_ERROR in err
    assert "--no-backup" in err
