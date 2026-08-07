"""Unit tests for the shared ``resolve_bench_path`` helper (commands/utils.py).

This is the single resolver behind every command's ``--bench``/``--path`` handling.
It codifies the approved default behavior: single-bench -> that bench; multi-bench
with no selector -> error (data ops) or first+note (``start``); an unknown selector
or a ``--bench``+``--path`` conflict -> clear error.
"""

import pytest
import typer

from caffeinated_whale_cli.commands import utils as cmd_utils
from caffeinated_whale_cli.core import resolvers


@pytest.fixture()
def cache(monkeypatch):
    """Install a controllable cached-project store for resolve_bench_path."""
    store = {}

    def fake_get(project_name):
        benches = store.get(project_name)
        if benches is None:
            return None
        return {"project_name": project_name, "bench_instances": benches}

    monkeypatch.setattr(resolvers.db_utils, "get_cached_project_data", fake_get)
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


# --------------------------------------------------------------------------- #
# resolve_bench_path_with_fallback - the shared backup/restore prologue
# (fm/cwcli-backup-restore-autoinspect)
# --------------------------------------------------------------------------- #
class TestResolveBenchPathWithFallback:
    """A cold cache runs ``cwcli inspect`` to populate it before falling back to
    the hardcoded default, instead of dead-ending or silently guessing wrong."""

    def test_cached_bench_skips_inspect_entirely(self, cache, monkeypatch):
        cache["proj"] = [{"path": "/only"}]
        called = []
        monkeypatch.setattr(
            "caffeinated_whale_cli.core.inspect.inspect", lambda *a, **k: called.append(True)
        )
        assert cmd_utils.resolve_bench_path_with_fallback("proj", None, None) == "/only"
        assert called == []

    def test_cold_cache_populates_and_resolves_the_real_bench(self, cache, monkeypatch):
        def fake_inspect(project_name, **kwargs):
            cache[project_name] = [{"path": "/discovered"}]

        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", fake_inspect)
        result = cmd_utils.resolve_bench_path_with_fallback("proj", None, None)
        assert result == "/discovered"

    def test_populate_finding_nothing_degrades_to_the_default(self, cache, monkeypatch):
        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", lambda *a, **k: None)
        result = cmd_utils.resolve_bench_path_with_fallback("proj", None, None)
        assert result == resolvers.DEFAULT_BENCH_PATH

    def test_hard_cwcli_error_from_inspect_aborts_with_exit(self, cache, monkeypatch):
        from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

        def raise_not_found(project_name, **kwargs):
            raise CwcliError(ErrorKind.NOT_FOUND, "bench.none_found", "No Bench Instances found.")

        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", raise_not_found)
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.resolve_bench_path_with_fallback("proj", None, None)
        assert exc.value.exit_code == 1

    def test_non_cwcli_exception_from_inspect_degrades_to_default(self, cache, monkeypatch):
        def boom(project_name, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("caffeinated_whale_cli.core.inspect.inspect", boom)
        result = cmd_utils.resolve_bench_path_with_fallback("proj", None, None)
        assert result == resolvers.DEFAULT_BENCH_PATH
