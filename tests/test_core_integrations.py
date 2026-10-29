"""The integrations contract and store (core.integrations): secrets sealed and
write-only, turning on only behind a passing test, a live integration never replaced
by settings that fail, and the runtime Error state."""

from __future__ import annotations

import json

import pytest

from caffeinated_whale_cli.core import integrations
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

SECRET = "hunter2-very-secret"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """One integration whose test passes only for host 'good', and records its hooks."""
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    calls = {"changed": 0, "tested": []}

    def _normalize(settings):
        host = str(settings.get("host") or "").strip()
        if host == "bad input":
            raise CwcliError(ErrorKind.USAGE, "fake.host", "not a host")
        return {"host": host, "port": int(settings.get("port") or 1)}

    def _test(settings, secrets):
        calls["tested"].append((dict(settings), dict(secrets)))
        ok = settings["host"] == "good" and secrets.get("token") == SECRET
        return integrations.Check(ok, "fine" if ok else "refused")

    def _changed():
        calls["changed"] += 1

    integ = integrations.Integration(
        id="fake",
        name="Fake",
        publisher=integrations.OFFICIAL,
        description="",
        provides=("sign_in",),
        fields=(
            integrations.Field("host", "Host", required=True),
            integrations.Field("port", "Port"),
            integrations.Field("token", "Token", kind="secret", required=True),
        ),
        normalize=_normalize,
        test=_test,
        actions={"echo": lambda s, sec, args: {"host": s["host"], "args": args}},
        on_change=_changed,
        secrets_bound_to=("port",),
    )
    monkeypatch.setattr(integrations, "official", lambda: {"fake": integ})
    monkeypatch.setattr(integrations, "registry", lambda: {"fake": integ})
    return calls


def _stored() -> str:
    return integrations.store_path().read_text()


