"""``cwcli unlock`` builds container commands as argv lists, never shell strings.

The previous implementation interpolated ``bench_path`` / ``site`` into
``sh -c "..."`` strings, guarded only by a metacharacter denylist. A denylist can
never be complete (a space or a ``*`` glob is not a "special shell character" but
is still shell-significant), so the strings were the fragile thing. These tests
pin that ``test``/``rm`` now run as argv lists (no shell), with the untrusted
path/site carried as a single, un-interpreted list element.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import unlock as unlock_mod
from caffeinated_whale_cli.utils import docker_utils

runner = CliRunner()

BENCH = "/w/bench"


class _FakeFrappe:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"

    def __init__(self):
        self.calls: list[tuple[object, object]] = []

    def exec_run(self, cmd, workdir=None):
        self.calls.append((cmd, workdir))
        return (0, b"")


def _wire(monkeypatch):
    container = _FakeFrappe()
    monkeypatch.setattr(docker_utils.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_utils.docker, "from_env", lambda: type("C", (), {"ping": lambda s: True})()
    )
    monkeypatch.setattr(unlock_mod, "ensure_containers_running", lambda *a, **k: True)
    monkeypatch.setattr(unlock_mod, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(unlock_mod, "resolve_bench_path", lambda *a, **k: BENCH)
    return container


def _app():
    app = typer.Typer()
    app.command()(unlock_mod.unlock)
    return app


def test_every_exec_run_is_an_argv_list(monkeypatch):
    container = _wire(monkeypatch)
    result = runner.invoke(_app(), ["proj", "--site", "example.com"])
    assert result.exit_code == 0
    # No call is a shell string; each is a list (no `sh -c` wrapper anywhere).
    for cmd, _workdir in container.calls:
        assert isinstance(cmd, list)
        assert cmd[0] != "sh"


def test_argv_construction_for_the_three_commands(monkeypatch):
    container = _wire(monkeypatch)
    result = runner.invoke(_app(), ["proj", "--site", "example.com"])
    assert result.exit_code == 0
    cmds = [cmd for cmd, _ in container.calls]
    assert ["test", "-d", f"{BENCH}/sites"] in cmds
    assert ["test", "-d", f"{BENCH}/sites/example.com"] in cmds
    # The rm runs with workdir=bench_path and the locks path as one literal element.
    rm_call = next((c, w) for c, w in container.calls if c[0] == "rm")
    assert rm_call == (["rm", "-rfv", f"{BENCH}/sites/example.com/locks"], BENCH)


def test_shell_significant_site_stays_a_single_literal_element(monkeypatch):
    # A glob/space in the site name is NOT in the denylist, so it passes validation;
    # under the old shell-string form the shell would expand/split it. As an argv
    # element it is a single, literal, un-interpreted token.
    container = _wire(monkeypatch)
    result = runner.invoke(_app(), ["proj", "--site", "ev*l site.com"])
    assert result.exit_code == 0
    site_check = next(c for c, _ in container.calls if c[:2] == ["test", "-d"] and "sites/" in c[2])
    assert site_check == ["test", "-d", f"{BENCH}/sites/ev*l site.com"]
