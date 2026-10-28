"""The stdlib LDAP client's pure parts: filter encoding, escaping, URLs, and the two
refusals that never touch the network (clear text, an empty password). The network
half is proven against a real directory in ``tests/e2e/test_ldap_signin_e2e.py``."""

from __future__ import annotations

import pytest

from caffeinated_whale_cli.core import ldap_client as ldap


class TestFilters:
    def test_equality_is_the_rfc_4511_encoding(self):
        # [3] equalityMatch { attributeDesc "uid", assertionValue "jo" }
        assert ldap.parse_filter("(uid=jo)") == bytes.fromhex("a309040375696404026a6f")

    def test_parentheses_are_optional_on_a_single_item(self):
        assert ldap.parse_filter("uid=jo") == ldap.parse_filter("(uid=jo)")

    def test_presence_and_the_set_operators(self):
        assert ldap.parse_filter("(cn=*)") == b"\x87\x02cn"
        both = ldap.parse_filter("(&(cn=*)(!(uid=jo)))")
        assert both[0] == 0xA0 and b"\xa2" in both
        assert ldap.parse_filter("(|(cn=*)(sn=*))")[0] == 0xA1

    def test_substrings_split_into_initial_any_final(self):
        enc = ldap.parse_filter("(cn=a*b*c)")
        assert enc[0] == 0xA4
        assert b"\x80\x01a" in enc and b"\x81\x01b" in enc and b"\x82\x01c" in enc

    def test_the_active_directory_in_chain_rule_is_an_extensible_match(self):
        enc = ldap.parse_filter("(member:1.2.840.113556.1.4.1941:=CN=Jo,DC=x)")
        assert enc[0] == 0xA9
        assert b"\x81\x171.2.840.113556.1.4.1941" in enc and b"\x82\x06member" in enc

    def test_escaped_bytes_are_decoded_into_the_value(self):
        assert ldap.parse_filter(r"(cn=a\2ab)") == b"\xa3\x09\x04\x02cn\x04\x03a*b"

    @pytest.mark.parametrize("bad", ["(uid=jo", "(&)", "(=jo)", "(uid=jo))", r"(cn=\z)"])
    def test_malformed_filters_are_refused(self, bad):
        with pytest.raises(ldap.LdapError):
            ldap.parse_filter(bad)


class TestEscape:
    def test_every_filter_metacharacter_is_escaped(self):
        assert ldap.escape("a*b(c)d\\e\0") == r"a\2ab\28c\29d\5ce\00"

    def test_a_typed_star_cannot_become_a_wildcard(self):
        """A username of '*' would otherwise match every entry."""
        assert ldap.parse_filter(f"(uid={ldap.escape('*')})")[0] == 0xA3


class TestUrls:
    def test_default_ports(self):
        assert ldap.parse_url("ldaps://dc1.example.com") == ("ldaps", "dc1.example.com", 636)
        assert ldap.parse_url("ldap://dc1.example.com") == ("ldap", "dc1.example.com", 389)
        assert ldap.parse_url("LDAPS://dc1:3269") == ("ldaps", "dc1", 3269)

    @pytest.mark.parametrize("bad", ["http://dc1", "dc1.example.com", "ldaps://", "ldap://h:x"])
    def test_anything_else_is_refused(self, bad):
        with pytest.raises(ldap.LdapError):
            ldap.parse_url(bad)


class TestRefusalsBeforeTheNetwork:
    def test_clear_text_ldap_without_starttls_is_refused(self):
        with pytest.raises(ldap.LdapError) as e:
            ldap.Connection("ldap://127.0.0.1:1")
        assert "clear text" in e.value.message

    def test_an_empty_password_is_never_sent(self):
        """RFC 4513: a name with no password is an unauthenticated bind that servers
        answer with success, so it must never reach the wire."""
        conn = object.__new__(ldap.Connection)
        with pytest.raises(ldap.LdapError) as e:
            conn.bind("uid=jo,dc=x", "")
        assert e.value.code == ldap.INVALID_CREDENTIALS

    def test_a_ca_that_is_not_pem_is_refused(self):
        with pytest.raises(ldap.LdapError) as e:
            ldap.Connection("ldaps://127.0.0.1:1", ca_pem="not a certificate")
        assert "PEM" in e.value.message


def test_entry_values_are_case_insensitive_by_attribute_name():
    entry = ldap.Entry("uid=jo", {"mail": [b"jo@x"], "memberof": [b"cn=a", b"cn=b"]})
    assert entry.first("Mail") == "jo@x"
    assert entry.values("memberOf") == ["cn=a", "cn=b"]
    assert entry.first("cn") is None
