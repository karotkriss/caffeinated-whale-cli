"""Directory sign-in decisions (core.directory) against a fake directory: who gets in,
with which role, and every refusal. The same flow against a real OpenLDAP server is
``tests/e2e/test_ldap_signin_e2e.py``."""

from __future__ import annotations

import re

import pytest

from caffeinated_whale_cli.core import accounts, directory, integrations
from caffeinated_whale_cli.core import ldap_client as ldap
from caffeinated_whale_cli.core.errors import CwcliError

BASE = "dc=example,dc=org"
SVC = ("cn=svc,dc=example,dc=org", "svc-pw")


def _user(uid, **attrs):
    return f"uid={uid},ou=people,{BASE}", {"uid": uid, "mail": f"{uid}@example.org", **attrs}


USERS = dict(
    [
        _user("alice", cn="Alice A", password="a-pw"),
        _user("bob", cn="Bob B", password="b-pw"),
        _user("carol", cn="Carol C", password="c-pw"),
        _user("dave", cn="Dave D", password="d-pw"),
        _user("eve", cn="Eve E", password="e-pw", pwdAccountLockedTime="000001010000Z"),
        _user("frank", cn="Frank F", password="f-pw", userAccountControl="514"),
        _user("gina", cn="Gina G", password="g-pw", ad_refusal="533"),
        _user("admin", cn="Directory Admin", password="x-pw"),
    ]
)
GROUPS = {
    f"cn=cwcli-admins,ou=groups,{BASE}": {"member": ["alice", "eve", "frank", "gina"]},
    f"cn=cwcli-operators,ou=groups,{BASE}": {"member": ["bob", "alice", "admin"]},
    f"cn=cwcli-viewers,ou=groups,{BASE}": {"memberUid": ["carol"]},
}


class FakeDirectory:
    """Answers the exact filters core.directory sends, and records them."""

    filters: list[str] = []
    down = False

    def __init__(self, url, *, starttls=False, ca_pem="", timeout=10.0):
        if FakeDirectory.down:
            raise ldap.LdapError(-1, "Cannot reach dc1:636: connection refused")
        self.bound = None

    def bind(self, dn, password):
        if (dn, password) == SVC:
            self.bound = dn
            return
        user = USERS.get(dn)
        if user is None or user["password"] != password:
            raise ldap.LdapError(49, "80090308: LdapErr: AcceptSecurityContext error, data 52e")
        if user.get("ad_refusal"):
            raise ldap.LdapError(49, f"AcceptSecurityContext error, data {user['ad_refusal']}")
        self.bound = dn

    def search(self, base, filter_text, *, scope=ldap.SCOPE_SUB, attributes=(), size_limit=0):
        FakeDirectory.filters.append(filter_text)
        ldap.parse_filter(filter_text)  # every filter sent must be well formed
        if scope == ldap.SCOPE_BASE:
            if base != BASE:
                raise ldap.LdapError(ldap.NO_SUCH_OBJECT, "")
            return [ldap.Entry(BASE)]
        m = re.fullmatch(r"\(uid=(.*)\)", filter_text)
        if m:
            return [
                ldap.Entry(dn, {k.lower(): [str(v).encode()] for k, v in u.items()})
                for dn, u in USERS.items()
                if u["uid"] == m.group(1)
            ]
        m = re.search(r"member=uid=([^,]+),", filter_text)
        uid = m.group(1) if m else None
        return [
            ldap.Entry(g, {"cn": [g.split(",")[0][3:].encode()]})
            for g, members in GROUPS.items()
            if uid in members.get("member", []) or uid in members.get("memberUid", [])
        ]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


SETTINGS = {
    "server_url": "ldaps://dc1.example.org",
    "directory_type": "OpenLDAP",
    "bind_dn": SVC[0],
    "base_dn": BASE,
    "group_roles": [
        {"group": "cwcli-admins", "role": "admin"},
        {"group": f"cn=cwcli-operators,ou=groups,{BASE}", "role": "operator"},
        {"group": "CWCLI-Viewers", "role": "viewer"},
    ],
}


