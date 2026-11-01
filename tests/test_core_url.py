"""``core.url`` - the host URL a bench serves on, plus a fresh HTTP observation.

Covers the pattern single-bench verbs such as ``cwcli axi logs`` already establish
(the shared ``resolve_container_and_bench`` prologue: a stopped project is
``confirm_start``, an ambiguous multi-bench project is ``select_bench``) plus what
is unique to this verb: the URL is unresolvable without failing the call, the probe
is read fresh every time (never through ``core.status``'s cached/``--watch`` tiers),
and an explicit ``--bench``/``--site`` reaches exactly the bench/site named.
"""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import docker as core_docker
from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core import url as core_url
from caffeinated_whale_cli.core.envelope import Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from tests.test_core_supervision import FakeContainer

BENCH_0 = "/workspace/frappe-bench"
BENCH_1 = "/workspace/frappe-bench-1"


class UrlFakeContainer(FakeContainer):
    """``FakeContainer`` plus published port bindings and a fake site directory.

    Neither is on the shared fake (no other current caller of it needs a host port
    map or a ``test -d sites/<site>`` probe), so they are added here rather than
    widening a fixture every other core-supervision test also imports.
    """

    def __init__(self, *, ports=None, missing_sites=(), site_configs=None, **kwargs):
        super().__init__(**kwargs)
        self.ports = ports or {}
        self.missing_sites = set(missing_sites)
        self.site_configs = site_configs or {}

    def _exec_list(self, cmd, detach):
        if cmd[0] == "cat" and cmd[1].endswith("/site_config.json"):
            site = cmd[1].rsplit("/", 2)[-2]
            config = self.site_configs.get(site)
            return (1, b"") if config is None else (0, json.dumps(config).encode())
        if cmd[0] == "test" and cmd[1] == "-d":
            site_path = cmd[2]
            if any(site_path.endswith(f"/sites/{s}") for s in self.missing_sites):
                return (1, b"")
            return (0, b"")
        return super()._exec_list(cmd, detach)


def _default_ports():
    return {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}]}


@pytest.fixture
def wire(monkeypatch):
    def _wire(container, *, benches=None):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda _n: [container])
        monkeypatch.setattr(
            resolvers.db_utils,
            "get_cached_project_data",
            lambda _p: {"bench_instances": benches} if benches is not None else None,
        )

    return _wire


def _site_cache(monkeypatch, *, default=None, sites=()):
    monkeypatch.setattr(resolvers.db_utils, "get_default_site", lambda p, b=None: default)
    monkeypatch.setattr(
        resolvers.db_utils, "get_all_site_configs", lambda p, b=None: {s: {} for s in sites}
    )


