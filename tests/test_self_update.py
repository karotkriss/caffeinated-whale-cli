"""``cwcli self-update`` human frontend over ``core.version.check``.

Covers the honest exit-code matrix (dev/uvx no-op, up-to-date, upgrade run,
subprocess failure, network failure) and ``--check`` vs the default run. The
core lookup and the upgrade subprocess are faked; no network, no real upgrade.
"""

import subprocess

import pytest
import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import self_update as su_mod
from caffeinated_whale_cli.core import version as core_version
from caffeinated_whale_cli.core.envelope import Message, Result, Status

runner = CliRunner()

app = typer.Typer()
app.command()(su_mod.self_update)


def _info(**kw):
    base = dict(
        current="0.35.0",
        latest="0.37.0",
        method="uv",
        upgrade_command=["uv", "tool", "upgrade", "caffeinated-whale-cli"],
        is_outdated=True,
        is_dev=False,
        dev_path=None,
    )
    base.update(kw)
    return core_version.VersionInfo(**base)


def _patch_check(monkeypatch, info, *, warnings=None):
    status = Status.WARNING if warnings else Status.OK
    result = Result(status=status, data=info, warnings=warnings or [])
    monkeypatch.setattr(core_version, "check", lambda **kw: result)


@pytest.fixture()
def no_real_subprocess(monkeypatch):
    """Fail loudly if a test would actually shell out."""
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(su_mod.subprocess, "run", fake_run)
    return calls


class TestDevAndUvxNoOp:
    def test_dev_checkout_is_noop_exit_0(self, monkeypatch, no_real_subprocess):
        _patch_check(
            monkeypatch,
            _info(is_dev=True, method="dev", dev_path="/src/cwcli", upgrade_command=None),
        )
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "dev/editable checkout" in result.stdout
        assert "/src/cwcli" in result.stdout
        assert no_real_subprocess == []  # never shells out

    def test_dev_checkout_check_is_noop_exit_0(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info(is_dev=True, method="dev", upgrade_command=None))
        result = runner.invoke(app, ["--check"])
        assert result.exit_code == 0

    def test_uvx_ephemeral_is_noop_exit_0(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info(method="uvx", upgrade_command=None))
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "uvx" in result.stdout
        assert no_real_subprocess == []

    def test_standalone_binary_is_noop_exit_0(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info(method="standalone", upgrade_command=None))
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "standalone binary" in result.stdout
        assert "winget upgrade" in result.stdout
        assert no_real_subprocess == []  # never shells out


class TestDefaultRun:
    def test_up_to_date_exit_0_no_upgrade(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info(current="0.37.0", latest="0.37.0", is_outdated=False))
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "up to date" in result.stdout
        assert no_real_subprocess == []

    def test_dev_ahead_exit_0_no_upgrade(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info(current="0.37.0", latest="0.35.0", is_outdated=False))
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert no_real_subprocess == []

    def test_outdated_runs_upgrade_subprocess(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info())
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert no_real_subprocess == [["uv", "tool", "upgrade", "caffeinated-whale-cli"]]

    def test_upgrade_subprocess_failure_exit_1(self, monkeypatch):
        _patch_check(monkeypatch, _info())
        monkeypatch.setattr(
            su_mod.subprocess,
            "run",
            lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 3),
        )
        result = runner.invoke(app, [])
        assert result.exit_code == 1

    def test_upgrade_binary_missing_exit_1(self, monkeypatch):
        _patch_check(monkeypatch, _info())

        def fake_run(cmd, *a, **kw):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(su_mod.subprocess, "run", fake_run)
        result = runner.invoke(app, [])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_network_failure_blocking_upgrade_exit_1(self, monkeypatch, no_real_subprocess):
        _patch_check(
            monkeypatch,
            _info(latest=None, is_outdated=False),
            warnings=[Message("pypi.unreachable", "no net")],
        )
        result = runner.invoke(app, [])
        assert result.exit_code == 1
        assert no_real_subprocess == []


class TestCheckFlag:
    def test_check_update_available_exit_1(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info())
        result = runner.invoke(app, ["--check"])
        assert result.exit_code == 1
        assert "update is available" in result.stdout
        assert "uv tool upgrade" in result.stdout
        assert no_real_subprocess == []  # --check never executes

    def test_check_up_to_date_exit_0(self, monkeypatch, no_real_subprocess):
        _patch_check(monkeypatch, _info(current="0.37.0", latest="0.37.0", is_outdated=False))
        result = runner.invoke(app, ["--check"])
        assert result.exit_code == 0
        assert "up to date" in result.stdout

    def test_check_network_failure_fails_open_exit_0(self, monkeypatch, no_real_subprocess):
        _patch_check(
            monkeypatch,
            _info(latest=None, is_outdated=False),
            warnings=[Message("pypi.unreachable", "no net")],
        )
        result = runner.invoke(app, ["--check"])
        assert result.exit_code == 0
        assert "could not reach pypi" in result.stdout.lower()


class TestNoCacheFlag:
    def test_no_cache_forwarded_to_core(self, monkeypatch, no_real_subprocess):
        seen = {}

        def fake_check(**kw):
            seen.update(kw)
            return Result(
                status=Status.OK, data=_info(is_outdated=False, current="0.37.0", latest="0.37.0")
            )

        monkeypatch.setattr(core_version, "check", fake_check)
        runner.invoke(app, ["--no-cache"])
        assert seen == {"use_cache": False}
