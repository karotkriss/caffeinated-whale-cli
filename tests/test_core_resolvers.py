"""Split core resolvers + their thin CLI wrappers (tasks 2.1-2.5).

The pure resolvers are driven against faked I/O for every branch; the wrapper
tests assert the CLI translation preserves today's exit codes (a non-TTY stopped
container refuses non-zero, an ambiguous multi-bench errors, --bench+--path is a
usage error), and that no live Docker object leaks across a core boundary.
"""

import dataclasses

import pytest
import typer
from docker.errors import APIError, NotFound

from caffeinated_whale_cli.commands import utils as cmd_utils
from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind


class _FakeContainer:
    def __init__(self, status="running"):
        self.status = status
        self.labels = {"com.docker.compose.service": "frappe"}
        self.reloaded = False

    def reload(self):
        self.reloaded = True


# ------------------------------------------------------------- resolve_container_state


class TestResolveContainerState:
    def test_running(self):
        """`resolve_container_state` reports a running container as running, not start-requested."""
        result = resolvers.resolve_container_state("proj", _FakeContainer("running"))
        assert result.status is Status.OK
        assert result.data.running is True
        assert result.data.start_requested is False

    def test_stopped_with_auto_start_signals_start(self):
        result = resolvers.resolve_container_state(
            "proj", _FakeContainer("exited"), auto_start=True
        )
        assert result.status is Status.OK
        assert result.data.running is False
        assert result.data.start_requested is True

    def test_stopped_offer_choice_returns_confirm_start(self):
        result = resolvers.resolve_container_state("proj", _FakeContainer("exited"))
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"
        assert result.choice.param == "auto_start"

    def test_stopped_no_choice_raises_not_running(self):
        with pytest.raises(CwcliError) as exc:
            resolvers.resolve_container_state("proj", _FakeContainer("exited"), offer_choice=False)
        assert exc.value.kind is ErrorKind.NOT_RUNNING

    def test_not_running_hint_defaults_to_the_yes_wording(self):
        """Callers that DO have a --yes (backup, unlock, the CLI wrapper) are unchanged."""
        with pytest.raises(CwcliError) as exc:
            resolvers.resolve_container_state("proj", _FakeContainer("exited"), offer_choice=False)
        assert exc.value.hint == (
            "Pass --yes to auto-start it, or start it first with 'cwcli start proj'."
        )

    def test_not_running_hint_can_be_supplied_by_the_caller(self):
        """The default names --yes, which a caller like `label` has no such flag for;
        it would tell a user (and, via axi's help line, an agent) to pass a flag that
        does not exist."""
        with pytest.raises(CwcliError) as exc:
            resolvers.resolve_container_state(
                "proj",
                _FakeContainer("exited"),
                offer_choice=False,
                not_running_hint="Start the project first.",
            )
        assert exc.value.hint == "Start the project first."
        assert "--yes" not in exc.value.hint

    def test_container_state_dto_has_no_live_object(self):
        result = resolvers.resolve_container_state("proj", _FakeContainer("running"))
        blob = dataclasses.asdict(result.data)
        assert blob == {"running": True, "start_requested": False}

    @pytest.mark.parametrize("exc", [APIError("boom"), NotFound("gone")])
    def test_reload_docker_error_becomes_typed_error(self, exc):
        """A raw docker error on reload() (e.g. container rm'd mid-op) never leaks the core."""

        class _Exploding(_FakeContainer):
            def reload(self):
                raise exc

        with pytest.raises(CwcliError) as caught:
            resolvers.resolve_container_state("proj", _Exploding("running"))
        assert caught.value.kind is ErrorKind.DOCKER
        assert caught.value.code == "container.reload_failed"


# --------------------------------------------------------------------- resolve_bench


def _patch_benches(monkeypatch, benches):
    monkeypatch.setattr(
        resolvers.db_utils,
        "get_cached_project_data",
        lambda name: {"bench_instances": benches} if benches is not None else None,
    )


