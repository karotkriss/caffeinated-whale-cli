"""``cwcli axi apps update`` + ``cwcli apps update --json`` - the structured surfaces.

Both emit the same `UpdateReport`; only the serialization differs (axi stdout is
ALWAYS TOON, JSON lives only on the human command's --json). What they must both
get right is stdout purity - a bench command's output can never corrupt the one
document there - and an exit code that reads ``report.ok``.

`README.md`'s "apps update delegates to the streaming update flow and has no
--json" is the line this batch deletes; these are what let it go.
"""

import json

import pytest
import typer

from caffeinated_whale_cli.commands import apps as apps_mod
from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.commands import update as update_mod
from caffeinated_whale_cli.core import update as core_update

from .test_apps import FakeFrappeContainer, _count_discovery, _no_sleep, _wire_update

BENCH = "/workspace/frappe-bench"


class _NoisyContainer(FakeFrappeContainer):
    """A container whose bench commands actually emit output.

    The base fake returns "" for migrate/reset, which cannot show a purity break:
    with no bytes to stream, streaming and not streaming look identical.
    """

    NOISE = "Updating DocType...\n✓ migrated\n"

    def _run(self, cmd, workdir=None):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        # fail_on still wins: the base fake owns failure, this only adds output.
        if not any(sub in cmd_str for sub in self.fail_on) and (
            "migrate" in cmd_str or "bench update --reset" in cmd_str or cmd_str == "git pull"
        ):
            self.calls.append(cmd_str)
            return 0, self.NOISE
        return super()._run(cmd, workdir)


@pytest.fixture()
def wired(monkeypatch):
    container = _NoisyContainer(available_apps=["frappe", "payments"])
    _wire_update(monkeypatch, container)
    _no_sleep(monkeypatch)
    _count_discovery(monkeypatch, ["a.localhost"])
    return container


def _json_update(**kwargs):
    kwargs.setdefault("verbose", False)
    apps_mod.update_apps(
        "proj",
        kwargs.pop("apps", ["payments"]),
        bench=None,
        bench_path=BENCH,
        sites=kwargs.pop("sites", None),
        clear_cache=kwargs.pop("clear_cache", False),
        clear_website_cache=kwargs.pop("clear_website_cache", False),
        build=kwargs.pop("build", False),
        skip_maintenance=kwargs.pop("skip_maintenance", False),
        no_recache=kwargs.pop("no_recache", True),
        json_output=True,
        yes=True,
        verbose=kwargs.pop("verbose"),
    )


# ---------------------------------------------------------------- apps update --json


class TestHumanJson:
    def test_stdout_holds_exactly_one_json_document(self, wired, capsys):
        _json_update()

        out = capsys.readouterr().out
        doc = json.loads(out)  # raises if anything else reached stdout
        assert doc["project"] == "proj"
        assert doc["ok"] is True
        assert doc["migrated_sites"] == ["a.localhost"]
        assert "Updating DocType" not in out

    def test_bench_output_never_reaches_stdout_on_the_frappe_path(self, wired, capsys):
        # The path that made this impossible before: _run_frappe_update_reset used to
        # hardcode verbose=True and stream the reset to stdout whatever was asked.
        _json_update(apps=["frappe"])

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["frappe_reset"] is True
        assert "Updating DocType" not in out

    def test_verbose_json_still_keeps_stdout_pure(self, wired, capsys):
        # --json wins over --verbose: the document is the contract.
        _json_update(verbose=True)

        json.loads(capsys.readouterr().out)

    def test_a_partial_failure_is_in_the_document_and_the_exit_code(self, wired, capsys):
        wired.fail_on = ["bench --site a.localhost migrate"]

        with pytest.raises(typer.Exit) as exc:
            _json_update()

        assert exc.value.exit_code == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc["ok"] is False
        assert doc["failed_migrations"] == ["a.localhost"]

    def test_the_document_carries_every_aggregation_field(self, wired, capsys):
        _json_update()

        doc = json.loads(capsys.readouterr().out)
        for field in (
            "failed_apps",
            "unknown_apps",
            "failed_maintenance_enable",
            "failed_migrations",
            "unknown_migrations",
            "failed_builds",
            "unknown_builds",
            "failed_cache_clears",
            "failed_website_cache_clears",
            "failed_maintenance_disable",
            "affected_sites",
            "migrated_sites",
            "aborted",
            "ok",
        ):
            assert field in doc, field


