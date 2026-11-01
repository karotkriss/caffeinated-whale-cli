"""Split core resolvers + their thin CLI wrappers (tasks 2.1-2.5).

The pure resolvers are driven against faked I/O for every branch; the wrapper
tests assert the CLI translation preserves today's exit codes (a non-TTY stopped
container refuses non-zero, an ambiguous multi-bench errors, --bench+--path is a
usage error), and that no live Docker object leaks across a core boundary.
"""

import dataclasses
import json

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


B1 = "/w/b1"
_PROCFILE_8001 = "web: bench serve  --port 8001\nsocketio: bench socketio\n"


class _BenchFiles:
    """A frappe container serving a bench's files by path, plus its published ports.

    ``files`` maps an absolute in-container path to its content (a dict is written as
    JSON); any other path reads as missing, as ``cat`` reports it.
    """

    def __init__(self, files, *, ports=None, foreign_listener=False):
        self.foreign_listener = foreign_listener
        self.files = {
            path: json.dumps(body) if isinstance(body, dict) else body
            for path, body in files.items()
        }
        self.ports = (
            ports
            if ports is not None
            else {
                "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}],
                "8001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21001"}],
            }
        )
        self.reads: list[str] = []

    def exec_run(self, cmd, **kwargs):
        if cmd[0] == "python3":
            # The /proc listener check for a bare web line's implicit 8000.
            assert cmd[3:] == ["8000", B1], cmd
            return (1 if self.foreign_listener else 0), b""
        assert cmd[0] == "cat", cmd
        self.reads.append(cmd[1])
        body = self.files.get(cmd[1])
        return (1, b"") if body is None else (0, body.encode())


def _bench(
    common=None,
    *,
    procfile=_PROCFILE_8001,
    sites=None,
    bench=B1,
    ports=None,
    foreign_listener=False,
):
    """A bench's files: its common config, Procfile, and each site's own config."""
    files: dict = {}
    if common is not None:
        files[f"{bench}/sites/common_site_config.json"] = common
    if procfile is not None:
        files[f"{bench}/Procfile"] = procfile
    for site, config in (sites or {}).items():
        files[f"{bench}/sites/{site}/site_config.json"] = config
    return _BenchFiles(files, ports=ports, foreign_listener=foreign_listener)


class TestResolveWebPorts:
    """Where each bench's web process listens, with or without webserver_port.

    The key is optional: an administrator removes it so Frappe stops appending the
    internal port to the URLs it builds behind a reverse proxy, and the bench runs
    fully without it. cwcli then reads the port the Procfile's web line binds.
    """

    def test_the_configured_webserver_port_wins_and_the_procfile_is_not_read(self):
        c = _bench({"webserver_port": 8001, "socketio_port": 9001})
        assert resolvers.resolve_web_ports(c, [B1]) == {B1: 8001}
        assert f"{B1}/Procfile" not in c.reads

    @pytest.mark.parametrize(
        ("web_line", "port"),
        [
            ("web: bench serve  --port 8003", 8003),
            ("web: bench serve --port=8004", 8004),
            ("web: gunicorn -b 127.0.0.1:8005 -w 2 frappe.app:application", 8005),
            ("web: gunicorn --bind=0.0.0.0:8006 frappe.app:application", 8006),
            # bench writes the line without --port when the key is absent at init;
            # bench serve and gunicorn then bind their own default.
            ("web: bench serve", 8000),
        ],
    )
    def test_without_the_key_the_procfile_web_line_names_the_port(self, web_line, port):
        c = _bench({"socketio_port": 9001}, procfile=f"redis_cache: redis-server\n{web_line}\n")
        assert resolvers.resolve_web_ports(c, [B1]) == {B1: port}

    def test_a_bare_web_line_never_borrows_a_sibling_benchs_8000(self):
        # bench writes a bare `bench serve` when the key is absent; that bench cannot
        # bind 8000 while bench 0 holds it, so 8000 would measure bench 0's server.
        c = _bench({"socketio_port": 9001}, procfile="web: bench serve\n", foreign_listener=True)
        assert resolvers.resolve_web_ports(c, [B1]) == {}

    def test_without_the_key_or_a_web_line_the_port_is_unknown_never_8000(self):
        c = _bench({"socketio_port": 9001}, procfile="worker: bench worker\n")
        assert resolvers.resolve_web_ports(c, [B1]) == {}

    def test_an_unreadable_config_is_unknown_never_8000(self):
        assert resolvers.resolve_web_ports(_bench(None), [B1]) == {}

    def test_a_non_numeric_port_is_unknown(self):
        assert resolvers.resolve_web_ports(_bench({"webserver_port": "eight"}), [B1]) == {}

    def test_the_pair_resolver_shares_the_web_answer(self):
        # core.scale and the reverse-proxy forwarding read both ports; the web half
        # must not need the key either, or a bench without it is never published.
        c = _bench({"socketio_port": 9001})
        assert resolvers.resolve_assigned_ports(c, [B1], fill_defaults=False) == {B1: (8001, 9001)}
        assert resolvers.resolve_assigned_ports(
            _bench({}, procfile=None), [B1], fill_defaults=True
        ) == {B1: (8000, 9000)}


