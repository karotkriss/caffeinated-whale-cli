"""``cwcli axi doctor`` tests: one TOON document, machine-readable per-check status
tokens, and the ruled exit contract (warn exits 0, fail exits non-zero) - identical
to the human surface, both computed from ``DoctorReport.ok``, never ``Result.status``.
"""

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.doctor import Check, CheckStatus, DoctorReport
from caffeinated_whale_cli.core.envelope import Result, Status
from tests.test_axi import assert_is_one_toon_document

runner = CliRunner()


def _report(checks):
    passed = sum(1 for c in checks if c.status is CheckStatus.PASS)
    warned = sum(1 for c in checks if c.status is CheckStatus.WARN)
    failed = sum(1 for c in checks if c.status is CheckStatus.FAIL)
    return DoctorReport(checks=checks, passed=passed, warned=warned, failed=failed, ok=failed == 0)


def _check(id_="c1", status=CheckStatus.PASS, detail="ok", fix=None):
    from caffeinated_whale_cli.core.doctor import CheckResult

    return CheckResult(
        id=id_, title="Docker", group="Docker", status=status, detail=detail, fix=fix
    )


class TestAxiDoctor:
    def test_emits_one_toon_document_and_exits_zero_when_all_pass(self, monkeypatch):
        report = _report([_check()])
        monkeypatch.setattr(
            axi_mod.core_doctor, "run_all", lambda: Result(status=Status.OK, data=report)
        )
        result = runner.invoke(axi_mod.app, ["doctor"])
        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)

    def test_carries_machine_readable_status_tokens(self, monkeypatch):
        report = _report(
            [
                _check("c1", CheckStatus.PASS, "docker 27.1.1"),
                _check("c8", CheckStatus.WARN, "not installed", "install it"),
            ]
        )
        monkeypatch.setattr(
            axi_mod.core_doctor, "run_all", lambda: Result(status=Status.WARNING, data=report)
        )
        result = runner.invoke(axi_mod.app, ["doctor"])
        out = result.stdout
        assert "pass" in out
        assert "warn" in out
        assert "c1" in out and "c8" in out
        assert_is_one_toon_document(out)

    def test_warn_exits_zero_chainable_preflight_gate(self, monkeypatch):
        report = _report([_check("c8", CheckStatus.WARN, "not installed", "install it")])
        monkeypatch.setattr(
            axi_mod.core_doctor, "run_all", lambda: Result(status=Status.WARNING, data=report)
        )
        result = runner.invoke(axi_mod.app, ["doctor"])
        assert result.exit_code == 0

    def test_fail_exits_nonzero(self, monkeypatch):
        report = _report([_check("c2", CheckStatus.FAIL, "daemon unreachable", "start docker")])
        monkeypatch.setattr(
            axi_mod.core_doctor, "run_all", lambda: Result(status=Status.WARNING, data=report)
        )
        result = runner.invoke(axi_mod.app, ["doctor"])
        assert result.exit_code == 1

    def test_takes_no_flags_read_only(self):
        command = next(c for c in axi_mod.app.registered_commands if c.name == "doctor")
        import inspect

        params = list(inspect.signature(command.callback).parameters)
        assert params == []

    def test_registered_as_a_top_level_axi_verb(self):
        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "doctor" in registered
