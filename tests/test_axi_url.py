"""``cwcli axi url`` verb: resolved host URL + a fresh HTTP probe, as TOON.

The core is stubbed, so these exercise the axi rendering / exit mapping in
isolation: the ``UrlProbe`` TOON on stdout, the stopped-project and multi-bench
needs-choice flag-naming usage errors, and the typed-error mapping. ``core/test_
core_url.py`` covers the resolve/probe logic itself against a faked container.
"""

from __future__ import annotations

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.url import UrlProbe

runner = CliRunner()


def _probe(**overrides):
    defaults = dict(
        project="proj",
        bench_path="/workspace/frappe-bench",
        site="one.localhost",
        url="http://one.localhost:21000",
        reachable=True,
        http_code="200",
    )
    defaults.update(overrides)
    return UrlProbe(**defaults)


class TestAxiUrl:
    def test_success_emits_toon_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_url,
            "probe_url",
            lambda *a, **k: Result(status=Status.OK, data=_probe()),
        )
        result = runner.invoke(axi_mod.app, ["url", "proj"])
        assert result.exit_code == 0
        # TOON quotes a value carrying a `:` or a bare-number-looking string.
        assert 'url: "http://one.localhost:21000"' in result.stdout
        assert 'http_code: "200"' in result.stdout
        assert "reachable: true" in result.stdout

    def test_unreachable_bench_still_exits_0_with_the_observation(self, monkeypatch):
        # An on-demand read that determined "it is not answering" has not failed -
        # the same distinction `axi status`/`axi logs` already draw.
        monkeypatch.setattr(
            axi_mod.core_url,
            "probe_url",
            lambda *a, **k: Result(
                status=Status.OK,
                data=_probe(reachable=False, http_code=None),
            ),
        )
        result = runner.invoke(axi_mod.app, ["url", "proj"])
        assert result.exit_code == 0
        assert "reachable: false" in result.stdout

    def test_unresolved_url_reports_null_with_a_warning(self, monkeypatch):
        from caffeinated_whale_cli.core.envelope import Message

        monkeypatch.setattr(
            axi_mod.core_url,
            "probe_url",
            lambda *a, **k: Result(
                status=Status.OK,
                data=_probe(url=None, reachable=False, http_code=None),
                warnings=[Message("url.unresolved", "Could not resolve the host URL.")],
            ),
        )
        result = runner.invoke(axi_mod.app, ["url", "proj"])
        assert result.exit_code == 0
        assert "url: null" in result.stdout
        assert "Could not resolve the host URL" in result.stdout

    def test_stopped_project_names_start_exit_2(self, monkeypatch):
        choice = Choice(
            kind="confirm_start",
            param="auto_start",
            prompt="Frappe container for project 'proj' is not running. Start it?",
            default="true",
        )
        monkeypatch.setattr(
            axi_mod.core_url,
            "probe_url",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["url", "proj"])
        assert result.exit_code == 2
        assert "cwcli start" in result.stdout

    def test_multi_bench_names_bench_flag_exit_2(self, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="Project 'proj' has multiple benches; select one.",
            options=[{"value": "0", "label": "/w/b0"}, {"value": "1", "label": "/w/b1"}],
        )
        monkeypatch.setattr(
            axi_mod.core_url,
            "probe_url",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["url", "proj"])
        assert result.exit_code == 2
        assert "--bench" in result.stdout

    def test_bench_and_site_flags_reach_the_core(self, monkeypatch):
        captured = {}

        def _fake(project, *, bench=None, bench_path=None, site=None):
            captured["project"] = project
            captured["bench"] = bench
            captured["site"] = site
            return Result(status=Status.OK, data=_probe())

        monkeypatch.setattr(axi_mod.core_url, "probe_url", _fake)
        result = runner.invoke(
            axi_mod.app, ["url", "proj", "--bench", "1", "--site", "other.localhost"]
        )
        assert result.exit_code == 0
        assert captured == {"project": "proj", "bench": "1", "site": "other.localhost"}

    def test_missing_project_renders_not_found_exit_1(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "No such project 'proj'.")

        monkeypatch.setattr(axi_mod.core_url, "probe_url", _raise)
        result = runner.invoke(axi_mod.app, ["url", "proj"])
        assert result.exit_code == 1
        assert "No such project 'proj'" in result.stdout
