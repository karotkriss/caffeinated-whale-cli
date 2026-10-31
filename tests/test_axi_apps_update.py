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
from caffeinated_whale_cli.core import update_preflight

from .test_apps import (
    FakeFrappeContainer,
    _count_discovery,
    _no_sleep,
    _wire_update,
    fail_requirements_like_uv,
)

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
            "migrate" in cmd_str
            or cmd_str in ("git pull", "bench setup requirements", "bench setup requirements --python")
            or cmd_str.startswith(("git fetch", "git reset --hard"))
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

    # Bypass the @handle_docker_errors docker preflight on the apps commands
    # (mirrors test_apps.py's `wired` fixture; a CI runner has no `docker` binary).
    from caffeinated_whale_cli.utils import docker_utils

    monkeypatch.setattr(docker_utils.shutil, "which", lambda name: "/usr/bin/docker")

    class _Client:
        def ping(self):
            return True

    monkeypatch.setattr(docker_utils.docker, "from_env", lambda: _Client())
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
        skip_requirements=kwargs.pop("skip_requirements", False),
        no_recache=kwargs.pop("no_recache", True),
        force=kwargs.pop("force", False),
        no_preflight=kwargs.pop("no_preflight", False),
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
        skip_requirements=kwargs.pop("skip_requirements", False),
        no_recache=True,
        force=kwargs.pop("force", False),
        no_preflight=kwargs.pop("no_preflight", False),
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

    def test_a_stopped_container_is_a_toon_usage_error_naming_cwcli_start(self, wired, capsys):
        # There is deliberately no --yes on this verb: starting a container is
        # UI-coupled, so a stopped project is a usage error naming `cwcli start`
        # rather than an auto-start, matching axi backup/axi unlock.
        wired.status = "exited"

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 2
        out = capsys.readouterr().out
        assert "help:" in out
        assert "start it first with 'cwcli start <project>'" in out


class TestAxiWrongCopy:
    """BUG-10: a pull that lands in a copy the bench does not import is not-ok."""

    def _patch(self, monkeypatch):
        def _fake(_c, bench, apps):
            return {
                a: core_update.resolvers.AppImport(
                    app=a,
                    entry=f"{bench}/apps/{a}",
                    is_symlink=False,
                    resolved_path=f"{bench}/apps/{a}",
                    imported_path="/workspace/.hdsrc/payments",
                    diverged=True,
                    checked=True,
                )
                for a in apps
            }

        monkeypatch.setattr(core_update.resolvers, "resolve_app_imports", _fake)

    def test_a_wrong_copy_update_exits_one_with_the_divergence_in_toon(
        self, wired, monkeypatch, capsys
    ):
        self._patch(monkeypatch)

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "ok: false" in out
        assert "app_imports" in out
        assert "/workspace/.hdsrc/payments,true,true" in out  # imported,diverged,checked
        # The warning naming both copies rides the TOON document too.
        assert "other copy" in out.lower()


# ------------------------------------------ a failed migrate names its site and the state


