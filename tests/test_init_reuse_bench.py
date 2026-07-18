"""Tests for the existing-bench flow of ``cwcli init`` - the FRONTEND half.

Regression coverage for issue #20: when the chosen bench already exists and the
user declines to reuse it, ``init`` must continue site setup on a different
bench name instead of dead-ending with "No changes made".

Re-pointed with its subjects by ``migrate-init-core``, split BY DESIGN:

- The DECISION (the tri-state ``reuse_bench``, the ``confirm_reuse_bench``
  choice, the ``CONFLICT`` refusal, the readiness-poll bounds) lives in the
  core and is pinned by ``tests/test_core_init.py``.
- The PROMPT LOOP (the confirm, the rename prompt, the exit-0 cancels, the
  non-TTY refusal, prompts-only-after-the-spinner-closes) stays here, driven
  through the real command body with the core stubbed to return choices.
- The command-level non-interactive refusals are additionally pinned end-to-end
  by ``tests/test_init_characterization.py``.
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.commands.init import _bench_name_validation
from caffeinated_whale_cli.core import init as core_init
from caffeinated_whale_cli.core.envelope import Choice, Result, Status


def _reuse_choice(bench_name: str) -> Result:
    return Result(
        status=Status.NEEDS_CHOICE,
        choice=Choice(
            kind="confirm_reuse_bench",
            param="reuse_bench",
            prompt=f"Reuse the existing bench '{bench_name}' and continue with site setup?",
            options=[{"value": bench_name, "label": f"/workspace/{bench_name}"}],
            default="true",
        ),
    )


def _ok_report(project: str, bench_name: str, bench_created: bool) -> Result:
    return Result(
        status=Status.OK,
        data=core_init.InitReport(
            project=project,
            bench_name=bench_name,
            bench_path=f"/workspace/{bench_name}",
            site_name="development.localhost",
            bench_created=bench_created,
            site_created=True,
            erpnext_installed=False,
        ),
    )


@pytest.fixture
def harness(monkeypatch):
    """Drive the real ``init`` body with both core stages stubbed.

    ``existing`` names the benches that surface ``confirm_reuse_bench`` when
    ``reuse_bench is None`` (the core's decision, faked here); every resolved
    ``init_bench`` call is recorded as ``(bench_name, reuse_bench)``.
    """
    state = MagicMock()
    state.existing = set()
    state.calls = []
    state.questionary = MagicMock()

    def fake_init_instance(project, **kwargs):
        return Result(status=Status.OK, data=core_init.InstanceUp(project=project, conf_dir="/x"))

    def fake_init_bench(project, *, bench_name, reuse_bench=None, **kwargs):
        if bench_name in state.existing and reuse_bench is None:
            return _reuse_choice(bench_name)
        state.calls.append((bench_name, reuse_bench))
        return _ok_report(project, bench_name, bench_created=bench_name not in state.existing)

    monkeypatch.setattr(init_mod.core_init, "init_instance", fake_init_instance)
    monkeypatch.setattr(init_mod.core_init, "init_bench", fake_init_bench)
    monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
    monkeypatch.setattr(init_mod, "questionary", state.questionary)
    monkeypatch.setattr(init_mod.sys.stdin, "isatty", lambda: True)

    def run(**overrides):
        params = dict(
            project_name="proj",
            port=18500,
            bench_name="frappe-bench",
            site_name="development.localhost",
            bench_parent="/workspace",
            frappe_branch=None,
            version=None,
            db_root_password="123",
            admin_password="admin",
            auto_start=False,
            reuse_bench=None,
            verbose=False,
            install_erpnext=False,
            erpnext_branch="version-16",
        )
        params.update(overrides)
        init_mod.init.__wrapped__(**params)

    state.run = run
    return state


class TestReusePromptLoop:
    """The interactive confirm + decline-and-rename loop (issue #20)."""

    def test_fresh_bench_runs_without_prompting(self, harness):
        harness.run()
        assert harness.calls == [("frappe-bench", None)]
        harness.questionary.confirm.assert_not_called()
        harness.questionary.text.assert_not_called()

    def test_existing_bench_reused_when_confirmed(self, harness):
        harness.existing = {"frappe-bench"}
        harness.questionary.confirm.return_value.ask.return_value = True

        harness.run()

        assert harness.calls == [("frappe-bench", True)]
        harness.questionary.text.assert_not_called()

    def test_declining_reuse_continues_with_new_bench_name(self, harness):
        # Regression for issue #20: declining must NOT abort. The user supplies a
        # different bench name and setup continues on a fresh bench.
        harness.existing = {"frappe-bench"}
        harness.questionary.confirm.return_value.ask.return_value = False
        harness.questionary.text.return_value.ask.return_value = "primis-bench"

        harness.run()

        assert harness.calls == [("primis-bench", None)]

    def test_declining_then_naming_another_existing_bench_can_reuse_it(self, harness):
        # Both the default and the chosen replacement already exist: the choice
        # surfaces AGAIN for the replacement (the re-invocation fixpoint), and
        # confirming reuses it.
        harness.existing = {"frappe-bench", "other-bench"}
        harness.questionary.confirm.return_value.ask.side_effect = [False, True]
        harness.questionary.text.return_value.ask.return_value = "other-bench"

        harness.run()

        assert harness.calls == [("other-bench", True)]

    def test_declining_reuse_with_blank_name_cancels_cleanly(self, harness):
        harness.existing = {"frappe-bench"}
        harness.questionary.confirm.return_value.ask.return_value = False
        harness.questionary.text.return_value.ask.return_value = ""

        with pytest.raises(typer.Exit) as excinfo:
            harness.run()
        assert excinfo.value.exit_code == 0
        # Cancelling never proceeds to a resolved init_bench call.
        assert harness.calls == []

    def test_cancelled_confirm_prompt_exits_cleanly(self, harness):
        harness.existing = {"frappe-bench"}
        # Ctrl-C / cancelled confirm yields None.
        harness.questionary.confirm.return_value.ask.return_value = None

        with pytest.raises(typer.Exit) as excinfo:
            harness.run()
        assert excinfo.value.exit_code == 0
        assert harness.calls == []

    def test_replacement_name_is_normalized_to_lowercase(self, harness):
        harness.existing = {"frappe-bench"}
        harness.questionary.confirm.return_value.ask.return_value = False
        harness.questionary.text.return_value.ask.return_value = "  Primis-Bench  "

        harness.run()

        assert harness.calls == [("primis-bench", None)]

    def test_non_tty_choice_refuses_naming_both_flags(self, harness, monkeypatch, capsys):
        # The reported bug: a non-TTY run used to hang or crash (EOFError) inside
        # questionary. It must now refuse honestly with exit 1 and never prompt.
        harness.existing = {"frappe-bench"}
        monkeypatch.setattr(init_mod.sys.stdin, "isatty", lambda: False)

        with pytest.raises(typer.Exit) as excinfo:
            harness.run()
        assert excinfo.value.exit_code == 1
        harness.questionary.confirm.assert_not_called()
        err = capsys.readouterr().err
        assert "--reuse-bench" in err
        assert "--no-reuse-bench" in err


class TestInitContainerReadiness:
    """The spinner-race fix: prompts run only AFTER the renderer's spinner closed."""

    def test_prompt_runs_after_spinner_closes_then_reinvokes_auto_start(self, monkeypatch):
        calls = []
        instance_calls = []

        class RecordingSpinner:
            active = False

            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                type(self).active = True
                return self

            def __exit__(self, exc_type, exc, tb):
                type(self).active = False
                return False

            def update(self, _message):
                pass

        def fake_init_instance(
            project,
            *,
            port,
            bench_parent="/workspace",
            auto_start=False,
            stream_output=False,
            on_event=None,
        ):
            instance_calls.append(auto_start)
            # Emit a step so the renderer genuinely opens its spinner.
            if on_event is not None:
                on_event(core_init.InitStepStart(phase="pull", message="Pulling Docker images"))
            if not auto_start:
                # First pass: the readiness poll timed out.
                return Result(
                    status=Status.NEEDS_CHOICE,
                    choice=Choice(
                        kind="confirm_start",
                        param="auto_start",
                        prompt="Frappe container for project 'proj' is not running. Start it?",
                        default="true",
                    ),
                )
            return Result(
                status=Status.OK, data=core_init.InstanceUp(project=project, conf_dir="/x")
            )

        def fake_ensure(project_name, **kwargs):
            calls.append({"inside_spinner": RecordingSpinner.active, "kwargs": kwargs})
            return True

        def fake_init_bench(project, **kwargs):
            return _ok_report(project, kwargs["bench_name"], bench_created=True)

        monkeypatch.setattr(init_mod, "TipSpinner", RecordingSpinner)
        monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
        monkeypatch.setattr(init_mod.core_init, "init_instance", fake_init_instance)
        monkeypatch.setattr(init_mod.core_init, "init_bench", fake_init_bench)
        monkeypatch.setattr(init_mod, "ensure_containers_running", fake_ensure)

        init_mod.init.__wrapped__(
            project_name="proj",
            port=18000,
            bench_name="frappe-bench",
            site_name="development.localhost",
            bench_parent="/workspace",
            frappe_branch="version-15",
            version=None,
            db_root_password="123",
            admin_password="admin",
            auto_start=False,
            reuse_bench=None,
            verbose=False,
            install_erpnext=False,
            erpnext_branch="version-15",
        )

        # The promptable ensure_containers_running ran exactly once, OUTSIDE the
        # spinner, with the user's --auto-start (False here, so it may prompt).
        assert len(calls) == 1
        assert calls[0]["inside_spinner"] is False
        assert calls[0]["kwargs"]["auto_start"] is False
        # Stage 1 was re-invoked exactly once, with auto_start=True (the cap).
        assert instance_calls == [False, True]


class TestBenchNameValidation:
    """The reuse-decline prompt validates in place instead of aborting init."""

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_is_allowed_so_cancel_path_can_handle_it(self, value):
        assert _bench_name_validation(value) is True

    @pytest.mark.parametrize("value", ["frappe-bench", "my_bench2", "Primis-Bench", "  abc  "])
    def test_valid_names_pass(self, value):
        """`_bench_name_validation` accepts valid bench names."""
        assert _bench_name_validation(value) is True

    @pytest.mark.parametrize("value", ["my bench", "bad/name", "name!", "a.b"])
    def test_disallowed_characters_return_error_string(self, value):
        result = _bench_name_validation(value)
        assert isinstance(result, str)

    @pytest.mark.parametrize("value", ["-bench", "bench-", "_bench", "bench_"])
    def test_leading_or_trailing_separator_returns_error_string(self, value):
        result = _bench_name_validation(value)
        assert isinstance(result, str)