class TestResolveBench:
    def test_path_override_wins(self, monkeypatch):
        """An explicit bench path overrides any selector or cache lookup."""
        _patch_benches(monkeypatch, None)
        result = resolvers.resolve_bench("proj", None, "/explicit/path")
        assert result.status is Status.OK
        assert result.data == "/explicit/path"

    def test_bench_and_path_together_is_usage_error(self, monkeypatch):
        _patch_benches(monkeypatch, None)
        with pytest.raises(CwcliError) as exc:
            resolvers.resolve_bench("proj", "0", "/explicit/path")
        assert exc.value.kind is ErrorKind.USAGE

    def test_single_bench_resolves_with_sole_warning(self, monkeypatch):
        _patch_benches(monkeypatch, [{"path": "/w/only"}])
        result = resolvers.resolve_bench("proj", None, None)
        assert result.status is Status.OK
        assert result.data == "/w/only"
        assert result.warnings[0].code == "bench.sole"

    def test_selector_match(self, monkeypatch):
        """`resolve_bench` resolves a bench by user label and by numeric index."""
        _patch_benches(monkeypatch, [{"path": "/w/b0"}, {"path": "/w/b1", "label": "staging"}])
        assert resolvers.resolve_bench("proj", "staging", None).data == "/w/b1"
        assert resolvers.resolve_bench("proj", "0", None).data == "/w/b0"

    def test_unresolved_selector_raises_not_found(self, monkeypatch):
        _patch_benches(monkeypatch, [{"path": "/w/b0"}])
        with pytest.raises(CwcliError) as exc:
            resolvers.resolve_bench("proj", "nope", None)
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_multi_bench_no_selector_returns_choice(self, monkeypatch):
        _patch_benches(monkeypatch, [{"path": "/w/b0"}, {"path": "/w/b1"}])
        result = resolvers.resolve_bench("proj", None, None)
        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"
        assert result.choice.param == "bench"
        assert [o["value"] for o in result.choice.options] == ["0", "1"]

    def test_no_cache_returns_none(self, monkeypatch):
        _patch_benches(monkeypatch, None)
        assert resolvers.resolve_bench("proj", None, None) is None


# --------------------------------------------------------- core docker accessor + wrapper


class TestCoreDockerAccessor:
    def test_daemon_error_raises_docker(self, monkeypatch):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: None)
        with pytest.raises(CwcliError) as exc:
            core_docker.get_frappe_container("proj")
        assert exc.value.kind is ErrorKind.DOCKER

    def test_project_not_found_raises_not_found(self, monkeypatch):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])
        with pytest.raises(CwcliError) as exc:
            core_docker.get_frappe_container("proj")
        assert exc.value.kind is ErrorKind.NOT_FOUND
        assert "not found" in exc.value.message

    def test_no_frappe_service_raises_not_found(self, monkeypatch):
        class _Other:
            labels = {"com.docker.compose.service": "db"}

        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [_Other()])
        with pytest.raises(CwcliError) as exc:
            core_docker.get_frappe_container("proj")
        assert exc.value.kind is ErrorKind.NOT_FOUND

    def test_cli_wrapper_preserves_message_and_exit(self, monkeypatch):
        # docker_utils.get_frappe_container is the CLI wrapper: maps the typed
        # error to the historical stderr message + Exit(1).
        from caffeinated_whale_cli.utils import docker_utils

        monkeypatch.setattr(core_docker, "get_project_containers", lambda name: [])
        with pytest.raises(typer.Exit) as exc:
            docker_utils.get_frappe_container("proj")
        assert exc.value.exit_code == 1


# --------------------------------------------------------- ensure_containers_running wrapper


class TestEnsureContainersRunningWrapper:
    def test_running_returns_true(self, monkeypatch):
        """`ensure_containers_running` returns True when the frappe container is up."""
        monkeypatch.setattr(
            cmd_utils, "get_frappe_container", lambda name: _FakeContainer("running")
        )
        assert cmd_utils.ensure_containers_running("proj", require_running=True) is True

    def test_stopped_non_tty_refuses_exit_1(self, monkeypatch):
        monkeypatch.setattr(
            cmd_utils, "get_frappe_container", lambda name: _FakeContainer("exited")
        )
        monkeypatch.setattr(cmd_utils.sys.stdin, "isatty", lambda: False)
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.ensure_containers_running("proj", require_running=True)
        assert exc.value.exit_code == 1

    def test_stopped_prompt_false_returns_false(self, monkeypatch):
        # The load-bearing rm-recache / inspect-T2 path: silent degrade, no Exit.
        monkeypatch.setattr(
            cmd_utils, "get_frappe_container", lambda name: _FakeContainer("exited")
        )
        assert (
            cmd_utils.ensure_containers_running("proj", require_running=True, prompt=False) is False
        )

    def test_auto_start_starts_and_returns_true(self, monkeypatch):
        started = {}
        monkeypatch.setattr(
            cmd_utils, "get_frappe_container", lambda name: _FakeContainer("exited")
        )
        monkeypatch.setattr(
            cmd_utils,
            "_start_containers_for_command",
            lambda name, verbose: started.setdefault("v", True),
        )
        assert (
            cmd_utils.ensure_containers_running("proj", require_running=True, auto_start=True)
            is True
        )
        assert started["v"] is True


# --------------------------------------------------------- resolve_bench_path wrapper


