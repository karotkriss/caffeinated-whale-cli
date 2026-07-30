"""``core.scale`` - the port-widening verb, every branch that guards a real property.

The feature widens a Frappe instance's published port range so more than six
benches are host-reachable, database-safe. These tests pin the load-bearing
properties without any Docker: the port truth is READ from each bench's config
(never invented), the reconciliation only ever expands (idempotent no-op when the
range already covers every bench), the whole-instance restart is a CORE consent
decision, the recreate is ``--no-deps`` (never ``down``), and the v13/v14
toolchain repair runs only for a genuinely broken interpreter.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import scale as core_scale
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

# A compose file whose frappe service publishes the init default: six web ports
# (16000-16005 -> 8000-8005) and six socketio ports (17000-17005 -> 9000-9005).
_COMPOSE_TEMPLATE = """\
services:
  frappe:
    image: docker.io/frappe/bench:1.0.0
    ports:
      - {web}
      - {sio}
    working_dir: /workspace
"""


def _compose(web="16000-16005:8000-8005", sio="17000-17005:9000-9005") -> str:
    return _COMPOSE_TEMPLATE.format(web=web, sio=sio)


class FakeContainer:
    """A frappe container that answers exactly the probes ``core.scale`` issues.

    ``configs`` maps a bench path to its ``common_site_config.json`` dict; ``broken``
    is the set of bench paths whose ``env/bin/python`` should report broken; ``majors``
    maps a bench path to the Frappe major its version file reports.
    """

    def __init__(self, *, configs, broken=(), majors=None, status="running"):
        self.configs = configs
        self.broken = set(broken)
        self.majors = majors or {}
        self.status = status
        self.name = "fake-frappe-1"
        self.labels = {"com.docker.compose.service": "frappe"}

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None, environment=None, user=None):
        script = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 else str(cmd)

        if isinstance(cmd, (list, tuple)) and cmd[0] == "cat":
            for path, config in self.configs.items():
                if cmd[1] == f"{path}/sites/common_site_config.json":
                    return (0, json.dumps(config).encode())
            return (1, b"")

        if "env/bin/python" in script:
            for path in self.configs:
                if f"{path}/env/bin/python" in script:
                    return (1 if path in self.broken else 0, b"")
            return (1, b"")

        if "apps/frappe/frappe/__init__.py" in script:
            for path, major in self.majors.items():
                if f"{path}/apps/frappe/frappe/__init__.py" in script:
                    return (0, f'__version__ = "{major}.0.0"\n'.encode())
            return (1, b"")

        return (0, b"")


def test_assigned_port_paths_are_passed_as_argv_data():
    bench_path = "/workspace/bench one; false"

    class RecordingContainer:
        def __init__(self):
            self.calls = []

        def exec_run(self, cmd):
            self.calls.append(cmd)
            return (0, b'{"webserver_port": 8001, "socketio_port": 9001}')

    container = RecordingContainer()
    assigned = core_scale.resolvers.resolve_assigned_ports(
        container, [bench_path], fill_defaults=False
    )

    assert assigned == {bench_path: (8001, 9001)}
    assert container.calls == [["cat", f"{bench_path}/sites/common_site_config.json"]]


@pytest.fixture
def wiring(monkeypatch, tmp_path):
    """Patch the module's Docker/host seams; return a handle to configure/inspect them.

    Writes a real compose file under a temp PROJECTS_DIR so the parse/widen path
    exercises real file I/O.
    """
    from caffeinated_whale_cli.utils import config_utils

    monkeypatch.setattr(config_utils, "PROJECTS_DIR", tmp_path / "projects")

    state = SimpleNamespace(
        container=None,
        benches=[],
        recreate_calls=[],
        start_calls=[],
        align_calls=[],
        pyenv_calls=[],
        nvm_calls=[],
        recreate_rc=0,
        port_check_calls=[],
        ports_in_use=frozenset(),
    )

    def fake_check_ports_in_use(ports, host="0.0.0.0", verbose=False):
        state.port_check_calls.append(list(ports))
        return {p: p in state.ports_in_use for p in ports}

    monkeypatch.setattr(core_scale, "check_ports_in_use", fake_check_ports_in_use)

    def write_compose(project, text):
        conf = tmp_path / "projects" / project / "conf"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "docker-compose.yml").write_text(text)

    state.write_compose = write_compose

    monkeypatch.setattr(core_scale.core_docker, "get_frappe_container", lambda _p: state.container)
    monkeypatch.setattr(core_scale.resolvers, "cached_benches", lambda _p: state.benches)

    def fake_subprocess_run(cmd, cwd=None, capture_output=None, **kwargs):
        if list(cmd) == ["docker", "compose", "version"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        state.recreate_calls.append(cmd)
        return SimpleNamespace(returncode=state.recreate_rc, stdout=b"", stderr=b"err")

    monkeypatch.setattr(core_scale.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(core_scale, "_wait_for_frappe_running", lambda *a, **k: True)

    def fake_align(container, *, chown_home=False):
        state.align_calls.append(chown_home)
        return (True, None)

    monkeypatch.setattr(core_scale, "align_container_user_to_host", fake_align)

    def fake_pyenv(container, prefix, emit, warnings):
        state.pyenv_calls.append(prefix)
        # A successful reinstall un-breaks the bench.
        for path in list(state.container.broken):
            if state.container.majors.get(path) and str(state.container.majors[path]) in (
                "13",
                "14",
            ):
                state.container.broken.discard(path)
        return prefix + ".9"

    def fake_nvm(container, major, emit, warnings):
        state.nvm_calls.append(major)
        return f"v{major}.20.0"

    monkeypatch.setattr(core_scale, "_install_pyenv_python", fake_pyenv)
    monkeypatch.setattr(core_scale, "_install_nvm_node", fake_nvm)

    def fake_start(project, *, bench_path=None, restart=False):
        state.start_calls.append((bench_path, restart))
        return SimpleNamespace(status=Status.OK, data=None, warnings=[])

    monkeypatch.setattr(core_scale.core_start, "start", fake_start)

    return state


def _bench(path, label=None):
    return {"path": path, "label": label}


# --------------------------------------------------------------- pure parse/widen


def test_parse_published_range_reads_base_and_count():
    published = core_scale._parse_published_range(_compose(), "proj")
    assert published.web_base == 16000
    assert published.socketio_base == 17000
    assert published.count == 6


def test_parse_unrecognized_ports_is_precondition():
    with pytest.raises(CwcliError) as exc:
        core_scale._parse_published_range("services:\n  frappe: {}\n", "proj")
    assert exc.value.kind is ErrorKind.PRECONDITION


def test_widen_preserves_host_base_and_grows_high_bound():
    published = core_scale._parse_published_range(_compose(), "proj")
    widened = core_scale._widen_ports_block(_compose(), published, 8)
    assert "16000-16007:8000-8007" in widened
    assert "17000-17007:9000-9007" in widened


# ------------------------------------------------------------------ no-op / consent


def test_no_op_when_range_already_covers(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench("/workspace/frappe-bench"), _bench("/workspace/bench2")]
    wiring.container = FakeContainer(
        configs={
            "/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000},
            "/workspace/bench2": {"webserver_port": 8001, "socketio_port": 9001},
        }
    )

    result = core_scale.scale("proj", consent=False)
    assert result.status is Status.OK
    assert result.data.expanded is False
    assert wiring.recreate_calls == []  # nothing recreated on a no-op
    assert wiring.start_calls == []


def test_expansion_needs_consent(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench(f"/workspace/bench{i}") for i in range(7)]
    wiring.container = FakeContainer(
        configs={
            f"/workspace/bench{i}": {"webserver_port": 8000 + i, "socketio_port": 9000 + i}
            for i in range(7)
        }
    )

    result = core_scale.scale("proj", consent=False)
    assert result.status is Status.NEEDS_CHOICE
    assert result.choice.kind == "confirm_scale"
    assert wiring.recreate_calls == []  # no mutation before consent


# ----------------------------------------------------------------- expand + report


def test_expand_widens_recreates_no_deps_and_restarts_every_bench(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench(f"/workspace/bench{i}") for i in range(7)]
    wiring.container = FakeContainer(
        configs={
            f"/workspace/bench{i}": {"webserver_port": 8000 + i, "socketio_port": 9000 + i}
            for i in range(7)
        }
    )

    result = core_scale.scale("proj", consent=True)
    assert result.status in (Status.OK, Status.WARNING)
    report = result.data
    assert report.expanded is True
    assert report.previous_published_ports == 6
    assert report.published_ports == 7

    # The compose file was widened on disk, host base preserved.
    from caffeinated_whale_cli.utils import config_utils

    compose_text = (config_utils.PROJECTS_DIR / "proj" / "conf" / "docker-compose.yml").read_text()
    assert "16000-16006:8000-8006" in compose_text
    assert "17000-17006:9000-9006" in compose_text

    # The recreate is --no-deps and never `down`.
    assert len(wiring.recreate_calls) == 1
    cmd = wiring.recreate_calls[0]
    assert "--no-deps" in cmd and "frappe" in cmd and "up" in cmd
    assert "down" not in cmd

    # Every bench relaunched.
    assert len(wiring.start_calls) == 7
    assert all(restart for _, restart in wiring.start_calls)
    assert report.benches_restarted == 7

    # The 7th bench (webserver_port 8006 -> host 16006) is now reachable.
    seventh = next(b for b in report.port_map if b.webserver_port == 8006)
    assert seventh.host_web_port == 16006
    assert seventh.reachable is True


def test_new_port_conflict_is_refused_before_any_write(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench(f"/workspace/bench{i}") for i in range(7)]
    wiring.container = FakeContainer(
        configs={
            f"/workspace/bench{i}": {"webserver_port": 8000 + i, "socketio_port": 9000 + i}
            for i in range(7)
        }
    )
    wiring.ports_in_use = frozenset({16006})

    from caffeinated_whale_cli.utils import config_utils

    compose_path = config_utils.PROJECTS_DIR / "proj" / "conf" / "docker-compose.yml"
    compose_before = compose_path.read_text()

    with pytest.raises(CwcliError) as exc:
        core_scale.scale("proj", consent=True)
    assert exc.value.kind is ErrorKind.CONFLICT

    # Only the NEWLY needed ports were checked - the already-published ones are
    # this project's own current binding, not a real conflict.
    checked = {p for call in wiring.port_check_calls for p in call}
    assert checked == {16006, 17006}

    # Refused before any write and before any recreate.
    assert compose_path.read_text() == compose_before
    assert wiring.recreate_calls == []


def test_failed_recreate_rolls_back_the_compose_file(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench(f"/workspace/bench{i}") for i in range(7)]
    wiring.container = FakeContainer(
        configs={
            f"/workspace/bench{i}": {"webserver_port": 8000 + i, "socketio_port": 9000 + i}
            for i in range(7)
        }
    )
    wiring.recreate_rc = 1

    from caffeinated_whale_cli.utils import config_utils

    compose_path = config_utils.PROJECTS_DIR / "proj" / "conf" / "docker-compose.yml"
    compose_before = compose_path.read_text()

    with pytest.raises(CwcliError) as exc:
        core_scale.scale("proj", consent=True)
    assert exc.value.kind is ErrorKind.DOCKER

    # The compose command never actually applied the widened file - restore it so
    # a retry re-attempts the expansion instead of reading the already-widened
    # file as a completed, idempotent no-op.
    assert compose_path.read_text() == compose_before


def test_unreadable_bench_config_reports_unverified_not_fabricated(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [
        _bench("/workspace/frappe-bench"),
        _bench("/workspace/flaky-bench"),
    ]
    wiring.container = FakeContainer(
        configs={"/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000}}
        # "/workspace/flaky-bench" is deliberately absent: its config read fails
        # (exit_code 1), so its assigned ports are genuinely unknown.
    )

    result = core_scale.scale("proj", consent=False)
    assert result.status is Status.WARNING
    assert result.data.expanded is False

    flaky = next(b for b in result.data.port_map if b.bench_path == "/workspace/flaky-bench")
    assert flaky.ports_verified is False
    assert flaky.webserver_port is None
    assert flaky.host_web_port is None
    assert flaky.reachable is False

    known = next(b for b in result.data.port_map if b.bench_path == "/workspace/frappe-bench")
    assert known.ports_verified is True
    assert known.reachable is True

    assert any("flaky-bench" in w.text for w in result.warnings)


def test_to_floor_expands_even_when_benches_fit(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench("/workspace/frappe-bench")]
    wiring.container = FakeContainer(
        configs={"/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000}}
    )

    result = core_scale.scale("proj", to=8, consent=True)
    assert result.data.published_ports == 8
    from caffeinated_whale_cli.utils import config_utils

    compose_text = (config_utils.PROJECTS_DIR / "proj" / "conf" / "docker-compose.yml").read_text()
    assert "16000-16007:8000-8007" in compose_text


def test_bad_to_is_usage_error(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench("/workspace/frappe-bench")]
    wiring.container = FakeContainer(
        configs={"/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000}}
    )
    with pytest.raises(CwcliError) as exc:
        core_scale.scale("proj", to=0, consent=True)
    assert exc.value.kind is ErrorKind.USAGE


# --------------------------------------------------------------- toolchain repair


def test_v14_broken_interpreter_reinstalls_pyenv_and_nvm(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench(f"/workspace/bench{i}") for i in range(7)]
    wiring.container = FakeContainer(
        configs={
            f"/workspace/bench{i}": {"webserver_port": 8000 + i, "socketio_port": 9000 + i}
            for i in range(7)
        },
        broken={"/workspace/bench0"},
        majors={"/workspace/bench0": 14},
    )

    result = core_scale.scale("proj", consent=True)
    assert "/workspace/bench0" in result.data.toolchain_repaired
    assert wiring.pyenv_calls == ["3.10"]  # _BRANCH_PYTHON[14]
    assert wiring.nvm_calls == ["16"]  # _BRANCH_NODE[14]
    # Aligned with chown_home so the installers can write /home/frappe.
    assert wiring.align_calls == [True]


def test_v16_working_interpreter_skips_repair(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench(f"/workspace/bench{i}") for i in range(7)]
    wiring.container = FakeContainer(
        configs={
            f"/workspace/bench{i}": {"webserver_port": 8000 + i, "socketio_port": 9000 + i}
            for i in range(7)
        },
        # nothing broken -> no repair, no align, no installers
    )

    result = core_scale.scale("proj", consent=True)
    assert result.data.toolchain_repaired == []
    assert wiring.pyenv_calls == []
    assert wiring.nvm_calls == []
    assert wiring.align_calls == []


# ------------------------------------------------------------------ preconditions


def test_stopped_instance_is_refused(wiring):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench("/workspace/frappe-bench")]
    wiring.container = FakeContainer(
        configs={"/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000}},
        status="exited",
    )
    with pytest.raises(CwcliError) as exc:
        core_scale.scale("proj", consent=True)
    assert exc.value.kind is ErrorKind.NOT_RUNNING


def test_missing_compose_is_not_found(wiring):
    wiring.benches = [_bench("/workspace/frappe-bench")]
    wiring.container = FakeContainer(
        configs={"/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000}}
    )
    with pytest.raises(CwcliError) as exc:
        core_scale.scale("nope", consent=True)
    assert exc.value.kind is ErrorKind.NOT_FOUND


def test_missing_compose_plugin_refuses_before_any_mutation(wiring, monkeypatch):
    wiring.write_compose("proj", _compose())
    wiring.benches = [_bench("/workspace/frappe-bench")]
    wiring.container = FakeContainer(
        configs={"/workspace/frappe-bench": {"webserver_port": 8000, "socketio_port": 9000}}
    )

    def no_plugin_run(cmd, cwd=None, capture_output=None, **kwargs):
        if list(cmd) == ["docker", "compose", "version"]:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"unknown command")
        raise AssertionError(f"unexpected host command before the compose preflight: {cmd}")

    monkeypatch.setattr(core_scale.subprocess, "run", no_plugin_run)
    with pytest.raises(CwcliError) as exc:
        core_scale.scale("proj", consent=True)
    assert exc.value.kind is ErrorKind.PRECONDITION
    assert exc.value.code == "compose.unavailable"
    assert "docker-compose-plugin" in (exc.value.hint or "")
    # Refused before touching the compose file or any container.
    assert wiring.recreate_calls == []
    assert wiring.start_calls == []
