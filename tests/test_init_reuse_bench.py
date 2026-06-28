"""Tests for bench-target resolution during ``cwcli init``.

Regression coverage for issue #20: when the chosen bench already exists and the
user declines to reuse it, ``init`` must continue site setup on a different
bench name instead of dead-ending with "No changes made".
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import init as init_mod
from caffeinated_whale_cli.commands.init import _resolve_bench_target


@pytest.fixture
def patched(monkeypatch):
    """Patch questionary, add_path, and _directory_exists used by the resolver.

    Returns ``(questionary_mock, directory_exists_mock)`` so each test can wire
    up prompt answers and bench-existence results.
    """
    questionary_mock = MagicMock()
    directory_exists_mock = MagicMock()
    monkeypatch.setattr(init_mod, "questionary", questionary_mock)
    monkeypatch.setattr(init_mod, "add_path", MagicMock())
    monkeypatch.setattr(init_mod, "_directory_exists", directory_exists_mock)
    return questionary_mock, directory_exists_mock


def _container():
    return MagicMock(name="frappe_container")


class TestResolveBenchTarget:
    def test_fresh_bench_is_returned_without_prompting(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = False

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", False)
        questionary_mock.confirm.assert_not_called()
        questionary_mock.text.assert_not_called()

    def test_existing_bench_reused_when_confirmed(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True
        questionary_mock.confirm.return_value.ask.return_value = True

        name, path, exists = _resolve_bench_target(_container(), "/workspace", "frappe-bench")

        assert (name, path, exists) == ("frappe-bench", "/workspace/frappe-bench", True)
        questionary_mock.text.assert_not_called()

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

    def test_cancelled_confirm_prompt_exits_cleanly(self, patched):
        questionary_mock, directory_exists_mock = patched
        directory_exists_mock.return_value = True
        # Ctrl-C / cancelled confirm yields None.
        questionary_mock.confirm.return_value.ask.return_value = None

        with pytest.raises(typer.Exit) as excinfo:
            _resolve_bench_target(_container(), "/workspace", "frappe-bench")
        assert excinfo.value.exit_code == 0