@pytest.fixture
def ldap_on(monkeypatch, tmp_path):
    monkeypatch.setenv("CWCLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(directory.ldap, "Connection", FakeDirectory)
    monkeypatch.setattr(FakeDirectory, "filters", [])
    monkeypatch.setattr(FakeDirectory, "down", False)
    accounts.ensure_default()
    check, view = integrations.configure("ldap", SETTINGS, {"bind_password": SVC[1]}, enable=True)
    assert check.ok and view["state"] == "on", check


class TestWhoGetsIn:
    @pytest.mark.parametrize(
        "user,password,role",
        [("bob", "b-pw", "operator"), ("carol", "c-pw", "viewer"), ("alice", "a-pw", "admin")],
    )
    def test_each_allowed_group_signs_in_with_its_role(self, ldap_on, user, password, role):
        found = directory.authenticate(user, password)
        assert (found.username, found.role) == (user, role)
        assert found.email == f"{user}@example.org"

    def test_someone_in_several_groups_gets_the_highest_role(self, ldap_on):
        assert directory.authenticate("alice", "a-pw").role == "admin"  # admins + operators

    def test_groups_match_by_cn_or_full_dn_case_insensitively(self, ldap_on):
        assert directory.authenticate("carol", "c-pw").role == "viewer"  # 'CWCLI-Viewers'


class TestRefusals:
    def _refused(self, user, password):
        with pytest.raises(CwcliError) as e:
            directory.authenticate(user, password)
        return e.value.code

    def test_a_wrong_password_and_an_unknown_name_get_the_same_answer(self, ldap_on):
        assert self._refused("bob", "wrong") == "auth.invalid"
        assert self._refused("nobody", "x") == "auth.invalid"

    def test_a_member_of_no_allowed_group_is_refused(self, ldap_on):
        assert self._refused("dave", "d-pw") == "auth.directory_no_group"

    def test_a_disabled_account_is_refused_even_in_an_allowed_group(self, ldap_on):
        assert self._refused("eve", "e-pw") == "auth.directory_disabled"  # OpenLDAP ppolicy lock
        assert self._refused("frank", "f-pw") == "auth.directory_disabled"  # AD ACCOUNTDISABLE

    def test_active_directory_s_disabled_sub_code_is_named(self, ldap_on):
        assert self._refused("gina", "g-pw") == "auth.directory_disabled"

    def test_the_local_admin_name_is_never_a_directory_user(self, ldap_on):
        assert self._refused("admin", "x-pw") == "auth.directory_reserved"

    def test_an_empty_password_never_reaches_the_directory(self, ldap_on):
        FakeDirectory.filters.clear()
        assert self._refused("bob", "") == "auth.invalid"
        assert FakeDirectory.filters == []

    def test_a_typed_wildcard_is_escaped_into_the_filter(self, ldap_on):
        assert self._refused("*", "x") == "auth.invalid"
        assert r"(uid=\2a)" in FakeDirectory.filters

    def test_an_unreachable_directory_is_its_own_answer_and_marks_the_integration(self, ldap_on):
        FakeDirectory.down = True
        assert self._refused("bob", "b-pw") == "auth.directory_unreachable"
        view = integrations.view("ldap")[0]
        assert view["state"] == "error" and "Cannot reach" in view["last_error"]["message"]
        FakeDirectory.down = False
        directory.authenticate("bob", "b-pw")
        assert integrations.view("ldap")[0]["state"] == "on"

    def test_nobody_signs_in_through_the_directory_while_it_is_off(self, ldap_on):
        integrations.disable("ldap")
        assert self._refused("bob", "b-pw") == "auth.invalid"


class TestLookUp:
    def test_it_reports_what_sign_in_would_decide_without_a_password(self, ldap_on):
        out = integrations.run_action("ldap", "lookup_user", {"username": "eve"})
        assert out["found"] and out["disabled"] and not out["allowed"]
        assert out["role"] == "admin" and "disabled" in out["reason"]
        assert integrations.run_action("ldap", "lookup_user", {"username": "zed"}) == {
            "found": False,
            "username": "zed",
        }
        out = integrations.run_action("ldap", "lookup_user", {"username": "dave"})
        assert out["found"] and out["role"] is None and not out["allowed"]


class TestSettings:
    @pytest.mark.parametrize(
        "change,code",
        [
            ({"server_url": "ldap://dc1"}, "ldap.cleartext"),
            ({"server_url": "http://dc1"}, "ldap.server_url"),
            ({"user_filter": "(uid=jo)"}, "ldap.user_filter"),
            ({"user_filter": "(uid={username}"}, "ldap.user_filter"),
            ({"ca_certificate": "nope"}, "ldap.ca_certificate"),
            ({"group_roles": [{"group": "g", "role": "root"}]}, "ldap.group_roles"),
            ({"directory_type": "Novell"}, "ldap.directory_type"),
        ],
    )
    def test_bad_settings_are_refused_with_a_reason(self, change, code):
        with pytest.raises(CwcliError) as e:
            directory._normalize({**SETTINGS, **change})
        assert e.value.code == code

    def test_defaults_follow_the_directory_type(self):
        ad = directory._with_defaults(
            directory._normalize({**SETTINGS, "directory_type": "Active Directory"})
        )
        assert ad["user_filter"] == "(sAMAccountName={username})"
        assert ad["username_attribute"] == "sAMAccountName"
        ol = directory._with_defaults(directory._normalize(SETTINGS))
        assert (ol["user_filter"], ol["display_name_attribute"]) == ("(uid={username})", "cn")

    def test_switching_the_directory_type_drops_the_old_types_search(self, ldap_on):
        integrations.configure("ldap", {"directory_type": "Active Directory"})
        stored = integrations.view("ldap")[0]["settings"]
        integrations.configure("ldap", {**stored, "directory_type": "OpenLDAP"})
        out = integrations.run_action("ldap", "lookup_user", {"username": "bob"})
        assert out["found"] and out["role"] == "operator"

    def test_a_filter_without_parentheses_is_wrapped_and_starttls_is_kept(self):
        out = directory._normalize(
            {
                **SETTINGS,
                "server_url": "ldap://dc1",
                "starttls": True,
                "user_filter": "uid={username}",
            }
        )
        assert out["user_filter"] == "(uid={username})" and out["starttls"]

    def test_saving_or_turning_off_signs_every_directory_user_out(self, ldap_on):
        local = accounts.open_session("admin", remember=False)
        accounts.open_session("bob", remember=False, source="ldap", role="operator")
        integrations.disable("ldap")
        assert accounts.session_account(local) is not None
        data = accounts._load()["sessions"]
        assert all(v.get("source", "local") == "local" for v in data.values())
