"""Publishing a site (core.publish) through the Cloudflare and Nginx Proxy Manager
integrations, against local fakes of both APIs over real HTTP: the step order, Retry
repeating only what is missing, cwcli changing and deleting only what it wrote, the
read-only drift check, and the preflight New site runs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import cloudflare, integrations, npm, publish, webapi
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

from .publish_fakes import CF_TOKEN, NPM_EMAIL, NPM_PASSWORD, FakeCloudflare, FakeNpm

SITE = "erp.example.com"
BENCH = "/workspace/frappe-bench"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    cf, proxy = FakeCloudflare(), FakeNpm()
    monkeypatch.setattr(cloudflare, "API", cf.api)
    monkeypatch.setattr(cloudflare, "TRACE", cf.trace)
    monkeypatch.setattr(npm, "local_container", lambda: None)
    ports = {"value": (8001, 9001)}
    monkeypatch.setattr(
        publish.resolvers, "resolve_container_and_bench", lambda p, b, path: (object(), BENCH, [])
    )
    monkeypatch.setattr(publish.resolvers, "require_site_dir", lambda c, b, s: f"{b}/sites/{s}")
    monkeypatch.setattr(publish.resolvers, "resolve_host_ports", lambda c, b: ports["value"])
    pings: list[str] = []
    answer = {"status": 200}
    real = webapi.call

    def call(method, url, **kwargs):
        if url.endswith("/api/method/ping"):
            pings.append(url)
            return webapi.Response(answer["status"], {"message": "pong"}, "")
        return real(method, url, **kwargs)

    monkeypatch.setattr(webapi, "call", call)
    yield SimpleNamespace(cf=cf, npm=proxy, ports=ports, pings=pings, answer=answer)
    cf.close()
    proxy.close()


def turn_on(world, *, dns=True, proxy=True, challenge=npm.HTTP_CHALLENGE, proxied_default=False):
    if dns:
        check, view = integrations.configure(
            "cloudflare",
            {"ipv4": "203.0.113.7", "proxied_default": proxied_default},
            {"api_token": CF_TOKEN},
            enable=True,
        )
        assert view["enabled"], check
    if proxy:
        secrets = {"password": NPM_PASSWORD}
        if challenge == npm.DNS_CHALLENGE:
            secrets["dns_token"] = "npm-dns-challenge-token"
        check, view = integrations.configure(
            "npm",
            {
                "url": world.npm.url,
                "email": NPM_EMAIL,
                "forward_host": "172.17.0.1",
                "challenge": challenge,
            },
            secrets,
            enable=True,
        )
        assert view["enabled"], check


def set_ipv6(address: str) -> None:
    integrations.configure(
        "cloudflare", {"ipv4": "203.0.113.7", "ipv6": address}, None, enable=True
    )


def states(report) -> dict[str, str]:
    return {k: v["state"] for k, v in report.parts.items()}


class TestPublish:
    def test_it_creates_the_record_the_proxy_host_and_the_certificate_then_checks(self, world):
        turn_on(world)
        report = publish.publish_site("p", SITE).data
        assert report.ok and states(report) == dict.fromkeys(publish.PARTS, "done")
        [record] = world.cf.records.values()
        assert (record["type"], record["name"], record["content"], record["proxied"]) == (
            "A",
            SITE,
            "203.0.113.7",
            False,
        )
        assert record["comment"].startswith("cwcli ") and SITE in record["comment"]
        assert len(record["comment"]) <= 100
        [host] = world.npm.hosts.values()
        assert (host["forward_host"], host["forward_port"]) == ("172.17.0.1", 8001)
        assert host["locations"] == [
            {
                "path": "/socket.io",
                "forward_scheme": "http",
                "forward_host": "172.17.0.1",
                "forward_port": 9001,
            }
        ]
        [cert] = world.npm.certs.values()
        assert cert["meta"] == {"dns_challenge": False}
        assert host["certificate_id"] == cert["id"] and host["ssl_forced"] is True
        assert world.pings == [f"https://{SITE}/api/method/ping"]
        assert report.url == f"https://{SITE}"
        assert publish.entry(SITE)["parts"]["proxy"]["id"] == host["id"]

    def test_steps_run_in_order_dns_proxy_certificate_check(self, world):
        turn_on(world)
        events = []
        publish.publish_site("p", SITE, on_event=events.append)
        started = [e.phase for e in events if isinstance(e, publish.PublishStepStart)]
        assert started == ["dns", "proxy", "certificate", "verify"]

    def test_retry_repeats_only_what_is_missing(self, world):
        turn_on(world)
        world.npm.cert_error = "Some challenges have failed."
        first = publish.publish_site("p", SITE).data
        assert not first.ok
        assert states(first) == {
            "dns": "done",
            "proxy": "done",
            "certificate": "failed",
            "verify": "waiting",
        }
        assert "port 80 open" in first.parts["certificate"]["message"]
        cf_writes, npm_writes = len(world.cf.writes()), len(world.npm.writes())
        world.npm.cert_error = None
        second = publish.publish_site("p", SITE).data
        assert second.ok
        assert len(world.cf.writes()) == cf_writes  # the record was already there
        new_npm = world.npm.writes()[npm_writes:]
        assert [m for m, _p in new_npm] == ["POST", "PUT"]  # the certificate, then attaching it
        assert len(world.npm.hosts) == 1 and len(world.cf.records) == 1

    def test_a_published_site_published_again_writes_nothing(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        before = (len(world.cf.writes()), len(world.npm.writes()))
        assert publish.publish_site("p", SITE).data.ok
        assert (len(world.cf.writes()), len(world.npm.writes())) == before

    def test_a_record_cwcli_did_not_create_stops_the_publish_before_the_proxy(self, world):
        turn_on(world)
        world.cf.add("example.com", "A", SITE, "198.51.100.1")
        report = publish.publish_site("p", SITE).data
        assert not report.ok and report.parts["dns"]["state"] == "failed"
        assert "did not create" in report.parts["dns"]["message"]
        assert report.parts["proxy"]["state"] == "waiting"
        assert world.npm.hosts == {} and len(world.cf.records) == 1

    def test_a_record_already_pointing_here_is_used_but_never_owned(self, world):
        turn_on(world)
        rid = world.cf.add("example.com", "A", SITE, "203.0.113.7")
        report = publish.publish_site("p", SITE).data
        assert report.ok and report.parts["dns"]["records"][0]["owned"] is False
        assert publish.unpublish_site(SITE).data.ok
        assert rid in world.cf.records  # Unpublish leaves what cwcli did not create

    def test_a_record_changed_by_hand_is_never_overwritten(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [record] = world.cf.records.values()
        record["content"] = "198.51.100.9"
        report = publish.publish_site("p", SITE).data
        assert report.parts["dns"]["state"] == "failed"
        assert "changed outside cwcli" in report.parts["dns"]["message"]
        assert record["content"] == "198.51.100.9"

    def test_a_new_address_moves_the_record_cwcli_wrote(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        report = publish.publish_site("p", SITE, address="203.0.113.50").data
        assert report.ok
        [record] = world.cf.records.values()
        assert record["content"] == "203.0.113.50"

    def test_publishing_again_keeps_the_sites_own_choices(self, world):
        turn_on(world, challenge=npm.DNS_CHALLENGE)
        world.npm.cert_error = "Some challenges have failed."
        first = publish.publish_site(
            "p", SITE, address="203.0.113.50", proxied=True, access_list_id=3
        ).data
        assert first.parts["certificate"]["state"] == "failed"
        world.npm.cert_error = None
        assert publish.publish_site("p", SITE).data.ok  # Retry sends no choice
        [record] = world.cf.records.values()
        assert (record["content"], record["proxied"]) == ("203.0.113.50", True)
        [host] = world.npm.hosts.values()
        assert host["access_list_id"] == 3

    def test_a_proxy_host_changed_by_hand_is_never_overwritten(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [host] = world.npm.hosts.values()
        host["forward_port"] = 9999
        world.ports["value"] = (8003, 9003)
        report = publish.publish_site("p", SITE).data
        assert report.parts["proxy"]["state"] == "failed"
        assert "changed outside cwcli" in report.parts["proxy"]["message"]
        assert (
            "forwards to 172.17.0.1:9999, not 172.17.0.1:8001" in report.parts["proxy"]["message"]
        )
        assert host["forward_port"] == 9999

    def test_an_explicit_empty_choice_clears_the_stored_one(self, world):
        turn_on(world)
        publish.publish_site("p", SITE, address="203.0.113.50", access_list_id=3)
        assert publish.publish_site("p", SITE, address="", access_list_id=0).data.ok
        [record] = world.cf.records.values()
        [host] = world.npm.hosts.values()
        assert (record["content"], host["access_list_id"]) == ("203.0.113.7", 0)

    def test_a_hand_added_access_list_is_named_in_the_conflict(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [host] = world.npm.hosts.values()
        host["access_list_id"] = 3
        world.ports["value"] = (8003, 9003)
        message = publish.publish_site("p", SITE).data.parts["proxy"]["message"]
        assert "its access list is 3, not none" in message and "forwards to" not in message
        assert host["access_list_id"] == 3 and host["forward_port"] == 8001

    def test_a_record_written_before_a_failed_write_stays_cwclis(self, world, monkeypatch):
        turn_on(world)
        set_ipv6("2001:db8::7")
        real = cloudflare.Cloudflare.create

        def create(self, zone_id, record):
            if record["type"] == "AAAA":
                raise CwcliError(ErrorKind.DOCKER, "cf.down", "Cloudflare went away.")
            return real(self, zone_id, record)

        monkeypatch.setattr(cloudflare.Cloudflare, "create", create)
        report = publish.publish_site("p", SITE).data
        assert report.parts["dns"]["state"] == "failed"
        [a] = publish.entry(SITE)["parts"]["dns"]["records"]
        assert (a["type"], a["owned"]) == ("A", True)
        monkeypatch.setattr(cloudflare.Cloudflare, "create", real)
        assert publish.publish_site("p", SITE).data.ok
        assert publish.unpublish_site(SITE).data.ok
        assert world.cf.records == {}

    def test_a_record_no_longer_wanted_is_removed_while_unchanged(self, world):
        turn_on(world)
        set_ipv6("2001:db8::7")
        publish.publish_site("p", SITE)
        assert sorted(r["type"] for r in world.cf.records.values()) == ["A", "AAAA"]
        set_ipv6("")
        assert publish.publish_site("p", SITE).data.ok
        assert [r["type"] for r in world.cf.records.values()] == ["A"]
        assert [r["type"] for r in publish.entry(SITE)["parts"]["dns"]["records"]] == ["A"]

    def test_a_failed_dns_step_leaves_the_proxys_last_outcome(self, world):
        turn_on(world)
        integrations.record_outcome("npm", "The proxy was down.")
        world.cf.add("example.com", "A", SITE, "198.51.100.1")
        assert publish.publish_site("p", SITE).data.parts["proxy"]["state"] == "waiting"
        assert integrations.view("npm")[0]["state"] == "error"

    def test_a_domain_held_by_another_npm_user_is_refused(self, world):
        turn_on(world)
        world.npm.add_host(SITE, owner=1)
        report = publish.publish_site("p", SITE).data
        assert report.parts["proxy"]["state"] == "failed"
        assert "already in use" in report.parts["proxy"]["message"]

    def test_a_moved_bench_repoints_the_same_proxy_host(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        world.ports["value"] = (8003, 9003)
        report = publish.publish_site("p", SITE).data
        assert report.ok
        [host] = world.npm.hosts.values()
        assert host["forward_port"] == 8003 and host["locations"][0]["forward_port"] == 9003
        assert len(world.cf.records) == 1

    def test_proxied_is_refused_with_the_http_challenge_and_allowed_with_dns(self, world):
        turn_on(world)
        result = publish.publish_site("p", SITE, proxied=True)
        assert [w.code for w in result.warnings] == ["publish.proxied_refused"]
        assert next(iter(world.cf.records.values()))["proxied"] is False

    def test_proxied_with_the_dns_challenge_and_its_own_token(self, world):
        turn_on(world, challenge=npm.DNS_CHALLENGE)
        assert publish.publish_site("p", SITE, proxied=True).data.ok
        assert next(iter(world.cf.records.values()))["proxied"] is True
        [cert] = world.npm.certs.values()
        assert cert["meta"]["dns_challenge"] is True
        assert cert["meta"]["dns_provider_credentials"].endswith("npm-dns-challenge-token")
        assert CF_TOKEN not in json.dumps(cert)  # cwcli's own token never reaches NPM

    def test_dns_alone_publishes_at_the_bench_port(self, world):
        turn_on(world, proxy=False)
        report = publish.publish_site("p", SITE).data
        assert report.ok and report.url == f"http://{SITE}:8001"
        assert states(report) == {
            "dns": "done",
            "proxy": "off",
            "certificate": "off",
            "verify": "done",
        }

    def test_the_proxy_alone_publishes_without_a_record(self, world):
        turn_on(world, dns=False)
        report = publish.publish_site("p", SITE).data
        assert report.ok and report.parts["dns"]["state"] == "off"
        assert world.cf.calls == []

    def test_a_site_that_does_not_answer_fails_the_publish(self, world):
        turn_on(world)
        world.answer["status"] = 502
        report = publish.publish_site("p", SITE).data
        assert not report.ok and report.parts["verify"]["state"] == "failed"
        assert integrations.view("npm")[0]["state"] == "on"  # NPM's own steps passed

    def test_a_refused_token_turns_the_dns_integration_to_error(self, world):
        turn_on(world)
        world.cf.deny_writes = True
        report = publish.publish_site("p", SITE).data
        assert "needs the Zone > DNS > Edit permission" in report.parts["dns"]["message"]
        assert integrations.view("cloudflare")[0]["state"] == "error"

    @pytest.mark.parametrize("name", ["shop.localhost", "intranet", "Erp.example.com"])
    def test_a_name_that_is_not_public_is_refused(self, world, name):
        turn_on(world)
        with pytest.raises(CwcliError) as e:
            publish.publish_site("p", name)
        assert e.value.code == "publish.name"

    def test_nothing_to_publish_with_both_integrations_off(self, world):
        with pytest.raises(CwcliError) as e:
            publish.publish_site("p", SITE)
        assert e.value.code == "publish.no_integration"

    def test_no_secret_reaches_the_store_or_the_report(self, world):
        turn_on(world, challenge=npm.DNS_CHALLENGE)
        report = publish.publish_site("p", SITE).data
        stored = publish.store_path().read_text()
        for secret in (CF_TOKEN, NPM_PASSWORD, "npm-dns-challenge-token", "npm-session-token"):
            assert secret not in stored and secret not in json.dumps(report.parts)
        assert publish.store_path().stat().st_mode & 0o777 == 0o600


class TestUnpublish:
    def test_it_removes_the_host_then_the_certificate_then_the_record(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        result = publish.unpublish_site(SITE)
        assert result.data.ok
        assert result.data.removed == ["Proxy host", "Certificate", "DNS record"]
        assert world.npm.hosts == {} and world.npm.certs == {} and world.cf.records == {}
        assert publish.entry(SITE) is None

    def test_what_was_changed_by_hand_is_left_and_reported(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [record] = world.cf.records.values()
        record["content"] = "198.51.100.9"
        result = publish.unpublish_site(SITE)
        assert result.data.ok and world.cf.records
        assert "now points at 198.51.100.9" in result.data.left[0]
        assert [w.code for w in result.warnings] == ["publish.left"]
        assert world.npm.hosts == {}

    def test_a_hand_added_access_list_keeps_the_proxy_host(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [host] = world.npm.hosts.values()
        host["access_list_id"] = 3
        data = publish.unpublish_site(SITE).data
        assert world.npm.hosts and world.npm.certs and "left as it is" in data.left[0]

    def test_a_proxy_host_edited_by_hand_keeps_its_certificate(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [host] = world.npm.hosts.values()
        host["forward_port"] = 9999
        data = publish.unpublish_site(SITE).data
        assert data.ok and world.npm.hosts and world.npm.certs
        assert len(data.left) == 2 and world.cf.records == {}

    def test_an_integration_turned_off_keeps_its_part_for_the_retry(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        integrations.disable("cloudflare")
        first = publish.unpublish_site(SITE).data
        assert not first.ok and "Turn Cloudflare DNS back on" in first.failed[0]
        assert world.npm.hosts == {} and len(world.cf.records) == 1
        assert set(publish.entry(SITE)["parts"]) == {"dns", "verify"}
        integrations.configure("cloudflare", None, None, enable=True)
        assert publish.unpublish_site(SITE).data.ok
        assert world.cf.records == {} and publish.entry(SITE) is None

    def test_turning_an_integration_off_deletes_nothing(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        integrations.disable("cloudflare")
        integrations.disable("npm")
        assert world.cf.records and world.npm.hosts and world.npm.certs

    def test_an_unpublished_name_is_a_no_op(self, world):
        assert publish.unpublish_site("other.example.com").data.ok


class TestCheck:
    def test_no_drift_right_after_publishing(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        result = publish.check_site(SITE)
        assert result["ok"] and {p["state"] for p in result["parts"].values()} == {"ok"}

    def test_a_hand_added_access_list_is_drift(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        next(iter(world.npm.hosts.values()))["access_list_id"] = 3
        result = publish.check_site(SITE)
        assert result["parts"]["proxy"]["state"] == "drift"
        assert "its access list is 3, not none" in result["parts"]["proxy"]["message"]

    def test_it_reports_drift_and_repairs_nothing(self, world):
        turn_on(world)
        publish.publish_site("p", SITE)
        [record] = world.cf.records.values()
        record["content"] = "198.51.100.9"
        [cert] = world.npm.certs.values()
        soon = datetime.now(timezone.utc) + timedelta(days=10)
        cert["expires_on"] = soon.strftime("%Y-%m-%d %H:%M:%S")
        world.npm.hosts.clear()
        writes = (len(world.cf.writes()), len(world.npm.writes()))
        result = publish.check_site(SITE)
        assert not result["ok"]
        assert "points at 198.51.100.9" in result["parts"]["dns"]["message"]
        assert result["parts"]["proxy"]["message"] == "The proxy host was deleted."
        assert "renewal is failing" in result["parts"]["certificate"]["message"]
        assert (len(world.cf.writes()), len(world.npm.writes())) == writes


class TestPreflight:
    def test_a_free_name_passes_every_row(self, world, monkeypatch):
        turn_on(world)
        container = SimpleNamespace(status="running")
        monkeypatch.setattr(publish.core_docker, "get_frappe_container", lambda p: container)
        monkeypatch.setattr(publish.resolvers, "resolve_bench", lambda p, b, path: None)
        rows = publish.preflight("p", None, SITE)
        assert [(r["part"], r["state"]) for r in rows] == [
            ("port", "ok"),
            ("dns", "ok"),
            ("proxy", "ok"),
        ]
        assert world.cf.writes() == [] and world.npm.writes() == []

    def test_a_taken_name_and_a_stopped_instance(self, world, monkeypatch):
        turn_on(world)
        world.cf.add("example.com", "A", SITE, "198.51.100.1")
        world.npm.me["permissions"]["visibility"] = "all"
        world.npm.add_host(SITE, owner=1)
        container = SimpleNamespace(status="exited")
        monkeypatch.setattr(publish.core_docker, "get_frappe_container", lambda p: container)
        rows = {r["part"]: r["state"] for r in publish.preflight("p", None, SITE)}
        assert rows == {"port": "warn", "dns": "fail", "proxy": "fail"}

    @pytest.mark.parametrize(
        "rtype, content, proxied",
        [("AAAA", "2001:db8::99", False), ("A", "203.0.113.7", True)],
    )
    def test_it_refuses_what_the_dns_step_would_refuse(
        self, world, monkeypatch, rtype, content, proxied
    ):
        turn_on(world, proxy=False)
        set_ipv6("2001:db8::7")
        world.cf.add("example.com", rtype, SITE, content, proxied=proxied)
        monkeypatch.setattr(
            publish.core_docker, "get_frappe_container", lambda p: SimpleNamespace(status="exited")
        )
        rows = {r["part"]: r for r in publish.preflight("p", None, SITE)}
        assert rows["dns"]["state"] == "fail" and "did not create" in rows["dns"]["text"]
        assert not publish.publish_site("p", SITE).data.ok  # the step agrees

    @pytest.mark.parametrize("address", ["203.0.113.300", "2001:db8::1", "erp"])
    def test_an_override_that_is_not_ipv4_is_refused_before_anything_is_written(
        self, world, monkeypatch, address
    ):
        turn_on(world)
        with pytest.raises(CwcliError) as published:
            publish.publish_site("p", SITE, address=address)
        monkeypatch.setattr(
            publish.core_docker, "get_frappe_container", lambda p: SimpleNamespace(status="exited")
        )
        with pytest.raises(CwcliError) as checked:
            publish.preflight("p", None, SITE, address=address)
        for e in (published, checked):
            assert (e.value.kind, e.value.code) == (ErrorKind.USAGE, "publish.address")
        assert world.cf.writes() == [] and world.npm.writes() == []

    def test_a_name_outside_every_zone_fails(self, world, monkeypatch):
        turn_on(world, proxy=False)
        monkeypatch.setattr(
            publish.core_docker, "get_frappe_container", lambda p: SimpleNamespace(status="exited")
        )
        rows = {r["part"]: r for r in publish.preflight("p", None, "erp.example.net")}
        assert rows["dns"]["state"] == "fail" and "No zone" in rows["dns"]["text"]

    def test_a_localhost_name_fails_at_once(self, world):
        turn_on(world)
        assert [r["state"] for r in publish.preflight("p", None, "shop.localhost")] == ["fail"]


def test_the_options_new_site_offers(world):
    turn_on(world, challenge=npm.DNS_CHALLENGE)
    options = publish.options()
    assert options["dns"]["zones"] == ["example.com", "example.org"]
    assert options["dns"]["ipv4"] == "203.0.113.7"
    assert options["proxy"]["challenge"] == "dns"
    assert options["proxy"]["access_lists"] == [{"id": 3, "name": "Office only"}]
    assert options["proxied_allowed"] is True
    assert CF_TOKEN not in json.dumps(options)


def test_the_view_names_the_integrations_without_calling_them(world):
    turn_on(world)
    publish.publish_site("p", SITE)
    calls = (len(world.cf.calls), len(world.npm.calls))
    view = publish.view()
    assert view["dns"] == {"id": "cloudflare", "name": "Cloudflare DNS"}
    assert view["proxy"] == {"id": "npm", "name": "Nginx Proxy Manager"}
    assert SITE in view["sites"]
    assert (len(world.cf.calls), len(world.npm.calls)) == calls


def test_publishing_with_an_integration_off_keeps_what_it_made_removable(world):
    turn_on(world)
    publish.publish_site("p", SITE)
    integrations.disable("cloudflare")
    report = publish.publish_site("p", SITE).data
    assert report.ok and report.parts["dns"]["state"] == "off"
    assert report.parts["dns"]["records"][0]["owned"] is True  # still named, so removable
    integrations.configure("cloudflare", None, None, enable=True)
    assert publish.unpublish_site(SITE).data.ok
    assert world.cf.records == {}