class TestSecrets:
    def test_a_secret_is_sealed_on_disk_and_never_in_a_view(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        assert SECRET not in _stored()
        view = integrations.view("fake")[0]
        assert SECRET not in json.dumps(view)
        assert view["secrets_set"] == {"token": True}
        assert integrations.active("fake") == ({"host": "good", "port": 1}, {"token": SECRET})

    def test_an_empty_secret_keeps_the_stored_one_and_a_new_one_replaces_it(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        integrations.configure("fake", {"host": "good"}, {"token": ""})
        assert integrations.active("fake")[1] == {"token": SECRET}
        check, _view = integrations.configure("fake", None, {"token": "replaced"})
        assert check is not None and not check.ok  # on, so the new secret is tested first
        assert integrations.active("fake")[1] == {"token": SECRET}

    def test_a_sealed_secret_cannot_be_moved_to_another_field_or_integration(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        data = json.loads(_stored())
        sealed = data["integrations"]["fake"]["secrets"]["token"]
        with pytest.raises(CwcliError) as e:
            integrations._unseal(sealed, "other/token")
        assert e.value.code == "integrations.secret_unreadable"

    def test_the_store_and_key_are_private_files(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        assert integrations.store_path().stat().st_mode & 0o777 == 0o600
        assert integrations.key_path().stat().st_mode & 0o777 == 0o600


    def test_a_stored_secret_is_never_sent_where_the_request_points_it(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        tested = len(fake["tested"])
        for call in (
            lambda: integrations.test("fake", {"port": 9}),
            lambda: integrations.run_action("fake", "echo", {}, {"port": 9}),
            lambda: integrations.configure("fake", {"port": 9}),
        ):
            with pytest.raises(CwcliError) as e:
                call()
            assert e.value.code == "integration.secret_retype" and "Token" in e.value.message
        assert len(fake["tested"]) == tested
        assert integrations.test("fake", {"port": 9}, {"token": SECRET}).ok
        assert integrations.test("fake", {"host": "good"}).ok  # the port kept: stored secret


    def test_a_write_waits_while_another_process_holds_the_store(self, fake):
        import threading

        from caffeinated_whale_cli.core.schedules import StoreLock

        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        writer = threading.Thread(target=integrations.record_outcome, args=("fake", "failed"))
        with StoreLock(integrations.store_path()):
            writer.start()
            writer.join(0.3)
            assert writer.is_alive()
            assert json.loads(_stored())["integrations"]["fake"]["last_error"] is None
        writer.join(5)
        assert json.loads(_stored())["integrations"]["fake"]["last_error"]["message"] == "failed"


class TestTurningOn:
    def test_everything_starts_off(self, fake):
        assert integrations.view("fake")[0]["state"] == "off"
        assert integrations.active("fake") is None

    def test_it_turns_on_only_when_the_test_passes(self, fake):
        check, view = integrations.configure(
            "fake", {"host": "good"}, {"token": SECRET}, enable=True
        )
        assert check.ok and view["state"] == "on" and view["enabled"]
        assert fake["changed"] == 1

    def test_a_failed_first_setup_is_kept_as_needs_setup_and_stays_off(self, fake):
        check, view = integrations.configure(
            "fake", {"host": "nope"}, {"token": SECRET}, enable=True
        )
        assert not check.ok
        assert view["state"] == "needs_setup" and not view["enabled"]
        assert view["settings"]["host"] == "nope"  # the form is not lost
        assert integrations.active("fake") is None

    def test_a_failing_change_never_replaces_a_live_integration(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        check, view = integrations.configure("fake", {"host": "typo"})
        assert not check.ok
        assert view["state"] == "on" and view["settings"]["host"] == "good"
        assert integrations.active("fake")[0]["host"] == "good"

    def test_saving_without_enabling_runs_no_test(self, fake):
        check, view = integrations.configure("fake", {"host": "nope"}, {"token": SECRET})
        assert check is None and view["state"] == "off"
        assert fake["tested"] == []

    def test_turning_on_needs_every_required_field(self, fake):
        with pytest.raises(CwcliError) as e:
            integrations.configure("fake", {"host": "good"}, {}, enable=True)
        assert e.value.code == "integration.incomplete" and "Token" in e.value.message

    def test_bad_input_is_refused_before_anything_is_saved(self, fake):
        with pytest.raises(CwcliError):
            integrations.configure("fake", {"host": "bad input"}, {"token": SECRET}, enable=True)
        assert not integrations.store_path().exists()

    def test_turning_off_keeps_the_settings_and_runs_the_hook(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        view = integrations.disable("fake")
        assert view["state"] == "off" and view["settings"]["host"] == "good"
        assert view["secrets_set"]["token"] and fake["changed"] == 2
        assert integrations.active("fake") is None


class TestTestsAndActions:
    def test_a_test_lays_the_form_over_the_stored_values_and_saves_nothing(self, fake):
        integrations.configure("fake", {"host": "nope"}, {"token": SECRET})
        assert integrations.test("fake", {"host": "good"}).ok
        assert integrations.view("fake")[0]["settings"]["host"] == "nope"

    def test_a_test_does_not_need_a_complete_form(self, fake):
        assert not integrations.test(
            "fake", {"host": "good"}
        ).ok  # no token yet: refused, not raised

    def test_an_action_runs_with_the_form_values(self, fake):
        out = integrations.run_action("fake", "echo", {"x": 1}, {"host": "h"})
        assert out == {"host": "h", "args": {"x": 1}}
        with pytest.raises(CwcliError):
            integrations.run_action("fake", "nope")

    def test_an_unknown_integration_is_not_found(self, fake):
        with pytest.raises(CwcliError) as e:
            integrations.configure("nope", {})
        assert e.value.code == "integration.unknown"


class TestRuntimeOutcome:
    def test_an_error_shows_until_the_next_success(self, fake):
        integrations.configure("fake", {"host": "good"}, {"token": SECRET}, enable=True)
        integrations.record_outcome("fake", "upload failed")
        view = integrations.view("fake")[0]
        assert view["state"] == "error" and view["last_error"]["message"] == "upload failed"
        integrations.record_outcome("fake", None)
        assert integrations.view("fake")[0]["state"] == "on"

    def test_an_outcome_on_an_off_integration_is_ignored(self, fake):
        integrations.record_outcome("fake", "x")
        assert integrations.view("fake")[0]["state"] == "off"


def test_the_official_integrations_are_ldap_s3_cloudflare_npm_then_remote_ssh(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    views = integrations.view()
    assert [v["id"] for v in views] == ["ldap", "s3", "cloudflare", "npm", "remote_ssh"]
    assert all(v["publisher"] == "official" and v["state"] == "off" for v in views)
    assert [v["provides"] for v in views] == [
        ["sign_in"],
        ["backup_target"],
        ["dns_record"],
        ["proxy_host"],
        ["host_transport"],
    ]
    assert views[0]["actions"] == ["lookup_user"]
    assert views[2]["actions"] == ["check_edit_access", "detect_ip"]
    assert views[3]["actions"] == ["detect"]
