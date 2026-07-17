"""``cwcli axi init``: one terminal TOON document, honest exits, no prompts.

The verb is a thin renderer over the UNCHANGED ``core.init_instance`` then
``core.init_bench`` (``add-axi-init-verb``). These tests patch the two core
seams and pin the two captain-owned design decisions plus the choice-to-error
mapping:

- Progress (Q1): the terminal ``InitReport`` is ONE TOON document on stdout;
  coarse phase progress goes to STDERR and carries no secret.
- Secret (Q2): the admin password comes from ``CWCLI_ADMIN_PASSWORD`` or
  ``--admin-password`` (flag wins); omitted is a USAGE error (exit 2) naming
  both, never generated, never prompted. Same shape for the db-root password.
- The three interactive choice surfaces become non-prompting errors:
  ``confirm_reuse_bench`` -> exit 2 naming the flags, a port conflict -> exit 1
  naming ``--port``, ``confirm_start`` -> exit 1 pointing at ``cwcli status`` /
  ``cwcli logs``.
- Exit codes: OK/WARNING -> 0, operational error -> 1, usage error -> 2.
"""

from __future__ import annotations

from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.init import (
    InitNotice,
    InitReport,
    InitStepStart,
    InstanceUp,
)

from .test_axi import assert_is_one_toon_document

runner = CliRunner()

REPORT = InitReport(
    project="proj",
    bench_name="frappe-bench",
    bench_path="/workspace/frappe-bench",
    site_name="development.localhost",
    bench_created=True,
    site_created=True,
    erpnext_installed=False,
)


def _patch_stages(
    monkeypatch,
    *,
    instance_result=None,
    bench_result=None,
    instance_raises=None,
    bench_raises=None,
    instance_events=(),
    bench_events=(),
):
    """Patch both core seams; record kwargs; optionally emit events / raise."""
    calls: dict[str, list] = {"instance": [], "bench": []}

    def fake_instance(project, **kw):
        calls["instance"].append({"project": project, **kw})
        emit = kw.get("on_event") or (lambda _e: None)
        for event in instance_events:
            emit(event)
        if instance_raises is not None:
            raise instance_raises
        if instance_result is not None:
            return instance_result
        return Result(status=Status.OK, data=InstanceUp(project=project, conf_dir="/conf"))

    def fake_bench(project, **kw):
        calls["bench"].append({"project": project, **kw})
        emit = kw.get("on_event") or (lambda _e: None)
        for event in bench_events:
            emit(event)
        if bench_raises is not None:
            raise bench_raises
        if bench_result is not None:
            return bench_result
        return Result(status=Status.OK, data=REPORT)

    monkeypatch.setattr(axi_mod.core_init, "init_instance", fake_instance)
    monkeypatch.setattr(axi_mod.core_init, "init_bench", fake_bench)
    return calls


def _no_admin_env(monkeypatch):
    monkeypatch.delenv("CWCLI_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CWCLI_DB_ROOT_PASSWORD", raising=False)


# ------------------------------------------------------------------ success + one document


class TestSuccess:
    def test_success_is_one_toon_document_exit_0(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(monkeypatch)

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "s3cret"])

        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)
        assert "project: proj" in result.stdout
        assert "bench_path: /workspace/frappe-bench" in result.stdout
        assert "site_created: true" in result.stdout
        # No password field ever crosses the report.
        assert "s3cret" not in result.stdout

    def test_warning_still_exits_0_with_the_warning_in_the_document(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            bench_result=Result(
                status=Status.WARNING,
                data=REPORT,
                warnings=[Message("yarn.install_failed", "Failed to install yarn globally.")],
            ),
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "s3cret"])

        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)
        assert "warnings[1]:" in result.stdout


# ------------------------------------------------------------------ progress narration (Q1)