class TestResolveSiteAddress:
    """The address a person opens to reach a site, built the way Frappe builds it.

    Frappe's ``frappe.utils.get_url``: host_name, else the site name, with
    ``:<http_port or webserver_port>`` appended when the address names no port and
    no restart-on-update key is set. A localhost address is opened on the HOST port
    Docker publishes, key or no key, because that is the only way the host reaches it.
    """

    KEY = {"webserver_port": 8001, "socketio_port": 9001}
    NO_KEY = {"socketio_port": 9001}

    def _address(self, common, site, *, sites=None, **kwargs):
        return resolvers.resolve_site_address(_bench(common, sites=sites, **kwargs), B1, site=site)

    def test_a_localhost_site_opens_on_the_published_port(self):
        a = self._address(self.KEY, "two.localhost")
        # bench 1 serves 8001 INSIDE and is published on 21001 OUTSIDE.
        assert a.url == "http://two.localhost:21001"
        assert (a.source, a.internal_port) == ("site", False)

    def test_a_localhost_site_keeps_the_published_port_without_the_key(self):
        a = self._address(self.NO_KEY, "two.localhost")
        assert a.url == "http://two.localhost:21001"
        assert a.internal_port is False
        assert a.webserver_port_in == []
        # The same pass resolved the container port from the Procfile's web line.
        assert a.web_port == 8001

    def test_no_site_still_gives_the_right_port(self):
        assert self._address(self.KEY, None).url == "http://localhost:21001"

    @pytest.mark.parametrize("site", ["127.0.0.1", "LOCALHOST", "a.b.localhost"])
    def test_every_loopback_name_counts_as_localhost(self, site):
        a = self._address(self.KEY, site)
        assert a.url == f"http://{site}:21001"
        assert a.internal_port is False

    def test_a_hostname_keeps_the_port_while_the_key_is_set_and_says_so(self):
        a = self._address(self.KEY, "site.example.com")
        # Never stripped or rewritten: it is exactly what Frappe reports for the site.
        assert a.url == "http://site.example.com:8001"
        assert a.internal_port is True
        assert a.webserver_port_in == ["sites/common_site_config.json"]

    def test_the_key_in_the_sites_own_config_counts_too(self):
        a = self._address(
            self.NO_KEY,
            "site.example.com",
            sites={"site.example.com": {"webserver_port": 8001}},
        )
        assert a.url == "http://site.example.com:8001"
        assert a.internal_port is True
        assert a.webserver_port_in == ["sites/site.example.com/site_config.json"]

    def test_a_hostname_without_the_key_opens_by_name_with_no_port(self):
        a = self._address(self.NO_KEY, "site.example.com", ports={})
        # Frappe builds the same portless URL; no host binding is needed for it.
        assert a.url == "http://site.example.com"
        assert a.internal_port is False

    def test_host_name_without_the_key_is_the_sites_address_as_written(self):
        a = self._address(
            self.NO_KEY,
            "site.example.com",
            sites={"site.example.com": {"host_name": "https://site.example.com"}},
        )
        assert a.url == "https://site.example.com"
        assert (a.source, a.internal_port) == ("host_name", False)

    def test_frappe_appends_the_key_to_a_host_name_that_names_no_port(self):
        a = self._address(
            self.KEY,
            "site.example.com",
            sites={"site.example.com": {"host_name": "https://site.example.com"}},
        )
        # Every URL Frappe builds for this site, OAuth redirects included, carries it.
        assert a.url == "https://site.example.com:8001"
        assert (a.source, a.internal_port) == ("host_name", True)

    def test_a_host_name_that_names_its_port_is_used_as_written(self):
        a = self._address(
            self.KEY,
            "site.example.com",
            sites={"site.example.com": {"host_name": "https://site.example.com:8443"}},
        )
        assert a.url == "https://site.example.com:8443"
        assert a.internal_port is False

    @pytest.mark.parametrize("key", ["restart_supervisor_on_update", "restart_systemd_on_update"])
    def test_a_production_bench_gets_no_port_appended(self, key):
        a = self._address({**self.KEY, key: 1}, "site.example.com")
        assert a.url == "http://site.example.com"
        assert a.internal_port is False

    def test_http_port_wins_over_the_key_and_is_not_the_internal_port(self):
        a = self._address({**self.KEY, "http_port": 8080}, "site.example.com")
        assert a.url == "http://site.example.com:8080"
        assert a.internal_port is False

    def test_hostname_is_read_when_host_name_is_absent(self):
        a = self._address({**self.NO_KEY, "hostname": "erp.example.com"}, "site.example.com")
        assert (a.url, a.source) == ("http://erp.example.com", "host_name")

    def test_an_ssl_certificate_makes_the_site_name_https(self):
        a = self._address({**self.NO_KEY, "ssl_certificate": "/c.pem"}, "site.example.com")
        assert a.url == "https://site.example.com"

    def test_a_localhost_host_name_opens_on_the_published_port_not_the_container_port(self):
        a = self._address(
            self.KEY,
            "dev.localhost",
            sites={"dev.localhost": {"host_name": "http://dev.localhost:8001"}},
        )
        assert a.url == "http://dev.localhost:21001"
        assert (a.source, a.internal_port) == ("host_name", False)

    def test_a_localhost_host_name_without_a_port_gets_the_webs_published_port(self):
        a = self._address(
            {**self.NO_KEY, "restart_supervisor_on_update": 1},
            "dev.localhost",
            sites={"dev.localhost": {"host_name": "http://dev.localhost"}},
        )
        assert a.url == "http://dev.localhost:21001"

    def test_host_name_without_a_scheme_gets_http_as_frappe_adds_it(self):
        a = self._address(
            self.NO_KEY,
            "site.example.com",
            sites={"site.example.com": {"host_name": "erp.example.com"}},
        )
        assert a.url == "http://erp.example.com"

    def test_the_sites_config_overrides_the_common_host_name(self):
        common = {**self.NO_KEY, "host_name": "https://bench.example.com"}
        shared = self._address(common, "one.example.com")
        own = self._address(
            common,
            "two.example.com",
            sites={"two.example.com": {"host_name": "https://two.example.com"}},
        )
        assert shared.url == "https://bench.example.com"
        assert own.url == "https://two.example.com"

    def test_an_unpublished_container_port_is_unknown_never_the_container_port(self):
        # The 7th bench binds :8006 inside but is published nowhere. Reporting 8006
        # as a host port would send the user to a port nothing listens on.
        a = self._address({"webserver_port": 8006, "socketio_port": 9006}, "one.localhost")
        assert a.url is None


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