class TestAFailedMigrateNamesTheSiteAndTheBenchState:
    """A staging-server run (2026-10-28): the failure was in migrate on one site,
    yet cwcli reported "frappe: Git pull failed" and said nothing about which sites had
    migrated or what the bench was left in. A core update now runs the ordinary
    per-site state machine, so every surface names the stage, the site and the state."""

    def test_axi_names_the_site_and_leaves_nothing_bench_wide(self, wired, capsys):
        wired.fail_on = ["migrate"]

        with pytest.raises(typer.Exit) as exc:
            _axi_update(apps=["frappe"])

        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "failed_migrations" in out and "a.localhost" in out
        assert "failed_apps[0]" in out or "failed_apps: []" in out or "failed_apps[]" in out
        assert "bench_left_in_maintenance: false" in out
        assert "Git pull" not in out

    def test_json_carries_the_stage(self, wired, capsys):
        wired.fail_on = ["migrate"]

        with pytest.raises(typer.Exit) as exc:
            _json_update(apps=["frappe"])

        assert exc.value.exit_code == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc["failed_migrations"] == ["a.localhost"]
        assert doc["failed_apps"] == []
        assert doc["failed_maintenance_disable"] == []
        assert doc["ok"] is False

    def test_the_human_summary_names_the_stage_the_site_and_the_bench_state(self, wired, capsys):
        wired.fail_on = ["migrate"]

        with pytest.raises(typer.Exit) as exc:
            update_mod.run_app_update("proj", ["frappe"], bench_path=BENCH, yes=True)

        assert exc.value.exit_code == 1
        out = " ".join(capsys.readouterr().out.split())
        assert "a.localhost: Migration failed" in out
        assert "Bench state after this run:" in out
        assert "not migrated: a.localhost (migration failed)" in out
        assert "maintenance mode: cleared on every site cwcli touched" in out
        assert "Git pull failed" not in out and "bench update --reset" not in out

    def test_a_failed_core_reset_is_named_as_a_reset_not_a_pull(self, wired, capsys):
        wired.fail_on = ["git reset --hard"]

        with pytest.raises(typer.Exit):
            update_mod.run_app_update("proj", ["frappe"], bench_path=BENCH, yes=True)

        out = capsys.readouterr().out
        assert "frappe: reset to its remote branch failed" in out
        assert "Git pull failed" not in out


# ------------------------------------------------------------- the frappe preflight


@pytest.fixture()
def preflight_finds(monkeypatch):
    """Make the preflight report the given (severity, code) findings; return its calls."""
    calls = []

    def arm(*pairs):
        def fake(container, bench_path, *, project, bench=None, path=None, local_only=False):
            calls.append((project, local_only))
            findings = [
                update_preflight.PreflightFinding(
                    severity=severity,
                    code=code,
                    app="frappe",
                    site=None,
                    message="branch 'feature-x' does not exist on remote 'upstream'",
                    remedy="cwcli apps checkout proj localbranch <published-branch>",
                )
                for severity, code in pairs
            ]
            return update_preflight.PreflightOutcome(
                findings=findings, apps=2, sites=1, elapsed=0.1, bench_path=bench_path
            )

        monkeypatch.setattr(core_update.update_preflight, "run_preflight", fake)
        return calls

    return arm


_BLOCKER = (update_preflight.BLOCKER, "git.branch_not_on_remote")