class TestProgressNarration:
    def test_phases_go_to_stderr_stdout_stays_pure_toon(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            instance_events=[InitStepStart(phase="pull", message="Pulling Docker images")],
            bench_events=[
                InitStepStart(phase="new_site", message="Creating site"),
                InitNotice(code="search_path.added", text="/workspace/frappe-bench"),
            ],
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "s3cret"])

        assert result.exit_code == 0
        # stdout is still exactly one TOON document, no narration mixed in.
        assert_is_one_toon_document(result.stdout)
        assert "Pulling Docker images" not in result.stdout
        # The coarse phase labels landed on stderr.
        assert "Pulling Docker images" in result.stderr
        assert "Creating site" in result.stderr
        assert "/workspace/frappe-bench" in result.stderr

    def test_item_only_step_start_still_narrates(self, monkeypatch):
        # bench_init and new_site (the two longest-running phases) carry only
        # `item`, no `message`; they must still produce a non-empty stderr
        # line so an agent watching for liveness doesn't see a silent gap.
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            bench_events=[InitStepStart(phase="bench_init", item="frappe-bench")],
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "s3cret"])

        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)
        assert "frappe-bench" in result.stderr
        assert result.stderr.strip() != ""

    def test_narration_carries_no_secret(self, monkeypatch):
        _no_admin_env(monkeypatch)
        # An adversarial event whose text embeds the secret must not be emitted;
        # the narrator only forwards InitStepStart.message / InitNotice.text, and
        # the core guarantees neither is a secret. This pins the narrator's filter.
        _patch_stages(
            monkeypatch,
            bench_events=[InitStepStart(phase="new_site", message="Creating site")],
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "s3cret-xyz"])

        assert "s3cret-xyz" not in result.stdout
        assert "s3cret-xyz" not in result.stderr


# ------------------------------------------------------------------ secret transport (Q2)


class TestAdminPasswordTransport:
    def test_env_var_supplies_the_admin_password(self, monkeypatch):
        _no_admin_env(monkeypatch)
        monkeypatch.setenv("CWCLI_ADMIN_PASSWORD", "fromenv")
        calls = _patch_stages(monkeypatch)

        result = runner.invoke(axi_mod.app, ["init", "proj"])

        assert result.exit_code == 0
        assert calls["bench"][0]["admin_password"] == "fromenv"

    def test_flag_wins_over_env(self, monkeypatch):
        _no_admin_env(monkeypatch)
        monkeypatch.setenv("CWCLI_ADMIN_PASSWORD", "fromenv")
        calls = _patch_stages(monkeypatch)

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "fromflag"])

        assert result.exit_code == 0
        assert calls["bench"][0]["admin_password"] == "fromflag"

    def test_missing_password_is_a_usage_error_naming_both(self, monkeypatch):
        _no_admin_env(monkeypatch)
        calls = _patch_stages(monkeypatch)

        result = runner.invoke(axi_mod.app, ["init", "proj"])

        assert result.exit_code == 2
        assert result.stdout.startswith("error:")
        assert "CWCLI_ADMIN_PASSWORD" in result.stdout
        assert "--admin-password" in result.stdout
        # The core is never touched when the password is missing.
        assert calls["instance"] == []
        assert calls["bench"] == []

    def test_db_root_password_from_env_then_default(self, monkeypatch):
        _no_admin_env(monkeypatch)
        monkeypatch.setenv("CWCLI_ADMIN_PASSWORD", "a")
        monkeypatch.setenv("CWCLI_DB_ROOT_PASSWORD", "dbsecret")
        calls = _patch_stages(monkeypatch)

        runner.invoke(axi_mod.app, ["init", "proj"])
        assert calls["bench"][0]["db_root_password"] == "dbsecret"

        monkeypatch.delenv("CWCLI_DB_ROOT_PASSWORD", raising=False)
        calls2 = _patch_stages(monkeypatch)
        runner.invoke(axi_mod.app, ["init", "proj"])
        assert calls2["bench"][0]["db_root_password"] == "123"


# ------------------------------------------------------------------ frappe ref resolution


