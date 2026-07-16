"""Characterization tests pinning ``cwcli open``'s currently-untested behaviors
BEFORE the core migration (openspec change ``migrate-open-core``), batch 4's
discipline: written green against the unmigrated command, committed separately,
and required to stay green UNCHANGED against the migrated one.

Every patch target here is a surface that survives the migration:
``shutil.which`` (the editor probe on both sides), ``questionary.select``,
``sys.stdin``, the ``core.inspect`` module attributes (the fallback populate
calls through the module on both sides), ``db_utils.get_cached_project_data``
(the cache chokepoint under both ``resolve_bench_path`` and
``resolvers.resolve_bench``), and the frontend's own
``ensure_containers_running``/``exec_into_container`` (prologue and handover,
which stay in ``commands/open.py`` by design). The container lookup is patched
at BOTH chokepoints - the frontend's import today, ``core.docker``'s bound name
after - so the same fixture serves both shapes.
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import open as open_mod
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.utils import db_utils

BENCH = "/workspace/frappe-bench"
BENCH_B = "/home/frappe/other-bench"
DEFAULT_PATH = "/workspace/frappe-bench"

SINGLE_BENCH = {"bench_instances": [{"path": BENCH}]}


class _StubDockerClient:
    def ping(self):
        return True


class _FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _which_factory(*editors):
    installed = {"docker", *editors}
    return lambda name: f"/usr/bin/{name}" if name in installed else None


def _norm(text: str) -> str:
    """Collapse whitespace so Rich's soft-wrapping cannot split an assertion."""
    return " ".join(text.split())


def _patch_common(monkeypatch, *, editors=(), cached=None, tty=False):
    monkeypatch.setattr("shutil.which", _which_factory(*editors))
    monkeypatch.setattr("docker.from_env", lambda: _StubDockerClient())
    monkeypatch.setattr("sys.stdin", _FakeStdin(tty))

    frappe = MagicMock()
    frappe.labels = {"com.docker.compose.service": "frappe"}
    frappe.name = "proj-frappe-1"
    frappe.status = "running"  # the run-state check must see it running

    def containers(_name):
        return [frappe]

    monkeypatch.setattr(open_mod, "get_project_containers", containers, raising=False)
    monkeypatch.setattr("caffeinated_whale_cli.core.docker.get_project_containers", containers)

    monkeypatch.setattr(open_mod, "ensure_containers_running", lambda *a, **k: True)
    exec_mock = MagicMock()
    monkeypatch.setattr(open_mod, "exec_into_container", exec_mock)

    monkeypatch.setattr(db_utils, "get_cached_project_data", lambda _name: cached)
    return exec_mock


def _run_open(**overrides):
    kwargs = dict(
        project_name="proj",
        bench=None,
        bench_path=None,
        app=None,
        code=False,
        code_insiders=False,
        cursor=False,
        docker=False,
        yes=False,
        verbose=False,
    )
    kwargs.update(overrides)
    open_mod.open_bench(**kwargs)


class TestEditorFlagMutualExclusion:
    def test_two_editor_flags_exit_1(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, cached=SINGLE_BENCH)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open(code=True, docker=True)

        assert excinfo.value.exit_code == 1
        err = _norm(capsys.readouterr().err)
        assert "Only one of --code, --code-insiders, --cursor, or --docker" in err
        exec_mock.assert_not_called()


class TestRequestedEditorNotInstalled:
    """A requested-but-absent editor is exit 1 with the install-URL message."""

    def test_code_absent(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, cached=SINGLE_BENCH)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open(code=True)

        assert excinfo.value.exit_code == 1
        err = _norm(capsys.readouterr().err)
        assert "VS Code is not installed" in err
        assert "https://code.visualstudio.com/" in err
        exec_mock.assert_not_called()

    def test_code_insiders_absent(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, cached=SINGLE_BENCH)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open(code_insiders=True)

        assert excinfo.value.exit_code == 1
        err = _norm(capsys.readouterr().err)
        assert "VS Code Insiders is not installed" in err
        assert "https://code.visualstudio.com/insiders/" in err
        exec_mock.assert_not_called()

    def test_cursor_absent(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, cached=SINGLE_BENCH)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open(cursor=True)

        assert excinfo.value.exit_code == 1
        err = _norm(capsys.readouterr().err)
        assert "Cursor is not installed" in err
        assert "https://cursor.sh/" in err
        exec_mock.assert_not_called()


