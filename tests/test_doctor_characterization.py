"""``cwcli doctor`` human frontend over ``core.doctor.run_all``.

Covers the grouped glyph-checklist rendering, the ``-v`` id toggle, and the
ruled exit contract: warn exits clean (0), fail exits non-zero - identical to
``cwcli axi doctor``, both reading ``DoctorReport.ok`` rather than
``Result.status``.
"""

import typer
from typer.testing import CliRunner

from caffeinated_whale_cli import main as main_mod
from caffeinated_whale_cli import update_notice
from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import doctor as doctor_mod
from caffeinated_whale_cli.core.doctor import CheckResult, CheckStatus, DoctorReport
from caffeinated_whale_cli.core.envelope import Result, Status

runner = CliRunner()

app = typer.Typer()
app.command()(doctor_mod.doctor)


def _report(checks):
    passed = sum(1 for c in checks if c.status is CheckStatus.PASS)
    warned = sum(1 for c in checks if c.status is CheckStatus.WARN)
    failed = sum(1 for c in checks if c.status is CheckStatus.FAIL)
    return DoctorReport(checks=checks, passed=passed, warned=warned, failed=failed, ok=failed == 0)


def _check(id_, title, group, status, detail, fix=None, version_verified=None):
    return CheckResult(
        id=id_,
        title=title,
        group=group,
        status=status,
        detail=detail,
        fix=fix,
        version_verified=version_verified,
    )


def _patch(monkeypatch, report):
    status = Status.OK if report.warned == 0 and report.failed == 0 else Status.WARNING
    monkeypatch.setattr(doctor_mod, "run_all", lambda: Result(status=status, data=report))


class TestDoctorRendering:
    def test_groups_checks_and_shows_resolved_values(self, monkeypatch):
        report = _report(
            [
                _check("c1", "Docker", "Docker", CheckStatus.PASS, "Docker version 27.1.1"),
                _check("c8", "sendme", "Transfer", CheckStatus.WARN, "not installed", "install it"),
            ]
        )
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert "Docker" in result.stdout
        assert "Docker version 27.1.1" in result.stdout
        assert "Transfer" in result.stdout
        assert "not installed" in result.stdout
        assert "install it" in result.stdout

    def test_fix_hint_hidden_for_passing_checks(self, monkeypatch):
        report = _report([_check("c1", "Docker", "Docker", CheckStatus.PASS, "ok", "unused fix")])
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert "unused fix" not in result.stdout

    def test_unverified_version_uses_unknown_glyph_and_shows_refresh_hint(self, monkeypatch):
        report = _report(
            [
                _check(
                    "c4",
                    "cwcli version",
                    "cwcli",
                    CheckStatus.PASS,
                    "2.1.0 (release build); could not verify update freshness",
                    "run `cwcli self-update --check` to refresh",
                    False,
                )
            ]
        )
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert "?" in result.stdout
        assert "could not verify" in result.stdout
        assert "update freshness" in result.stdout
        assert "self-update --check" in result.stdout

    def test_verbose_shows_check_ids(self, monkeypatch):
        report = _report([_check("c1", "Docker", "Docker", CheckStatus.PASS, "ok")])
        _patch(monkeypatch, report)
        terse = runner.invoke(app, [])
        verbose = runner.invoke(app, ["-v"])
        assert "[c1]" not in terse.stdout
        assert "[c1]" in verbose.stdout

    def test_summary_line_counts(self, monkeypatch):
        report = _report(
            [
                _check("c1", "A", "G", CheckStatus.PASS, "ok"),
                _check("c2", "B", "G", CheckStatus.WARN, "meh"),
                _check("c3", "C", "G", CheckStatus.FAIL, "broken"),
            ]
        )
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert "1 passed" in result.stdout
        assert "1 warning" in result.stdout
        assert "1 failed" in result.stdout


class TestDoctorExitContract:
    def test_all_pass_exits_zero(self, monkeypatch):
        report = _report([_check("c1", "A", "G", CheckStatus.PASS, "ok")])
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert result.exit_code == 0

    def test_warn_exits_zero_never_blocks(self, monkeypatch):
        report = _report([_check("c8", "sendme", "Transfer", CheckStatus.WARN, "not installed")])
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert result.exit_code == 0

    def test_fail_exits_nonzero(self, monkeypatch):
        report = _report([_check("c2", "Docker daemon", "Docker", CheckStatus.FAIL, "unreachable")])
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert result.exit_code == 1


class TestDoctorReadOnlyEntrypoint:
    def test_human_and_axi_doctor_skip_the_passive_update_refresh(self, monkeypatch):
        report = _report([])
        calls = []
        monkeypatch.setattr(update_notice, "notify_if_outdated", lambda: calls.append(True))
        monkeypatch.setattr(
            doctor_mod,
            "run_all",
            lambda: Result(status=Status.OK, data=report),
        )
        monkeypatch.setattr(
            axi_mod.core_doctor,
            "run_all",
            lambda: Result(status=Status.OK, data=report),
        )

        human = runner.invoke(main_mod.app, ["doctor"])
        axi = runner.invoke(main_mod.app, ["axi", "doctor"])

        assert human.exit_code == 0
        assert axi.exit_code == 0
        assert calls == []

    def test_warn_and_fail_together_still_exits_nonzero(self, monkeypatch):
        report = _report(
            [
                _check("c8", "sendme", "Transfer", CheckStatus.WARN, "not installed"),
                _check("c2", "Docker daemon", "Docker", CheckStatus.FAIL, "unreachable"),
            ]
        )
        _patch(monkeypatch, report)
        result = runner.invoke(app, [])
        assert result.exit_code == 1