# ------------------------------------------------------------- present_bench_paths


class _ProbeContainer:
    """A container that answers the one-exec bench-existence probe.

    It runs the REAL script through a tiny shell interpretation so the argv
    contract (paths as positionals, never interpolated into the script) is what is
    actually exercised.
    """

    def __init__(self, present=(), exit_code=0, raises=None):
        self.present = set(present)
        self.exit_code = exit_code
        self.raises = raises
        self.calls: list = []

    def exec_run(self, cmd):
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        assert cmd[:2] == ["sh", "-c"], "the probe must be argv-form, not a shell string"
        assert cmd[3] == "sh", "paths must ride as $@ positionals after the $0 placeholder"
        paths = cmd[4:]
        body = "\n".join(p for p in paths if p in self.present)
        return (self.exit_code, body.encode())


class TestPresentBenchPaths:
    """The shared cached-bench verification. Fail-honest in BOTH directions."""

    def test_it_reports_which_paths_still_exist(self):
        c = _ProbeContainer(present={"/w/b0"})
        assert resolvers.present_bench_paths(c, ["/w/b0", "/w/b1"]) == {"/w/b0"}

    def test_one_exec_regardless_of_bench_count(self):
        # The property that makes verifying affordable on every read: flat in bench
        # count, so a six-bench instance costs the same round trip as a one-bench one.
        c = _ProbeContainer(present={"/w/b0", "/w/b1", "/w/b2"})
        resolvers.present_bench_paths(c, ["/w/b0", "/w/b1", "/w/b2"])
        assert len(c.calls) == 1

    def test_a_path_is_never_interpolated_into_the_script(self):
        # A cached path is data, not code. The assertions in _ProbeContainer.exec_run
        # pin the argv shape; this drives a hostile value through it.
        hostile = '/w/b0"; rm -rf /; echo "'
        c = _ProbeContainer(present={hostile})
        assert resolvers.present_bench_paths(c, [hostile]) == {hostile}
        assert c.calls[0][4] == hostile

    def test_a_failed_exec_is_none_not_an_empty_set(self):
        # An empty set would render as "every cached bench is gone" - the same wrong
        # answer as the staleness this exists to remove, just louder.
        assert resolvers.present_bench_paths(_ProbeContainer(exit_code=1), ["/w/b0"]) is None

    def test_a_transport_failure_is_none(self):
        c = _ProbeContainer(raises=APIError("container is gone"))
        assert resolvers.present_bench_paths(c, ["/w/b0"]) is None

    def test_nothing_to_check_needs_no_exec(self):
        c = _ProbeContainer()
        assert resolvers.present_bench_paths(c, []) == set()
        assert c.calls == []


