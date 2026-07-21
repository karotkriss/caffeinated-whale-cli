"""``cwcli axi inspect``: one TOON document, honest exits, and NO ``--yes``.

The verb is a thin mapping over ``core.inspect`` (``--update`` -> ``full``,
``--no-refresh`` -> ``cache_only``, default tiered); these tests patch the core
seam and pin the mapping, the one-TOON-document contract (including the nested
bench records), the 0/1/2 exit mapping, the stopped-project usage error naming
``cwcli start``, and - per the ``axi apps update`` incident - that ``--yes`` is
not registered. The ``axi benches`` dead-end hint re-point is pinned here too.
"""

from __future__ import annotations

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.inspect import BenchInfo, InspectReport, SiteInfo

from .test_axi import assert_is_one_toon_document

runner = CliRunner()

REPORT = InspectReport(
    project="proj",
    served_from="partial",
    degraded=False,
    benches=[
        BenchInfo(
            index=0,
            path="/workspace/development/bench-a",
            label="primary",
            current_site="a.localhost",
            default_site="a.localhost",
            available_apps=["erpnext", "frappe"],
            sites=[
                SiteInfo(
                    name="a.localhost",
                    installed_apps=["frappe 15.0.0 version-15"],
                    # served_from="partial": the refs are remembered, not observed.
                    installed_apps_verified=False,
                    has_site_config=True,
                )
            ],
        ),
        BenchInfo(
            index=1,
            path="/workspace/development/bench-b",
            label=None,
            current_site=None,
            default_site=None,
            available_apps=["frappe"],
            sites=[],
        ),
    ],
)


def _patch_inspect(monkeypatch, result):
    calls: list[dict] = []

    def fake_inspect(project, **kwargs):
        calls.append({"project": project, **kwargs})
        return result

    monkeypatch.setattr(axi_mod.core_inspect, "inspect", fake_inspect)
    return calls


class TestAxiInspect:
    def test_success_is_one_toon_document_exit_0(self, monkeypatch):
        _patch_inspect(monkeypatch, Result(status=Status.OK, data=REPORT))

        result = runner.invoke(axi_mod.app, ["inspect", "proj"])

        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)
        assert "project: proj" in result.stdout
        assert "served_from: partial" in result.stdout
        assert "benches[2]:" in result.stdout
        assert "label: primary" in result.stdout
        # The nested site records survive as TOON, never a Python repr.
        assert "installed_apps[1]: frappe 15.0.0 version-15" in result.stdout
        # The per-site verified-or-remembered token rides alongside the ref, so a
        # caller can tell a partial (remembered) read from a full (observed) one.
        assert "installed_apps_verified: false" in result.stdout
        assert "{'" not in result.stdout

    def test_flag_mapping_to_refresh_modes(self, monkeypatch):
        calls = _patch_inspect(monkeypatch, Result(status=Status.OK, data=REPORT))

        runner.invoke(axi_mod.app, ["inspect", "proj"])
        runner.invoke(axi_mod.app, ["inspect", "proj", "--update"])
        runner.invoke(axi_mod.app, ["inspect", "proj", "--no-refresh"])

        assert [c["refresh"] for c in calls] == ["auto", "full", "cache_only"]

    def test_degrade_to_cache_is_a_warning_with_the_data_served_exit_0(self, monkeypatch):
        degraded = InspectReport(
            project="proj", served_from="cache", degraded=True, benches=REPORT.benches
        )
        _patch_inspect(
            monkeypatch,
            Result(
                status=Status.WARNING,
                data=degraded,
                warnings=[Message("inspect.degraded", "Serving cached data without refreshing.")],
            ),
        )

        result = runner.invoke(axi_mod.app, ["inspect", "proj"])

        assert result.exit_code == 0  # WARNING is a completed read
        assert_is_one_toon_document(result.stdout)
        assert "degraded: true" in result.stdout
        assert "warnings[1]:" in result.stdout

    def test_a_typed_error_is_a_toon_error_exit_1(self, monkeypatch):
        def raise_not_found(project, **kwargs):
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "bench.none_found",
                f"No Bench Instances found for project '{project}'.",
            )

        monkeypatch.setattr(axi_mod.core_inspect, "inspect", raise_not_found)

        result = runner.invoke(axi_mod.app, ["inspect", "proj"])

        assert result.exit_code == 1
        assert_is_one_toon_document(result.stdout)
        assert "error:" in result.stdout

    def test_stopped_project_is_a_usage_error_naming_cwcli_start_exit_2(self, monkeypatch):
        _patch_inspect(
            monkeypatch,
            Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_start",
                    param="auto_start",
                    prompt="Frappe container for project 'proj' is not running. Start it?",
                    default="true",
                ),
            ),
        )

        result = runner.invoke(axi_mod.app, ["inspect", "proj", "--update"])

        assert result.exit_code == 2
        assert_is_one_toon_document(result.stdout)
        assert "cwcli start" in result.stdout

    def test_no_yes_flag_is_registered(self, monkeypatch):
        # Per the `axi apps update` incident: an axi verb must never open a
        # start-from-axi path, so --yes must not exist on this verb at all.
        _patch_inspect(monkeypatch, Result(status=Status.OK, data=REPORT))

        result = runner.invoke(axi_mod.app, ["inspect", "proj", "--yes"])

        assert result.exit_code != 0
        assert "No such option" in result.output


class TestAxiBenchesHintRePoint:
    def test_not_inspected_hint_names_the_axi_verb_not_the_human_command(self, monkeypatch):
        def raise_none_cached(project):
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "benches.none_cached",
                f"No cached benches for project '{project}'.",
                hint=f"Run 'cwcli inspect {project}' first.",
            )

        monkeypatch.setattr(axi_mod.core_label, "list_benches", raise_none_cached)

        result = runner.invoke(axi_mod.app, ["benches", "proj"])

        assert result.exit_code == 1
        assert "cwcli axi inspect proj" in result.stdout