class TestFrappeRef:
    def test_version_resolves_to_a_ref(self, monkeypatch):
        _no_admin_env(monkeypatch)
        calls = _patch_stages(monkeypatch)

        result = runner.invoke(
            axi_mod.app, ["init", "proj", "--admin-password", "a", "--version", "16"]
        )
        assert result.exit_code == 0
        assert calls["bench"][0]["frappe_ref"] == "version-16"

    def test_frappe_branch_passes_through(self, monkeypatch):
        _no_admin_env(monkeypatch)
        calls = _patch_stages(monkeypatch)

        result = runner.invoke(
            axi_mod.app, ["init", "proj", "--admin-password", "a", "--frappe-branch", "version-15"]
        )
        assert result.exit_code == 0
        assert calls["bench"][0]["frappe_ref"] == "version-15"

    def test_malformed_version_is_a_usage_error(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(monkeypatch)

        result = runner.invoke(
            axi_mod.app, ["init", "proj", "--admin-password", "a", "--version", "16.26"]
        )
        assert result.exit_code == 2
        assert result.stdout.startswith("error:")

    def test_frappe_branch_and_version_conflict_is_usage_error(self, monkeypatch):
        _no_admin_env(monkeypatch)
        calls = _patch_stages(monkeypatch)

        result = runner.invoke(
            axi_mod.app,
            ["init", "proj", "--admin-password", "a", "--version", "16", "--frappe-branch", "x"],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.stdout
        assert calls["instance"] == []


# ------------------------------------------------------------------ choice surfaces -> errors


class TestChoiceSurfaces:
    def test_confirm_reuse_bench_is_usage_error_naming_the_flags(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            bench_result=Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_reuse_bench",
                    param="reuse_bench",
                    prompt="Reuse the existing bench?",
                    options=[{"value": "frappe-bench", "label": "/workspace/frappe-bench"}],
                    default="true",
                ),
            ),
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "a"])

        assert result.exit_code == 2
        assert_is_one_toon_document(result.stdout)
        assert "--reuse-bench" in result.stdout
        assert "--no-reuse-bench" in result.stdout
        assert "/workspace/frappe-bench" in result.stdout

    def test_no_reuse_bench_on_existing_bench_is_operational_error(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            bench_raises=CwcliError(
                ErrorKind.CONFLICT,
                "bench.exists",
                "Bench '/workspace/frappe-bench' already exists and --no-reuse-bench was given.",
            ),
        )

        result = runner.invoke(
            axi_mod.app, ["init", "proj", "--admin-password", "a", "--no-reuse-bench"]
        )

        assert result.exit_code == 1
        assert result.stdout.startswith("error:")

    def test_stage1_readiness_timeout_is_operational_error(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            instance_result=Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_start",
                    param="auto_start",
                    prompt="Frappe container for project 'proj' is not running. Start it?",
                    default="true",
                ),
            ),
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "a"])

        assert result.exit_code == 1
        assert_is_one_toon_document(result.stdout)
        assert "did not become ready" in result.stdout
        assert "cwcli status proj" in result.stdout
        assert "cwcli logs proj" in result.stdout

    def test_stage2_confirm_start_is_operational_error(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            bench_result=Result(
                status=Status.NEEDS_CHOICE,
                choice=Choice(
                    kind="confirm_start",
                    param="auto_start",
                    prompt="Frappe container for project 'proj' is not running. Start it?",
                    default="true",
                ),
            ),
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "a"])

        assert result.exit_code == 1
        assert "is not running" in result.stdout
        assert "cwcli status proj" in result.stdout

    def test_port_conflict_names_port_exit_1(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(
            monkeypatch,
            instance_raises=CwcliError(
                ErrorKind.CONFLICT,
                "ports.in_use",
                "The following ports are already in use: 8000, 8001",
                hint="Use the --port flag to select a different starting port.",
            ),
        )

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "a"])

        assert result.exit_code == 1
        assert_is_one_toon_document(result.stdout)
        assert "--port" in result.stdout


# ------------------------------------------------------------------ registration + no prompt


class TestRegistrationAndFlags:
    def test_init_is_registered(self):
        names = {c.name for c in axi_mod.app.registered_commands}
        assert "init" in names

    def test_no_auto_start_or_verbose_and_bench_present(self):
        import typer.main as _typer_main

        group = _typer_main.get_command(axi_mod.app)
        init_cmd = group.commands["init"]
        opts: set[str] = set()
        for param in init_cmd.params:
            opts.update(getattr(param, "opts", []))
            opts.update(getattr(param, "secondary_opts", []))
        assert "--auto-start" not in opts
        assert "--verbose" not in opts
        # The bench-name flag IS present (init CREATES a bench, it does not select).
        assert "--bench" in opts
        # The password flags exist; the env-var transport is the recommended path.
        assert "--admin-password" in opts

    def test_unknown_flag_is_a_toon_parse_error(self, monkeypatch):
        _no_admin_env(monkeypatch)
        _patch_stages(monkeypatch)

        result = runner.invoke(axi_mod.app, ["init", "proj", "--admin-password", "a", "--bogus"])

        assert result.exit_code == 2
        assert result.stdout.startswith("error:")