# ------------------------------------------------------- resolve_app_imports (BUG-10)


class _AppImportContainer:
    """A fake frappe container answering the app-import probe with canned records.

    ``records`` is the JSON list the in-container python would print; ``exit_code``,
    ``output`` and ``raises`` let a test drive the fail-soft branches.
    """

    def __init__(self, *, records=None, exit_code=0, output=None, raises=None):
        self._records = records if records is not None else []
        self._exit_code = exit_code
        self._output = output
        self._raises = raises
        self.calls: list = []

    def exec_run(self, cmd, workdir=None, **kwargs):
        self.calls.append(cmd)
        if self._raises is not None:
            raise self._raises
        if self._output is not None:
            return self._exit_code, self._output
        import json as _json

        return self._exit_code, _json.dumps(self._records).encode()


BENCH = "/workspace/frappe-bench"


def _rec(app, *, is_symlink=False, resolved=None, origin=None):
    return {
        "app": app,
        "entry": f"{BENCH}/apps/{app}",
        "is_symlink": is_symlink,
        "resolved_path": resolved,
        "imported_origin": origin,
    }


class TestResolveAppImports:
    def test_normal_real_dir_is_not_diverged(self):
        c = _AppImportContainer(
            records=[
                _rec(
                    "srcapp",
                    resolved=f"{BENCH}/apps/srcapp",
                    origin=f"{BENCH}/apps/srcapp/srcapp/__init__.py",
                )
            ]
        )
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.checked is True
        assert imp.diverged is False
        assert imp.is_symlink is False
        assert imp.resolved_path == f"{BENCH}/apps/srcapp"
        assert imp.imported_path == f"{BENCH}/apps/srcapp"

    def test_normal_symlink_layout_is_not_flagged_as_diverged(self):
        """apps/<app> is a symlink whose target IS what the bench imports: fine."""
        target = "/workspace/.hdsrc/srcapp"
        c = _AppImportContainer(
            records=[
                _rec(
                    "srcapp",
                    is_symlink=True,
                    resolved=target,
                    origin=f"{target}/srcapp/__init__.py",
                )
            ]
        )
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.is_symlink is True
        assert imp.resolved_path == target
        assert imp.diverged is False

    def test_real_dir_but_bench_imports_a_different_copy_is_diverged(self):
        """Orientation B: apps/srcapp is real, the bench imports .hdsrc/srcapp."""
        c = _AppImportContainer(
            records=[
                _rec(
                    "srcapp",
                    resolved=f"{BENCH}/apps/srcapp",
                    origin="/workspace/.hdsrc/srcapp/srcapp/__init__.py",
                )
            ]
        )
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.diverged is True
        assert imp.resolved_path == f"{BENCH}/apps/srcapp"
        assert imp.imported_path == "/workspace/.hdsrc/srcapp"

    def test_symlink_pointing_away_from_the_imported_copy_is_diverged(self):
        """Orientation A: apps/srcapp is a symlink, but the site imports elsewhere."""
        c = _AppImportContainer(
            records=[
                _rec(
                    "srcapp",
                    is_symlink=True,
                    resolved="/workspace/.hdsrc/srcapp",
                    origin=f"{BENCH}/apps/srcapp-real/srcapp/__init__.py",
                )
            ]
        )
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.is_symlink is True
        assert imp.diverged is True

    def test_one_exec_for_many_apps(self):
        c = _AppImportContainer(records=[_rec("a"), _rec("b"), _rec("c")])
        resolvers.resolve_app_imports(c, BENCH, ["a", "b", "c"])
        assert len(c.calls) == 1
        argv = c.calls[0]
        # The bench venv python, run with -c and the apps as positional args.
        assert argv[0] == f"{BENCH}/env/bin/python"
        assert argv[1] == "-c"
        assert argv[3] == BENCH
        assert argv[4:] == ["a", "b", "c"]

    def test_no_apps_needs_no_exec(self):
        c = _AppImportContainer()
        assert resolvers.resolve_app_imports(c, BENCH, []) == {}
        assert c.calls == []

    def test_a_probe_that_cannot_run_is_unchecked_never_a_crash(self):
        c = _AppImportContainer(exit_code=1, output=b"boom")
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.checked is False
        assert imp.diverged is False

    def test_a_transport_error_is_unchecked(self):
        c = _AppImportContainer(raises=RuntimeError("daemon gone"))
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.checked is False

    def test_unparseable_output_is_unchecked(self):
        c = _AppImportContainer(output=b"not json")
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.checked is False

    def test_an_app_whose_import_is_not_locatable_is_checked_but_not_diverged(self):
        c = _AppImportContainer(records=[_rec("srcapp", resolved=f"{BENCH}/apps/srcapp")])
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert imp.checked is True
        assert imp.imported_path is None
        assert imp.diverged is False

    def test_divergence_warnings_name_both_paths(self):
        diverged = resolvers.AppImport(
            app="srcapp",
            entry=f"{BENCH}/apps/srcapp",
            is_symlink=False,
            resolved_path=f"{BENCH}/apps/srcapp",
            imported_path="/workspace/.hdsrc/srcapp",
            diverged=True,
            checked=True,
        )
        clean = dataclasses.replace(diverged, diverged=False)
        msgs = resolvers.app_import_divergence_warnings([diverged, clean])
        assert len(msgs) == 1
        assert msgs[0].code == "app.wrong_copy"
        assert f"{BENCH}/apps/srcapp" in msgs[0].text
        assert "/workspace/.hdsrc/srcapp" in msgs[0].text

    def test_app_import_dto_is_plain_serializable_data(self):
        c = _AppImportContainer(records=[_rec("srcapp", resolved=f"{BENCH}/apps/srcapp")])
        imp = resolvers.resolve_app_imports(c, BENCH, ["srcapp"])["srcapp"]
        assert isinstance(dataclasses.asdict(imp), dict)