class TestPreflightOnTheStructuredSurfaces:
    def test_axi_blocked_is_one_toon_document_exit_one_and_nothing_run(
        self, wired, preflight_finds, capsys
    ):
        preflight_finds(_BLOCKER)

        with pytest.raises(typer.Exit) as exc:
            _axi_update(apps=["frappe"])

        assert exc.value.exit_code == 1  # a precondition failure, not a usage error
        out = capsys.readouterr().out
        assert "preflight[1]{severity,code,app,site,message,remedy}:" in out
        assert "blocker,git.branch_not_on_remote,frappe,null," in out
        assert "preflight_blocked: true" in out
        assert "ok: false" in out
        assert out.count("project: proj") == 1  # one document
        assert not any("git reset --hard" in c for c in wired.calls)

    def test_axi_clean_twin_runs_the_reset_and_exits_zero(self, wired, preflight_finds, capsys):
        calls = preflight_finds()

        with pytest.raises(typer.Exit) as exc:
            _axi_update(apps=["frappe"])

        assert exc.value.exit_code == 0
        out = capsys.readouterr().out
        assert "preflight[0]" in out and "preflight_blocked: false" in out
        assert "Updating DocType" not in out  # bench's output still never reaches stdout
        assert calls == [("proj", False)]
        assert any("git reset --hard" in c for c in wired.calls)

    def test_axi_no_preflight_skips_it_and_runs_bench(self, wired, preflight_finds, capsys):
        calls = preflight_finds(_BLOCKER)

        with pytest.raises(typer.Exit) as exc:
            _axi_update(apps=["frappe"], no_preflight=True)

        assert exc.value.exit_code == 0
        assert calls == [("proj", True)]  # only the data-loss guard's local tier
        assert any("git reset --hard" in c for c in wired.calls)

    def test_human_json_blocked_carries_the_findings_and_exits_one(
        self, wired, preflight_finds, capsys
    ):
        preflight_finds(_BLOCKER, (update_preflight.DATA_LOSS, "git.uncommitted"))

        with pytest.raises(typer.Exit) as exc:
            _json_update(apps=["frappe"])

        assert exc.value.exit_code == 1
        doc = json.loads(capsys.readouterr().out)  # still exactly one document
        assert doc["preflight_blocked"] is True
        assert doc["data_loss_refused"] is True  # both reasons, reported apart
        assert doc["data_loss_apps"] == ["frappe"]
        assert [f["code"] for f in doc["preflight"]] == [
            "git.branch_not_on_remote",
            "git.uncommitted",
        ]
        assert doc["preflight"][0]["remedy"].startswith("cwcli apps checkout proj")

    def test_human_json_no_preflight_skips_it(self, wired, preflight_finds, capsys):
        calls = preflight_finds(_BLOCKER)

        _json_update(apps=["frappe"], no_preflight=True)

        assert calls == [("proj", True)]
        assert json.loads(capsys.readouterr().out)["preflight"] == []


# ------------------------------------------------------ --skip-requirements opt-out


class TestSkipRequirements:
    """The requirements step runs by default before any migrate; --skip-requirements
    opts out on both structured surfaces and the document says it was skipped."""

    def test_json_reports_the_requirements_step_by_default(self, wired, capsys):
        _json_update()

        doc = json.loads(capsys.readouterr().out)
        assert doc["requirements"] == "ok"
        assert "bench setup requirements --python" in wired.calls

    def test_json_says_skipped_and_still_exits_zero(self, wired, capsys):
        _json_update(skip_requirements=True)

        doc = json.loads(capsys.readouterr().out)
        assert doc["requirements"] == "skipped" and doc["ok"] is True
        assert not any("setup requirements" in str(c) for c in wired.calls)

    def test_axi_says_skipped(self, wired, capsys):
        with pytest.raises(typer.Exit) as exc:
            _axi_update(skip_requirements=True)

        assert exc.value.exit_code == 0
        out = capsys.readouterr().out
        assert "requirements: skipped" in out and "ok: true" in out

    def test_axi_failed_requirements_exit_one(self, wired, capsys):
        wired.fail_on = ["bench setup requirements"]

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 1
        assert "requirements: failed" in capsys.readouterr().out

    def test_axi_failed_requirements_name_the_failing_dependency(self, wired, capsys):
        # axi forwards no bench output, so the document itself must carry the cause.
        fail_requirements_like_uv(wired)

        with pytest.raises(typer.Exit) as exc:
            _axi_update()

        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "requirements_cause[" in out
        warning = next(line for line in out.splitlines() if "did not finish" in line)
        assert "cwreq-missing was not found in the package registry" in warning
        assert "--skip-requirements" in warning
        assert "CommandFailedError" not in out

    def test_human_output_says_the_step_was_skipped(self, wired, capsys):
        apps_mod.update_apps(
            "proj",
            ["payments"],
            bench=None,
            bench_path=BENCH,
            sites=None,
            clear_cache=False,
            clear_website_cache=False,
            build=False,
            skip_maintenance=False,
            skip_requirements=True,
            no_recache=True,
            force=False,
            no_preflight=False,
            fix_branches=False,
            json_output=False,
            yes=True,
            verbose=False,
        )

        out = capsys.readouterr().out
        assert "Skipped bench setup requirements (--skip-requirements)" in out