class TestResolveBenchPathWrapper:
    def test_conflict_exits_1(self, monkeypatch):
        """`resolve_bench_path` exits 1 when it cannot resolve the bench."""
        _patch_benches(monkeypatch, None)
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.resolve_bench_path("proj", "0", "/p")
        assert exc.value.exit_code == 1

    def test_ambiguous_error_exits_1(self, monkeypatch):
        _patch_benches(monkeypatch, [{"path": "/w/b0"}, {"path": "/w/b1"}])
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.resolve_bench_path("proj", None, None)
        assert exc.value.exit_code == 1

    def test_ambiguous_first_returns_first_with_note(self, monkeypatch):
        _patch_benches(monkeypatch, [{"path": "/w/b0"}, {"path": "/w/b1"}])
        assert cmd_utils.resolve_bench_path("proj", None, None, on_ambiguous="first") == "/w/b0"

    def test_no_cache_returns_none(self, monkeypatch):
        _patch_benches(monkeypatch, None)
        assert cmd_utils.resolve_bench_path("proj", None, None) is None

    def test_unresolved_selector_exits_1(self, monkeypatch):
        _patch_benches(monkeypatch, [{"path": "/w/b0"}])
        with pytest.raises(typer.Exit) as exc:
            cmd_utils.resolve_bench_path("proj", "nope", None)
        assert exc.value.exit_code == 1


class _PortContainer:
    """A frappe container with a readable bench config and published port bindings."""

    def __init__(self, *, config='{"webserver_port": 8001, "socketio_port": 9001}',
                 ports=None):
        self.config = config
        self.ports = ports if ports is not None else {
            "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}],
            "8001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21001"}],
        }

    def exec_run(self, cmd, **kwargs):
        if self.config is None:
            return 1, b""
        return 0, self.config.encode()


class TestResolveHostWebUrl:
    """The address a bench is actually reachable at, from the HOST.

    Two hops, and skipping either was the shipped defect: `http://<site>:8000`
    named a CONTAINER port as if it were a host port (wrong under any --port base)
    and ignored per-bench port assignment (wrong for every bench past the first).
    """

    def test_the_container_port_is_mapped_through_the_published_bindings(self):
        url = resolvers.resolve_host_web_url(
            _PortContainer(), "/w/b1", site="two.localhost"
        )
        # bench 1 serves 8001 INSIDE and is published on 21001 OUTSIDE.
        assert url == "http://two.localhost:21001"

    def test_the_site_is_the_host_part_because_frappe_routes_by_host(self):
        url = resolvers.resolve_host_web_url(
            _PortContainer(config='{"webserver_port": 8000, "socketio_port": 9000}'),
            "/w/b0",
            site="one.localhost",
        )
        assert url == "http://one.localhost:21000"

    def test_no_site_still_gives_the_right_port(self):
        url = resolvers.resolve_host_web_url(_PortContainer(), "/w/b1")
        assert url == "http://localhost:21001"

    def test_an_unreadable_bench_config_is_unknown_never_8000(self):
        assert resolvers.resolve_host_web_url(_PortContainer(config=None), "/w/b1") is None

    def test_an_unpublished_container_port_is_unknown_never_the_container_port(self):
        # The 7th bench binds :8006 inside but is published nowhere. Reporting 8006
        # as a host port would send the user to a port nothing listens on.
        c = _PortContainer(config='{"webserver_port": 8006, "socketio_port": 9006}')
        assert resolvers.resolve_host_web_url(c, "/w/b6") is None


class TestResolveRepresentativeSite:
    """Which site stands for a bench when the caller named none."""

    def _cache(self, monkeypatch, *, default=None, sites=()):
        monkeypatch.setattr(resolvers.db_utils, "get_default_site", lambda p, b=None: default)
        monkeypatch.setattr(
            resolvers.db_utils, "get_all_site_configs", lambda p, b=None: {s: {} for s in sites}
        )

    def test_the_benchs_own_default_site_wins(self, monkeypatch):
        self._cache(monkeypatch, default="chosen.localhost", sites=["other.localhost"])
        assert resolvers.resolve_representative_site("proj", "/w/b0") == "chosen.localhost"

    def test_a_bench_with_no_default_falls_back_to_its_only_site(self, monkeypatch):
        # NOT a corner case: a bench `cwcli init` creates has exactly one site and
        # records no default (nothing runs `bench use`), so a default-only lookup
        # answers None on precisely the benches that most need an answer.
        self._cache(monkeypatch, default=None, sites=["only.localhost"])
        assert resolvers.resolve_representative_site("proj", "/w/b0") == "only.localhost"

    def test_several_undefaulted_sites_pick_stably(self, monkeypatch):
        self._cache(monkeypatch, default=None, sites=["b.localhost", "a.localhost"])
        assert resolvers.resolve_representative_site("proj", "/w/b0") == "a.localhost"

    def test_a_bench_with_no_sites_at_all_is_none(self, monkeypatch):
        self._cache(monkeypatch, default=None, sites=[])
        assert resolvers.resolve_representative_site("proj", "/w/b0") is None

    def test_a_failing_cache_read_is_none_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(resolvers.db_utils, "get_default_site", lambda p, b=None: None)

        def boom(*a, **k):
            raise RuntimeError("cache is gone")

        monkeypatch.setattr(resolvers.db_utils, "get_all_site_configs", boom)
        assert resolvers.resolve_representative_site("proj", "/w/b0") is None