class TestSuccess:
    def test_resolves_the_url_and_probes_it_fresh(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_code="200",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.status is Status.OK
        assert result.data.url == "http://one.localhost:21000"
        assert result.data.site == "one.localhost"
        assert result.data.http_code == "200"
        assert result.data.reachable is True
        config_reads = [
            cmd
            for cmd in c.calls
            if isinstance(cmd, list) and cmd == ["cat", f"{BENCH_0}/sites/common_site_config.json"]
        ]
        assert len(config_reads) == 1
        # The probe used the resolved site as the Host header, hitting the
        # CONTAINER port (curl runs inside the container) - not a guessed 8000.
        curl_calls = [cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl"]
        assert curl_calls
        assert curl_calls[0][-1] == "http://localhost:8000"
        assert "one.localhost" in "".join(str(a) for a in curl_calls[0])


class TestUnhealthyResponse:
    def test_a_non_200_code_is_reported_verbatim_and_still_reachable(self, monkeypatch, wire):
        # "Any code is serving" - the same rule `core.status` already follows.
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_code="500",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.http_code == "500"
        assert result.data.reachable is True

    def test_an_unreachable_port_is_reported_down_not_an_error(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_ok=False,
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.status is Status.OK
        assert result.data.url == "http://one.localhost:21000"
        assert result.data.http_code is None
        assert result.data.reachable is False


class TestStoppedOrMissingInstance:
    def test_a_stopped_project_needs_choice_to_confirm_start(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer()
        c.status = "exited"
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "confirm_start"

    def test_a_missing_project_raises_not_found(self, monkeypatch):
        monkeypatch.setattr(core_docker, "get_project_containers", lambda _n: [])

        with pytest.raises(CwcliError) as exc:
            core_url.probe_url("no-such-project")

        assert exc.value.kind is ErrorKind.NOT_FOUND


class TestUnreadableRoutingData:
    def test_an_unreadable_port_config_reports_unresolved_never_guesses_8000(
        self, monkeypatch, wire
    ):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(configs={BENCH_0: None}, ports=_default_ports())
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.status is Status.OK
        assert result.data.url is None
        assert result.data.http_code is None
        assert result.data.reachable is False
        assert any(w.code == "url.unresolved" for w in result.warnings)
        assert "common_site_config.json" in result.warnings[-1].text
        assert "Procfile" in result.warnings[-1].text
        # Never probed - there is no port to have probed.
        assert not any(isinstance(cmd, list) and cmd[0] == "curl" for cmd in c.calls)

    def test_a_container_port_with_no_published_binding_is_unresolved(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports={},  # nothing published
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.url is None
        assert result.data.reachable is False
        assert "has no live host binding" in result.warnings[-1].text
        assert "run 'cwcli axi scale proj'" in result.warnings[-1].text


class TestExplicitMultiBenchSelection:
    def test_bench_selector_reaches_exactly_the_named_bench(self, monkeypatch, wire):
        _site_cache(monkeypatch, default=None, sites=[])
        c = UrlFakeContainer(
            configs={
                BENCH_0: {"webserver_port": 8000, "socketio_port": 9000},
                BENCH_1: {"webserver_port": 8001, "socketio_port": 9001},
            },
            ports={
                "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21000"}],
                "8001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21001"}],
            },
            web_code={8000: "200", 8001: "404"},
        )
        wire(c, benches=[{"index": 0, "path": BENCH_0}, {"index": 1, "path": BENCH_1}])

        result = core_url.probe_url("proj", bench="1")

        assert result.status is Status.OK
        assert result.data.bench_path == BENCH_1
        assert result.data.url == "http://localhost:21001"
        assert result.data.http_code == "404"

    def test_explicit_site_is_used_verbatim_and_checked_for_existence(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="default.localhost", sites=["default.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_code="200",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj", site="other.localhost")

        assert result.data.site == "other.localhost"
        assert result.data.url == "http://other.localhost:21000"

    def test_an_unknown_explicit_site_raises_not_found(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="default.localhost", sites=["default.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            missing_sites={"ghost.localhost"},
        )
        wire(c, benches=[{"path": BENCH_0}])

        with pytest.raises(CwcliError) as exc:
            core_url.probe_url("proj", site="ghost.localhost")

        assert exc.value.kind is ErrorKind.NOT_FOUND


class TestAmbiguousSelection:
    def test_multiple_benches_with_no_selector_needs_choice(self, monkeypatch, wire):
        _site_cache(monkeypatch, default=None, sites=[])
        c = UrlFakeContainer(
            configs={
                BENCH_0: {"webserver_port": 8000, "socketio_port": 9000},
                BENCH_1: {"webserver_port": 8001, "socketio_port": 9001},
            }
        )
        wire(c, benches=[{"index": 0, "path": BENCH_0}, {"index": 1, "path": BENCH_1}])

        result = core_url.probe_url("proj")

        assert result.status is Status.NEEDS_CHOICE
        assert result.choice.kind == "select_bench"


class TestNoSiteAtAll:
    def test_a_bench_with_no_site_probes_host_less_never_guessing_one(self, monkeypatch, wire):
        _site_cache(monkeypatch, default=None, sites=[])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_code="404",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.site is None
        assert result.data.url == "http://localhost:21000"
        assert any(w.code == "url.no_site" for w in result.warnings)


class TestWithoutWebserverPort:
    """A bench whose common_site_config.json omits ``webserver_port``.

    Removing the key is how an administrator stops Frappe appending the internal
    port to the site URLs it builds behind a reverse proxy, and such a bench runs
    fully without it, so Open site must too: the port its web process binds comes
    from the Procfile's web line.
    """

    def test_a_localhost_site_resolves_and_is_probed_on_the_procfile_port(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"socketio_port": 9000}}, ports=_default_ports(), web_code="200"
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.url == "http://one.localhost:21000"
        assert result.data.reachable is True
        assert result.data.internal_port is False
        curl_calls = [cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl"]
        assert curl_calls[0][-1] == "http://localhost:8000"
        assert not any(w.code == "url.unresolved" for w in result.warnings)

    def test_a_hostname_site_opens_by_name_without_the_internal_port(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="site.example.com", sites=["site.example.com"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"socketio_port": 9000}}, ports=_default_ports(), web_code="200"
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        # Frappe's own URLs for this site carry no port once the key is gone.
        assert result.data.url == "http://site.example.com"
        assert result.data.reachable is True
        assert result.data.internal_port is False
        assert result.data.fix == []
        assert not any(w.code == "url.internal_port" for w in result.warnings)


class TestInternalPort:
    """``webserver_port`` set on a site whose name is not a localhost one.

    The address keeps the port - it is what Frappe reports - and is flagged with the
    steps that remove the key, because behind a reverse proxy the port is internal.
    """

    def test_the_port_is_kept_and_the_fix_names_bench_and_frappe_commands(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="site.example.com", sites=["site.example.com"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_code="200",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.url == "http://site.example.com:21000"
        assert result.data.internal_port is True
        assert result.data.webserver_port_in == ["sites/common_site_config.json"]
        assert [s.command for s in result.data.fix] == [
            "cwcli run proj config remove-common-config webserver_port",
            "cwcli stop proj --bench 0",
            "cwcli start proj --bench 0",
            "cwcli run proj --site site.example.com set-config host_name https://site.example.com",
        ]
        warning = next(w for w in result.warnings if w.code == "url.internal_port")
        assert "http://site.example.com:21000" in warning.text
        assert warning.detail["fix"][0]["command"] == result.data.fix[0].command

    def test_a_key_in_the_sites_own_config_is_removed_with_frappes_set_config(
        self, monkeypatch, wire
    ):
        _site_cache(monkeypatch, default="site.example.com", sites=["site.example.com"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"socketio_port": 9000}},
            site_configs={"site.example.com": {"webserver_port": 8000}},
            ports=_default_ports(),
            web_code="200",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.url == "http://site.example.com:21000"
        assert result.data.webserver_port_in == ["sites/site.example.com/site_config.json"]
        commands = [s.command for s in result.data.fix]
        assert (
            commands[0] == "cwcli run proj --site site.example.com set-config webserver_port None"
        )
        assert not any("remove-common-config" in command for command in commands)

    def test_a_localhost_site_keeps_its_port_with_no_warning(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="one.localhost", sites=["one.localhost"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            ports=_default_ports(),
            web_code="200",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.url == "http://one.localhost:21000"
        assert result.data.internal_port is False
        assert result.data.fix == []
        assert not any(w.code == "url.internal_port" for w in result.warnings)

    def test_host_name_is_the_address_and_carries_no_warning(self, monkeypatch, wire):
        _site_cache(monkeypatch, default="site.example.com", sites=["site.example.com"])
        c = UrlFakeContainer(
            configs={BENCH_0: {"webserver_port": 8000, "socketio_port": 9000}},
            site_configs={"site.example.com": {"host_name": "https://site.example.com"}},
            ports=_default_ports(),
            web_code="200",
        )
        wire(c, benches=[{"path": BENCH_0}])

        result = core_url.probe_url("proj")

        assert result.data.url == "https://site.example.com"
        assert result.data.source == "host_name"
        assert result.data.internal_port is False
        # The bench is still probed on its own port, with the site as the Host.
        curl_calls = [cmd for cmd in c.calls if isinstance(cmd, list) and cmd[0] == "curl"]
        assert curl_calls[0][-1] == "http://localhost:8000"