class TestNoFlagEditorSelection:
    def test_only_docker_available_auto_picks_docker(self, monkeypatch):
        # No editors installed, no flag: docker is picked silently, no prompt.
        exec_mock = _patch_common(monkeypatch, cached=SINGLE_BENCH)

        _run_open()

        exec_mock.assert_called_once()
        assert exec_mock.call_args.kwargs["working_dir"] == BENCH

    def test_multi_editor_non_tty_refuses_naming_flags(self, monkeypatch, capsys):
        # An editor IS installed but no flag was given and stdin is not a TTY:
        # refuse (exit 1) naming every selector flag, never hang on questionary.
        exec_mock = _patch_common(monkeypatch, editors=("code",), cached=SINGLE_BENCH, tty=False)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open()

        assert excinfo.value.exit_code == 1
        err = _norm(capsys.readouterr().err)
        for flag in ("--code", "--code-insiders", "--cursor", "--docker"):
            assert flag in err
        exec_mock.assert_not_called()

    def test_questionary_cancel_prints_operation_cancelled(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, editors=("code",), cached=SINGLE_BENCH, tty=True)
        select_mock = MagicMock()
        select_mock.return_value.ask.return_value = None
        monkeypatch.setattr("questionary.select", select_mock)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open()

        assert excinfo.value.exit_code == 1
        assert "Operation cancelled." in _norm(capsys.readouterr().err)
        select_mock.assert_called_once()
        exec_mock.assert_not_called()


class TestFallbackPopulateDegrade:
    """The two SOFT halves of the fallback contract (the hard-``CwcliError``
    abort half is pinned by ``test_open_inspect_fallback.py``)."""

    def test_populate_succeeds_but_nothing_cached_uses_default_path(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, cached=None)
        inspected = []
        monkeypatch.setattr(
            core_inspect, "inspect", lambda project_name, **kwargs: inspected.append(project_name)
        )

        _run_open(docker=True)

        assert inspected == ["proj"]
        exec_mock.assert_called_once()
        assert exec_mock.call_args.kwargs["working_dir"] == DEFAULT_PATH
        err = _norm(capsys.readouterr().err)
        assert "No cached bench path found. Running inspect..." in err
        assert f"Could not detect bench path. Using default: {DEFAULT_PATH}" in err

    def test_non_cwcli_error_from_populate_degrades_to_default(self, monkeypatch, capsys):
        exec_mock = _patch_common(monkeypatch, cached=None)

        def raise_runtime(project_name, **kwargs):
            raise RuntimeError("transient docker hiccup")

        monkeypatch.setattr(core_inspect, "inspect", raise_runtime)

        _run_open(docker=True)

        exec_mock.assert_called_once()
        assert exec_mock.call_args.kwargs["working_dir"] == DEFAULT_PATH
        err = _norm(capsys.readouterr().err)
        assert f"Inspect failed. Using default bench path: {DEFAULT_PATH}" in err


class TestDockerHandover:
    def test_exec_into_container_receives_name_and_bench_dir(self, monkeypatch):
        exec_mock = _patch_common(monkeypatch, cached={"bench_instances": [{"path": BENCH_B}]})

        _run_open(docker=True)

        exec_mock.assert_called_once()
        args, kwargs = exec_mock.call_args
        assert args[0] == "proj-frappe-1"
        assert kwargs["working_dir"] == BENCH_B
