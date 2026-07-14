"""Unit tests for the shared ``resolve_bench_path`` helper (commands/utils.py).

This is the single resolver behind every command's ``--bench``/``--path`` handling.
It codifies the approved default behavior: single-bench -> that bench; multi-bench
with no selector -> error (data ops) or first+note (``start``); an unknown selector
or a ``--bench``+``--path`` conflict -> clear error.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import utils as cmd_utils


@pytest.fixture()
def cache(monkeypatch):
    """Install a controllable cached-project store for resolve_bench_path."""
    store = {}

    def fake_get(project_name):
        benches = store.get(project_name)
        if benches is None:
            return None
        return {"project_name": project_name, "bench_instances": benches}

    monkeypatch.setattr(cmd_utils.db_utils, "get_cached_project_data", fake_get)
    return store


def test_single_bench_no_selector_returns_that_bench(cache):
    cache["proj"] = [{"path": "/only"}]
    assert cmd_utils.resolve_bench_path("proj", None, None) == "/only"


def test_no_cache_returns_none(cache):
    # Lets the caller fall back to inspect/default.
    assert cmd_utils.resolve_bench_path("proj", None, None) is None


def test_path_override_wins(cache):
    """An explicit bench path overrides the cached bench list."""
    cache["proj"] = [{"path": "/a"}, {"path": "/b"}]
    assert cmd_utils.resolve_bench_path("proj", None, "/explicit") == "/explicit"


def test_bench_and_path_conflict_errors(cache):
    cache["proj"] = [{"path": "/a"}]
    with pytest.raises(typer.Exit) as exc:
        cmd_utils.resolve_bench_path("proj", "0", "/explicit")
    assert exc.value.exit_code == 1


def test_resolve_by_index(cache):
    """`resolve_bench_path` resolves a bench by numeric index."""
    cache["proj"] = [{"path": "/a"}, {"path": "/b"}, {"path": "/c"}]
    assert cmd_utils.resolve_bench_path("proj", "1", None) == "/b"


def test_resolve_by_label(cache):
    """`resolve_bench_path` resolves a bench by user label."""
    cache["proj"] = [{"path": "/a"}, {"path": "/b", "label": "staging"}]
    assert cmd_utils.resolve_bench_path("proj", "staging", None) == "/b"


def test_unknown_selector_errors(cache):
    """`resolve_bench_path` exits 1 on an unknown selector."""
    cache["proj"] = [{"path": "/a"}, {"path": "/b"}]
    with pytest.raises(typer.Exit) as exc:
        cmd_utils.resolve_bench_path("proj", "nope", None)
    assert exc.value.exit_code == 1


def test_multi_bench_no_selector_errors_by_default(cache):
    cache["proj"] = [{"path": "/a"}, {"path": "/b"}]
    with pytest.raises(typer.Exit) as exc:
        cmd_utils.resolve_bench_path("proj", None, None)
    assert exc.value.exit_code == 1


def test_multi_bench_first_mode_returns_first(cache):
    # start uses on_ambiguous="first" to keep working on a multi-bench project.
    cache["proj"] = [{"path": "/a"}, {"path": "/b"}]
    assert cmd_utils.resolve_bench_path("proj", None, None, on_ambiguous="first") == "/a"


def test_multi_bench_first_mode_still_honors_selector(cache):
    cache["proj"] = [{"path": "/a"}, {"path": "/b", "label": "staging"}]
    assert cmd_utils.resolve_bench_path("proj", "staging", None, on_ambiguous="first") == "/b"