# ---------------------------------------------------------------- axi apps update


def _axi_update(**kwargs):
    axi_mod.axi_apps_update(
        "proj",
        kwargs.pop("apps", ["payments"]),
        bench=kwargs.pop("bench", None),
        sites=kwargs.pop("sites", None),
        clear_cache=False,
        clear_website_cache=False,
        build=False,
        skip_maintenance=False,
        no_recache=True,
        yes=True,
    )


class TestAxiVerb:
    def test_emits_one_toon_document_and_exits_zero(self, wired, capsys):
        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 0
        out = capsys.readouterr().out
        assert "Updating DocType" not in out  # bench output never reaches stdout
        assert "project: proj" in out
        assert "ok: true" in out

    def test_a_partial_failure_exits_one_rather_than_zero(self, wired, capsys):
        # The trap this guards: the shipped `0 if status in (OK, WARNING)` pattern
        # maps a partial-failure WARNING envelope to exit 0. The exit must read
        # report.ok, or the agent surface reports success for a half-failed update.
        wired.fail_on = ["bench --site a.localhost migrate"]

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 1
        assert "ok: false" in capsys.readouterr().out

    def test_unknown_is_distinguishable_from_failed_in_the_document(
        self, monkeypatch, wired, capsys
    ):
        # An agent retries a failure; retrying a migration that may still be running
        # is harmful. The report has to let it tell the two apart.
        from caffeinated_whale_cli.core import exec_stream as exec_stream_mod

        monkeypatch.setattr(exec_stream_mod, "_EXIT_CODE_POLL_TIMEOUT", 0.01)
        api = wired.client.api
        api.exec_inspect = lambda exec_id: {"ExitCode": None, "Running": True}

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "unknown_apps[1]: payments" in out
        assert "failed_apps[1]" not in out

    def test_multibench_without_a_selector_is_a_toon_usage_error_exit_2(
        self, monkeypatch, wired, capsys
    ):
        import types

        from caffeinated_whale_cli.core.envelope import Status

        monkeypatch.setattr(
            core_update.resolvers,
            "resolve_bench",
            lambda *a, **k: types.SimpleNamespace(
                status=Status.NEEDS_CHOICE,
                choice=types.SimpleNamespace(
                    kind="select_bench",
                    param="bench",
                    prompt="which bench?",
                    options=[{"value": "0", "label": "/workspace/frappe-bench"}],
                    default=None,
                ),
                data=None,
                warnings=[],
            ),
        )

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 2
        out = capsys.readouterr().out
        assert "error: multiple benches; pass --bench <index|label>" in out
        assert "options[1]:" in out

    def test_a_hard_error_is_a_toon_error_document(self, wired, capsys):
        wired.fail_on = [f"test -d {BENCH}/apps"]  # no bench there

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 1
        assert "error: Bench directory not found" in capsys.readouterr().out

    def test_a_site_filter_typo_is_a_usage_error_exit_2(self, wired, capsys):
        # The one place the two surfaces deliberately DIFFER: the human CLI keeps
        # exit 1 for this (it always has), while axi maps USAGE -> 2. A difference
        # between surfaces, not a regression on either.
        with pytest.raises(typer.Exit) as exc:
            _axi_update(sites=["nope.localhost"])

        assert exc.value.exit_code == 2
        assert "matched no affected site" in capsys.readouterr().out

    def test_the_same_case_still_exits_1_on_the_human_cli(self, wired, capsys):
        with pytest.raises(typer.Exit) as exc:
            update_mod.run_app_update("proj", ["payments"], sites=["nope.localhost"], yes=True)

        assert exc.value.exit_code == 1
