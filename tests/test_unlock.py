"""``unlock`` builds container commands as argv lists, never shell strings.

An older implementation interpolated ``bench_path`` / ``site`` into ``sh -c "..."``
strings, guarded only by a metacharacter denylist. A denylist can never be complete
(a space or a ``*`` glob is not a "special shell character" but is still
shell-significant), so the strings were the fragile thing. These tests pin that
``test``/``rm`` run as argv lists (no shell), with the untrusted path/site carried
as a single, un-interpreted list element.

They target ``core.unlock`` since the logic moved there; the guard is unchanged in
substance, and it now also covers the shared bench-op probes in ``core.resolvers``
that ``backup`` issues through the same helper.
"""

from __future__ import annotations

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import unlock as core_unlock

BENCH = "/w/bench"


class _FakeFrappe:
    labels = {"com.docker.compose.service": "frappe"}
    name = "proj-frappe-1"
    id = "cid"
    status = "running"

    def __init__(self):
        self.calls: list[tuple[object, object]] = []

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None):
        self.calls.append((cmd, workdir))
        return (0, b"")


def _wire(monkeypatch):
    container = _FakeFrappe()
    monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [container])
    monkeypatch.setattr(
        resolvers.db_utils,
        "get_cached_project_data",
        lambda name: {"bench_instances": [{"path": BENCH}]},
    )
    return container


def test_every_exec_run_is_an_argv_list(monkeypatch):
    container = _wire(monkeypatch)
    core_unlock.unlock("proj", site="example.com")
    # No call is a shell string; each is a list (no `sh -c` wrapper anywhere).
    for cmd, _workdir in container.calls:
        assert isinstance(cmd, list)
        assert cmd[0] != "sh"


def test_argv_construction_for_the_three_commands(monkeypatch):
    container = _wire(monkeypatch)
    core_unlock.unlock("proj", site="example.com")
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
    core_unlock.unlock("proj", site="ev*l site.com")
    site_check = next(
        c for c, _ in container.calls if c[:2] == ["test", "-d"] and c[2].endswith("site.com")
    )
    assert site_check == ["test", "-d", f"{BENCH}/sites/ev*l site.com"]
