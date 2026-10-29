"""The Cloudflare DNS and Nginx Proxy Manager integrations' own contract: connection
tests that say what to fix, Detect, Check edit access, and secrets kept out of every
message. Both run against local fakes of their APIs (tests/publish_fakes.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from caffeinated_whale_cli.core import cloudflare, integrations, npm
from caffeinated_whale_cli.core.errors import CwcliError

from .publish_fakes import CF_TOKEN, NPM_EMAIL, NPM_PASSWORD, FakeCloudflare, FakeNpm


@pytest.fixture
def cf(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    fake = FakeCloudflare()
    monkeypatch.setattr(cloudflare, "API", fake.api)
    monkeypatch.setattr(cloudflare, "TRACE", fake.trace)
    yield fake
    fake.close()


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(npm, "local_container", lambda: None)
    fake = FakeNpm()
    yield fake
    fake.close()


def _npm_settings(fake, **extra):
    return {"url": fake.url, "email": NPM_EMAIL, "forward_host": "172.17.0.1", **extra}


class TestCloudflare:
    def test_the_test_names_the_zones_the_token_can_see(self, cf):
        check = integrations.test("cloudflare", {"ipv4": "203.0.113.7"}, {"api_token": CF_TOKEN})
        assert check.ok
        assert check.message == "Token is active. It can see 2 zones: example.com, example.org."
        assert cf.writes() == []  # the test is read-only

    def test_a_wrong_token_fails_with_cloudflares_reason(self, cf):
        check = integrations.test("cloudflare", {}, {"api_token": "wrong"})
        assert not check.ok and "Invalid API Token" in check.message
        assert "wrong" not in check.message

    def test_an_expired_token_or_no_zone(self, cf):
        cf.status = "expired"
        assert "expired" in integrations.test("cloudflare", {}, {"api_token": CF_TOKEN}).message
        cf.status, cf.zones = "active", {}
        check = integrations.test("cloudflare", {}, {"api_token": CF_TOKEN})
        assert not check.ok and "can see no zone" in check.message

    def test_turning_it_on_needs_the_address(self, cf):
        with pytest.raises(CwcliError) as e:
            integrations.configure("cloudflare", {}, {"api_token": CF_TOKEN}, enable=True)
        assert "public IPv4 address" in e.value.message

    @pytest.mark.parametrize(
        ("settings", "code"),
        [({"ipv4": "999.1.1.1"}, "cloudflare.ipv4"), ({"ipv6": "nope"}, "cloudflare.ipv6")],
    )
    def test_an_address_that_is_not_one_is_refused(self, cf, settings, code):
        with pytest.raises(CwcliError) as e:
            integrations.configure("cloudflare", settings, {"api_token": CF_TOKEN})
        assert e.value.code == code

    def test_detect_reads_cloudflares_trace_and_saves_nothing(self, cf):
        assert integrations.run_action("cloudflare", "detect_ip") == {"ipv4": "203.0.113.7"}
        assert integrations.view("cloudflare")[0]["settings"] == {}

    def test_check_edit_access_creates_and_deletes_a_txt_record_in_each_zone(self, cf):
        out = integrations.run_action(
            "cloudflare", "check_edit_access", {}, None, {"api_token": CF_TOKEN}
        )
        assert [(z["zone"], z["ok"]) for z in out["zones"]] == [
            ("example.com", True),
            ("example.org", True),
        ]
        assert [m for m, _p in cf.writes()] == ["POST", "DELETE", "POST", "DELETE"]
        assert cf.records == {}
        posted = [b for m, _p, b in cf.calls if m == "POST"]
        assert posted[0]["name"] == "_cwcli-check.example.com" and posted[0]["type"] == "TXT"

    def test_check_edit_access_says_which_permission_is_missing(self, cf):
        cf.deny_writes = True
        out = integrations.run_action(
            "cloudflare",
            "check_edit_access",
            {"zone": "example.org"},
            None,
            {"api_token": CF_TOKEN},
        )
        [zone] = out["zones"]
        assert not zone["ok"]
        assert zone["message"] == (
            "The Cloudflare token cannot edit example.org: it needs the Zone > DNS > Edit "
            "permission on that zone."
        )


class TestNginxProxyManager:
    def test_the_test_signs_in_and_checks_the_users_permissions(self, proxy):
        check = integrations.test("npm", _npm_settings(proxy), {"password": NPM_PASSWORD})
        assert check.ok, check.message
        assert "Nginx Proxy Manager 2.16.0 answers" in check.message
        assert "only the proxy hosts and certificates it created" in check.message
        assert "reachability check was skipped" in check.message

    def test_a_user_with_two_factor_sign_in_is_refused(self, proxy):
        proxy.requires_2fa = True
        check = integrations.test("npm", _npm_settings(proxy), {"password": NPM_PASSWORD})
        assert not check.ok and "two-factor" in check.message

    def test_a_wrong_password_is_refused_without_echoing_it(self, proxy):
        check = integrations.test("npm", _npm_settings(proxy), {"password": "nope-secret"})
        assert not check.ok and "refused the email and password" in check.message
        assert "nope-secret" not in check.message

    def test_a_user_that_cannot_manage_certificates_is_refused(self, proxy):
        proxy.me["permissions"]["certificates"] = "view"
        check = integrations.test("npm", _npm_settings(proxy), {"password": NPM_PASSWORD})
        assert not check.ok
        assert check.message.endswith("cwcli needs Manage for both.")

    def test_a_user_that_sees_every_host_passes_with_a_warning(self, proxy):
        proxy.me["permissions"]["visibility"] = "all"
        check = integrations.test("npm", _npm_settings(proxy), {"password": NPM_PASSWORD})
        assert check.ok and "sees every proxy host" in check.message

    def test_the_dns_challenge_needs_its_own_token(self, proxy):
        settings = _npm_settings(proxy, challenge=npm.DNS_CHALLENGE)
        check = integrations.test("npm", settings, {"password": NPM_PASSWORD})
        assert not check.ok and "DNS challenge" in check.message

    def test_a_changed_admin_url_needs_the_password_typed_again(self, proxy):
        integrations.configure("npm", _npm_settings(proxy), {"password": NPM_PASSWORD}, enable=True)
        with pytest.raises(CwcliError) as e:
            integrations.configure("npm", {"url": "http://127.0.0.1:9"}, {})
        assert e.value.code == "integration.secret_retype"

    def test_an_error_never_carries_a_secret(self, proxy):
        client = npm.ProxyManager(
            _npm_settings(proxy, challenge=npm.DNS_CHALLENGE),
            {"password": NPM_PASSWORD, "dns_token": "dns-secret"},
        )
        proxy.cert_error = "certbot failed with dns_cloudflare_api_token=dns-secret"
        with pytest.raises(CwcliError) as e:
            client.request_certificate("erp.example.com")
        assert "dns-secret" not in e.value.message and "[redacted]" in e.value.message

    @pytest.mark.parametrize(
        ("settings", "code"),
        [
            ({"url": "ftp://x"}, "npm.url"),
            ({"email": "no-at"}, "npm.email"),
            ({"forward_host": "http://172.17.0.1:80"}, "npm.forward_host"),
            ({"challenge": "carrier pigeon"}, "npm.challenge"),
        ],
    )
    def test_bad_settings_are_refused_before_saving(self, proxy, settings, code):
        with pytest.raises(CwcliError) as e:
            integrations.configure("npm", settings, {})
        assert e.value.code == code

    def test_the_admin_url_drops_a_trailing_api(self):
        assert npm._normalize({"url": "http://127.0.0.1:81/api/"})["url"] == "http://127.0.0.1:81"


class TestLocalContainer:
    def _container(self, monkeypatch, proxy, *, exec_out=b"404 0"):
        port = int(proxy.url.rsplit(":", 1)[1])
        container = SimpleNamespace(
            name="npm-app-1",
            ports={"81/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]},
            attrs={"NetworkSettings": {"Networks": {"npm_default": {"Gateway": "172.18.0.1"}}}},
            exec_run=lambda cmd: (0, exec_out),
        )
        monkeypatch.setattr(npm, "local_container", lambda: container)
        monkeypatch.setattr(npm, "_a_bench_port", lambda: 8000)
        return container

    def test_detect_fills_the_form_from_the_container(self, proxy, monkeypatch):
        self._container(monkeypatch, proxy)
        port = int(proxy.url.rsplit(":", 1)[1])
        assert integrations.run_action("npm", "detect") == {
            "found": True,
            "container": "npm-app-1",
            "version": "2.16.0",
            "admin_port": port,
            "url": f"http://127.0.0.1:{port}",
            "forward_host": "172.18.0.1",
        }

    def test_the_test_reaches_a_bench_port_from_inside_the_container(self, proxy, monkeypatch):
        self._container(monkeypatch, proxy)
        check = integrations.test("npm", _npm_settings(proxy), {"password": NPM_PASSWORD})
        assert check.ok and "http://172.17.0.1:8000/ answered" in check.message

    def test_an_unreachable_forward_address_fails_the_test(self, proxy, monkeypatch):
        self._container(monkeypatch, proxy, exec_out=b"000 28")
        check = integrations.test("npm", _npm_settings(proxy), {"password": NPM_PASSWORD})
        assert not check.ok and "could not reach http://172.17.0.1:8000/" in check.message

    def test_no_container_reports_not_found(self, proxy):
        assert integrations.run_action("npm", "detect") == {"found": False}


def test_a_community_integration_may_provide_the_new_hook_points():
    from caffeinated_whale_cli.core import community

    assert {"dns_record", "proxy_host"} <= set(community.HOOK_POINTS)
    assert json.dumps(integrations.HOOK_LABEL)  # every hook point has a label
    assert set(community.HOOK_POINTS) <= set(integrations.HOOK_LABEL)


def test_the_npm_test_names_what_is_missing_before_calling_anything(proxy):
    check = integrations.test("npm", {"url": proxy.url}, {})
    assert check.message == "Type the email and the password."
    assert proxy.calls == []
