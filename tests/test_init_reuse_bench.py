"""Tests for bench-target resolution during ``cwcli init``.

Regression coverage for issue #20: when the chosen bench already exists and the
user declines to reuse it, ``init`` must continue site setup on a different
bench name instead of dead-ending with "No changes made".
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.commands.init import (
    _bench_name_validation,
    _resolve_bench_target,
    _wait_for_containers_running,
)


@pytest.fixture
def patched(monkeypatch):
    """Patch questionary, add_path, and _directory_exists used by the resolver.

    Returns ``(questionary_mock, directory_exists_mock)`` so each test can wire
    up prompt answers and bench-existence results. Defaults ``sys.stdin.isatty``
    to True so the interactive resolver loop runs; non-TTY refusal is exercised
    explicitly where needed.
    """
    questionary_mock = MagicMock()
    directory_exists_mock = MagicMock()
    monkeypatch.setattr(init_mod, "questionary", questionary_mock)
    monkeypatch.setattr(init_mod, "add_path", MagicMock())
    monkeypatch.setattr(init_mod, "_directory_exists", directory_exists_mock)
    monkeypatch.setattr(init_mod.sys.stdin, "isatty", lambda: True)
    return questionary_mock, directory_exists_mock


def _container():
    return MagicMock(name="frappe_container")


class _StopInit(Exception):
    """Stop a command-level init test after the container readiness check."""


class TestResolveBenchTarget:
    def test_fresh_bench_is_returned_without_prompting(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = False

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", False)
        questionary_mock.confirm.assert_not_called()
        questionary_mock.text.assert_not_called()
        init_mod.add_path.assert_called_once_with("/workspace/frappe-bench")

    def test_existing_bench_reused_when_confirmed(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True
        questionary_mock.confirm.return_value.ask.return_value = True

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", True)
        questionary_mock.text.assert_not_called()
        init_mod.add_path.assert_called_once_with("/workspace/frappe-bench")

    def test_declining_reuse_continues_with_new_bench_name(self, patched):
        # Regression for issue #20: declining must NOT abort. The user supplies a
        # different bench name and setup continues on a fresh bench.
        questionary_mock, directory_exists_mock = patched
        # First probe: the default bench exists. Second probe: the new name is free.
        directory_exists_mock.side_effect = [True, False]
        questionary_mock.confirm.return_value.ask.return_value = False
        questionary_mock.text.return_value.ask.return_value = "primis-bench"

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("primis-bench", "/workspace/primis-bench", False)
        # Only the finally-resolved bench is persisted, not the declined one.
        init_mod.add_path.assert_called_once_with("/workspace/primis-bench")

    def test_declining_then_naming_another_existing_bench_can_reuse_it(self, patched):
        questionary_mock, directory_exists_mock = patched
        # Both the default and the chosen replacement already exist.
        directory_exists_mock.side_effect = [True, True]
        # Decline the first bench, then reuse the second.
        questionary_mock.confirm.return_value.ask.side_effect = [False, True]
        questionary_mock.text.return_value.ask.return_value = "other-bench"

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("other-bench", "/workspace/other-bench", True)

    def test_declining_reuse_with_blank_name_cancels_cleanly(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True
        questionary_mock.confirm.return_value.ask.return_value = False
        questionary_mock.text.return_value.ask.return_value = ""

        with pytest.raises(typer.Exit) as excinfo:
            _resolve_bench_target(_container(), "/workspace", "frappe-bench")
        assert excinfo.value.exit_code == 0
        # Cancelling must not persist any path to the custom search paths.
        init_mod.add_path.assert_not_called()

    def test_cancelled_confirm_prompt_exits_cleanly(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True
        # Ctrl-C / cancelled confirm yields None.
        questionary_mock.confirm.return_value.ask.return_value = None

        with pytest.raises(typer.Exit) as excinfo:
            _resolve_bench_target(_container(), "/workspace", "frappe-bench")
        assert excinfo.value.exit_code == 0
        init_mod.add_path.assert_not_called()

    def test_replacement_name_is_normalized_to_lowercase(self, patched):
        # A valid mixed-case/whitespace replacement is accepted and normalized
        # without hard-aborting via _validate_slug.
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.side_effect = [True, False]
        questionary_mock.confirm.return_value.ask.return_value = False
        questionary_mock.text.return_value.ask.return_value = "  Primis-Bench  "

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("primis-bench", "/workspace/primis-bench", False)
        init_mod.add_path.assert_called_once_with("/workspace/primis-bench")


class TestReuseBenchFlag:
    """Non-interactive existing-bench handling via --reuse-bench/--no-reuse-bench (issue #41)."""

    def test_reuse_bench_true_reuses_without_prompting(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True

        name, path, exists = _resolve_bench_target(
            _container(), "/workspace", "frappe-bench", reuse_bench=True
        )

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", True)
        questionary_mock.confirm.assert_not_called()
        questionary_mock.text.assert_not_called()
        init_mod.add_path.assert_called_once_with("/workspace/frappe-bench")

    def test_no_reuse_bench_on_existing_exits_1_without_prompting(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True

        with pytest.raises(typer.Exit) as excinfo:
            _resolve_bench_target(_container(), "/workspace", "frappe-bench", reuse_bench=False)

        assert excinfo.value.exit_code == 1
        questionary_mock.confirm.assert_not_called()
        questionary_mock.text.assert_not_called()
        init_mod.add_path.assert_not_called()

    def test_no_flag_non_tty_on_existing_refuses_without_prompting(self, patched, monkeypatch):
        # The reported bug: a non-TTY run used to hang or crash (EOFError) inside
        # questionary. It must now refuse honestly with exit 1 and never prompt.
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True
        monkeypatch.setattr(init_mod.sys.stdin, "isatty", lambda: False)

        with pytest.raises(typer.Exit) as excinfo:
            _resolve_bench_target(_container(), "/workspace", "frappe-bench", reuse_bench=None)

        assert excinfo.value.exit_code == 1
        questionary_mock.confirm.assert_not_called()
        questionary_mock.text.assert_not_called()
        init_mod.add_path.assert_not_called()

    def test_fresh_bench_with_no_reuse_bench_proceeds(self, patched):
        # --no-reuse-bench against a fresh name just asserts freshness; it must
        # NOT block creation.
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = False

        name, path, exists = _resolve_bench_target(
            _container(), "/workspace", "frappe-bench", reuse_bench=False
        )

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", False)
        questionary_mock.confirm.assert_not_called()
        questionary_mock.text.assert_not_called()
        init_mod.add_path.assert_called_once_with("/workspace/frappe-bench")

    def test_fresh_bench_non_tty_no_flag_proceeds(self, patched, monkeypatch):
        # A non-TTY refusal only applies when the bench already exists; a fresh
        # bench must be created without any prompt or refusal.
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = False
        monkeypatch.setattr(init_mod.sys.stdin, "isatty", lambda: False)

        name, path, exists = _resolve_bench_target(
            _container(), "/workspace", "frappe-bench", reuse_bench=None
        )

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", False)
        questionary_mock.confirm.assert_not_called()
        init_mod.add_path.assert_called_once_with("/workspace/frappe-bench")


class TestWaitForContainersRunning:
    """The spinner-race fix: the poll never prompts, so it is safe under a spinner."""

    def test_returns_true_immediately_when_running(self, monkeypatch):
        calls = []

        def fake_ensure(project, **kwargs):
            calls.append(kwargs)
            return True

        monkeypatch.setattr(init_mod, "ensure_containers_running", fake_ensure)

        assert _wait_for_containers_running("proj") is True
        assert len(calls) == 1
        # Never prompts (safe under a spinner) and never auto-starts during the poll.
        assert calls[0]["prompt"] is False
        assert calls[0]["auto_start"] is False

    def test_polls_then_gives_up_without_prompting(self, monkeypatch):
        monkeypatch.setattr(init_mod.time, "sleep", lambda _s: None)
        calls = []

        def fake_ensure(project, **kwargs):
            calls.append(kwargs)
            return False

        monkeypatch.setattr(init_mod, "ensure_containers_running", fake_ensure)

        assert _wait_for_containers_running("proj", attempts=3, delay=0.01) is False
        assert len(calls) == 3
        assert all(c["prompt"] is False for c in calls)


class TestInitContainerReadiness:
    """Command-level coverage for moving the promptable check outside TipSpinner."""

    def test_non_verbose_init_prompts_only_after_spinner_exits(self, monkeypatch, tmp_path):
        calls = []

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

        def fake_ensure(project_name, **kwargs):
            calls.append(
                {
                    "project_name": project_name,
                    "inside_spinner": RecordingSpinner.active,
                    "kwargs": kwargs,
                }
            )
            # The quiet poll under the spinner should give up, which drives the
            # promptable fallback after the spinner has exited.
            return kwargs.get("prompt") is not False

        def stop_after_readiness(_project_name):
            raise _StopInit

        monkeypatch.setattr(init_mod, "TipSpinner", RecordingSpinner)
        monkeypatch.setattr(init_mod.config_utils, "get_show_tips", lambda: False)
        monkeypatch.setattr(
            init_mod, "check_ports_in_use", lambda ports: dict.fromkeys(ports, False)
        )
        monkeypatch.setattr(init_mod, "_setup_project_directory", lambda *a, **k: tmp_path)
        monkeypatch.setattr(init_mod, "_customize_compose_ports", lambda *a, **k: None)
        monkeypatch.setattr(init_mod, "_pull_compose_images", lambda *a, **k: None)
        monkeypatch.setattr(init_mod, "_start_compose_project", lambda *a, **k: None)
        monkeypatch.setattr(init_mod, "ensure_containers_running", fake_ensure)
        monkeypatch.setattr(init_mod.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(init_mod, "get_frappe_container", stop_after_readiness)

        with pytest.raises(_StopInit):
            init_mod.init.__wrapped__(
                project_name="proj",
                port=18000,
                bench_name="frappe-bench",
                site_name="development.localhost",
                bench_parent="/workspace",
                frappe_branch="version-15",
                db_root_password="123",
                admin_password="admin",
                auto_start=False,
                reuse_bench=None,
                verbose=False,
                install_erpnext=False,
                erpnext_branch="version-15",
            )

        spinner_calls = [call for call in calls if call["inside_spinner"]]
        fallback_calls = [call for call in calls if not call["inside_spinner"]]
        assert spinner_calls
        assert all(call["kwargs"]["prompt"] is False for call in spinner_calls)
        assert all(call["kwargs"]["auto_start"] is False for call in spinner_calls)
        assert len(fallback_calls) == 1
        assert "prompt" not in fallback_calls[0]["kwargs"]
        assert fallback_calls[0]["kwargs"]["auto_start"] is False


class TestBenchNameValidation:
    """The reuse-decline prompt validates in place instead of aborting init."""

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_is_allowed_so_cancel_path_can_handle_it(self, value):
        assert _bench_name_validation(value) is True

    @pytest.mark.parametrize("value", ["frappe-bench", "my_bench2", "Primis-Bench", "  abc  "])
    def test_valid_names_pass(self, value):
        assert _bench_name_validation(value) is True

    @pytest.mark.parametrize("value", ["my bench", "bad/name", "name!", "a.b"])
    def test_disallowed_characters_return_error_string(self, value):
        result = _bench_name_validation(value)
        assert isinstance(result, str)

    @pytest.mark.parametrize("value", ["-bench", "bench-", "_bench", "bench_"])
    def test_leading_or_trailing_separator_returns_error_string(self, value):
        result = _bench_name_validation(value)
        assert isinstance(result, str)
