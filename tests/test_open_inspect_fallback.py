"""Regression test: ``open``'s no-cache fallback populate must ABORT on a hard
``core.inspect`` failure, not degrade to the default bench path.

Root cause: the fallback used to call the ``inspect`` COMMAND, which raised
``typer.Exit`` for a hard failure ("No Bench Instances found", "No containers
found", "No 'frappe' service container found"); ``except typer.Exit: raise``
re-raised it and aborted ``open``. The core migration re-pointed the fallback at
``core.inspect.inspect``, which raises ``CwcliError`` for the exact same
conditions - a plain ``Exception``, NOT caught by ``except typer.Exit: raise``,
so it fell into the generic ``except Exception`` branch and silently continued
with the guessed default path (``/workspace/frappe-bench``) instead of aborting.
The fix adds an explicit ``except CwcliError`` branch (shared via
``commands.inspect.render_error_exit``) that re-raises as ``typer.Exit(1)``,
restoring the pre-migration abort semantics.
"""

from unittest.mock import MagicMock

import pytest
import typer

from caffeinated_whale_cli.commands import open as open_mod
from caffeinated_whale_cli.core import inspect as core_inspect
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


class _StubDockerClient:
    def ping(self):
        return True


def _patch_common(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("docker.from_env", lambda: _StubDockerClient())
    frappe = MagicMock()
    frappe.labels = {"com.docker.compose.service": "frappe"}
    frappe.name = "proj-frappe-1"
    monkeypatch.setattr(open_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(open_mod, "get_project_containers", lambda name: [frappe])
    monkeypatch.setattr(open_mod.vscode_utils, "is_vscode_installed", lambda: False)
    monkeypatch.setattr(open_mod.vscode_utils, "is_vscode_insiders_installed", lambda: False)
    monkeypatch.setattr(open_mod.vscode_utils, "is_cursor_installed", lambda: False)
    # No cache: the first resolve_bench_path call (before the spinner) returns
    # None, which is what triggers the fallback populate under test.
    monkeypatch.setattr(open_mod, "resolve_bench_path", lambda *a, **k: None)


def _run_open(**overrides):
    kwargs = dict(
        project_name="proj",
        bench=None,
        bench_path=None,
        app=None,
        code=False,
        code_insiders=False,
        cursor=False,
        docker=True,
        yes=False,
        verbose=False,
    )
    kwargs.update(overrides)
    open_mod.open_bench(**kwargs)


class TestOpenFallbackAbortsOnHardInspectFailure:
    def test_no_bench_instances_aborts_instead_of_using_default_path(self, monkeypatch):
        _patch_common(monkeypatch)
        exec_mock = MagicMock()
        monkeypatch.setattr(open_mod, "exec_into_container", exec_mock)

        def raise_not_found(project_name, refresh="auto"):
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "bench.none_found",
                f"No Bench Instances found for project '{project_name}'.",
            )

        monkeypatch.setattr(core_inspect, "inspect", raise_not_found)

        with pytest.raises(typer.Exit) as excinfo:
            _run_open()

        assert excinfo.value.exit_code == 1
        # Must abort BEFORE ever falling back to the guessed default path.
        exec_mock.assert_not_called()
