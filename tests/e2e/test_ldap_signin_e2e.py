"""LDAP / Active Directory sign-in against a REAL directory server.

A throwaway OpenLDAP container (its certificate signed by a CA this test makes, so
the CA-certificate field is what makes TLS work) holds five users: one in each
allowed group (admins, operators via ``groupOfNames``, viewers via ``posixGroup``),
one in no group, and one in the admins group but locked by the password policy.
The shipped ``cwcli ui`` is driven over HTTP the way the page drives it.

What only a real directory proves: the stdlib LDAP client's BER, ``ldaps://`` and
StartTLS against a real TLS stack, the service bind, the user search, group
membership through both group shapes, and the directory refusing a locked account.
No Frappe instance is needed, so this is ``standalone``.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from . import harness

pytestmark = [pytest.mark.e2e, pytest.mark.standalone]

IMAGE = "osixia/openldap:1.5.0"
BASE = "dc=example,dc=org"
DIR_ADMIN_PW = "cwe2e-dir-admin"
SVC_DN, SVC_PW = f"cn=svc-cwcli,ou=services,{BASE}", "cwe2e-svc-pw"
ADMIN_PW = "cwe2e local admin pw"
TOKEN = "cwe2e-ldap-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _certs(out: Path) -> str:
    """A CA and a localhost server certificate it signed; returns the CA's PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = dt.datetime.now(dt.timezone.utc)

    def _key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _pem_key(k):
        return k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

    ca_key, srv_key = _key(), _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cwe2e directory CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    srv = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    (out / "ca.crt").write_bytes(ca_pem)
    (out / "ldap.crt").write_bytes(srv.public_bytes(serialization.Encoding.PEM))
    (out / "ldap.key").write_bytes(_pem_key(srv_key))
    return ca_pem.decode()


def _people() -> str:
    entries = [
        f"dn: ou={ou},{BASE}\nobjectClass: organizationalUnit\nou: {ou}\n"
        for ou in ("people", "groups", "services", "policies")
    ]
    entries.append(
        f"dn: {SVC_DN}\nobjectClass: person\ncn: svc-cwcli\nsn: service\n"
        f"userPassword: {SVC_PW}\n"
    )
    for uid in ("alice", "bob", "carol", "dave", "eve"):
        entries.append(
            f"dn: uid={uid},ou=people,{BASE}\nobjectClass: inetOrgPerson\nuid: {uid}\n"
            f"cn: {uid.title()} Example\nsn: Example\nmail: {uid}@example.org\n"
            f"userPassword: {uid}-pw-123\n"
        )
    entries += [
        f"dn: cn=cwcli-admins,ou=groups,{BASE}\nobjectClass: groupOfNames\ncn: cwcli-admins\n"
        f"member: uid=alice,ou=people,{BASE}\nmember: uid=eve,ou=people,{BASE}\n",
        f"dn: cn=cwcli-operators,ou=groups,{BASE}\nobjectClass: groupOfNames\n"
        f"cn: cwcli-operators\nmember: uid=bob,ou=people,{BASE}\n",
        f"dn: cn=cwcli-viewers,ou=groups,{BASE}\nobjectClass: posixGroup\ncn: cwcli-viewers\n"
        "gidNumber: 5003\nmemberUid: carol\n",
    ]
    return "\n".join(entries)


def _dexec(name: str, script: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-i", name, "bash", "-c", script],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def directory():
    """The throwaway directory: ``(name, ldaps_port, ldap_port, ca_pem)``."""
    name = f"cwe2e-ldap-{uuid.uuid4().hex[:8]}"
    ldaps_port, ldap_port = _free_port(), _free_port()
    with tempfile.TemporaryDirectory() as tmp:
        ca_pem = _certs(Path(tmp))
        created = subprocess.run(
            [
                "docker", "create", "--name", name,
                "-p", f"127.0.0.1:{ldaps_port}:636", "-p", f"127.0.0.1:{ldap_port}:389",
                "-e", "LDAP_ORGANISATION=cwe2e", "-e", "LDAP_DOMAIN=example.org",
                "-e", f"LDAP_ADMIN_PASSWORD={DIR_ADMIN_PW}",
                "-e", "LDAP_TLS_VERIFY_CLIENT=never",
                "-e", "LDAP_TLS_CRT_FILENAME=ldap.crt", "-e", "LDAP_TLS_KEY_FILENAME=ldap.key",
                "-e", "LDAP_TLS_CA_CRT_FILENAME=ca.crt",
                IMAGE, "--copy-service",
            ],
            capture_output=True, text=True, timeout=600,
        )  # fmt: skip
        assert created.returncode == 0, created.stderr
        try:
            for f in ("ca.crt", "ldap.crt", "ldap.key"):
                subprocess.run(
                    ["docker", "cp", str(Path(tmp) / f), f"{name}:/container/service/slapd/assets/certs/{f}"],
                    check=True, capture_output=True, timeout=60,
                )  # fmt: skip
            subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=60)
            admin = f"-x -H ldap://localhost -D cn=admin,{BASE} -w {DIR_ADMIN_PW}"
            harness.wait_until(
                lambda: _dexec(name, f"ldapsearch {admin} -b {BASE} -s base dn").returncode == 0,
                timeout=120, interval=1, desc="OpenLDAP answering",
            )  # fmt: skip
            # The people and groups, the password policy (so a locked account is refused
            # at bind; its default policy can only be added once the overlay exists), and
            # the service account's read access.
            setup = (
                "set -e\n"
                "Q='ldapsearch -Y EXTERNAL -H ldapi:/// -LLL -b cn=config'\n"
                "$Q '(cn=*ppolicy)' dn | grep -q ppolicy"
                " || ldapadd -Y EXTERNAL -H ldapi:/// -f /etc/ldap/schema/ppolicy.ldif\n"
                "ldapsearch -Y EXTERNAL -H ldapi:/// -LLL -b 'cn=module{0},cn=config' olcModuleLoad"
                " | grep -q ppolicy"
                " || printf 'dn: cn=module{0},cn=config\\nchangetype: modify\\n"
                "add: olcModuleLoad\\nolcModuleLoad: ppolicy\\n' | ldapmodify -Y EXTERNAL -H ldapi:///\n"
                f"ldapadd {admin} -f /dev/stdin\n"
                f"DB=$(ldapsearch -Y EXTERNAL -H ldapi:/// -LLL -b cn=config '(olcSuffix={BASE})' dn"
                " | sed -n 's/^dn: //p')\n"
                'printf "dn: olcOverlay=ppolicy,$DB\\nobjectClass: olcOverlayConfig\\n'
                'objectClass: olcPPolicyConfig\\nolcOverlay: ppolicy\\n"'
                " | ldapadd -Y EXTERNAL -H ldapi:///\n"
                f"printf 'dn: cn=default,ou=policies,{BASE}\\nobjectClass: pwdPolicy\\n"
                "objectClass: device\\ncn: default\\npwdAttribute: userPassword\\n"
                f"pwdLockout: TRUE\\n' | ldapadd {admin}\n"
                "OV=$(ldapsearch -Y EXTERNAL -H ldapi:/// -LLL -b \"$DB\" '(objectClass=olcPPolicyConfig)' dn"
                " | sed -n 's/^dn: //p')\n"
                'printf "dn: $OV\\nchangetype: modify\\n'
                f'add: olcPPolicyDefault\\nolcPPolicyDefault: cn=default,ou=policies,{BASE}\\n"'
                " | ldapmodify -Y EXTERNAL -H ldapi:///\n"
                'printf "dn: $DB\\nchangetype: modify\\nadd: olcAccess\\n'
                f'olcAccess: {{2}}to * by self read by dn.exact=\\"{SVC_DN}\\" read by * break\\n"'
                " | ldapmodify -Y EXTERNAL -H ldapi:///\n"
                f"printf 'dn: uid=eve,ou=people,{BASE}\\nchangetype: modify\\n"
                "add: pwdAccountLockedTime\\npwdAccountLockedTime: 000001010000Z\\n'"
                f" | ldapmodify {admin}\n"
            )
            done = _dexec(name, setup, stdin=_people())
            assert done.returncode == 0, done.stdout + done.stderr
            locked = _dexec(
                name,
                f"ldapwhoami -x -H ldap://localhost -D uid=eve,ou=people,{BASE} -w eve-pw-123",
            )
            assert locked.returncode != 0, "the password policy did not lock eve"
            yield name, ldaps_port, ldap_port, ca_pem
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def daemon():
    port = _free_port()
    proc = subprocess.Popen(
        [harness.CWCLI, "ui", "--foreground", "--no-browser", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "CWCLI_SERVE_TOKEN": TOKEN},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        harness.wait_until(
            lambda: _call(base, "/api/v1/meta")[0] == 200,
            timeout=60, interval=0.5, desc="cwcli ui listening",
        )  # fmt: skip
        # The local admin picks its first password, as a person would on first use.
        _s, _b, cookie = _sign_in(base, "admin", "admin")
        assert _call(base, "/api/v1/session/password", {"new": ADMIN_PW}, cookie=cookie)[0] == 200
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def _call(base, path, payload=None, *, cookie=None, bearer=False):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if bearer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def _sign_in(base, username, password):
    status, body, headers = _call(
        base, "/api/v1/session", {"username": username, "password": password}
    )
    return status, body, (headers.get("Set-Cookie") or "").split(";")[0]


def _code(body) -> str | None:
    return (body.get("errors") or [{}])[0].get("title")


def _settings(directory, *, starttls=False):
    _name, ldaps_port, ldap_port, ca_pem = directory
    return {
        "server_url": (
            f"ldap://localhost:{ldap_port}" if starttls else f"ldaps://localhost:{ldaps_port}"
        ),
        "starttls": starttls,
        "ca_certificate": ca_pem,
        "directory_type": "OpenLDAP",
        "bind_dn": SVC_DN,
        "base_dn": BASE,
        "group_roles": [
            {"group": "cwcli-admins", "role": "admin"},
            {"group": f"cn=cwcli-operators,ou=groups,{BASE}", "role": "operator"},
            {"group": "cwcli-viewers", "role": "viewer"},
        ],
    }


def _integrations(base, payload):
    status, body, _ = _call(base, "/api/v1/integrations", payload, bearer=True)
    assert status == 200, body
    return body["data"]


def test_directory_sign_in_end_to_end(directory, daemon):
    base = daemon

    # Without the CA the directory's certificate is not trusted: the test says so.
    no_ca = {**_settings(directory), "ca_certificate": ""}
    check = _integrations(
        base, {"op": "test", "id": "ldap", "settings": no_ca, "secrets": {"bind_password": SVC_PW}}
    )["check"]
    assert not check["ok"] and "CERTIFICATE_VERIFY_FAILED" in check["message"]

    # A wrong service password fails the test, and turning on stays off.
    out = _integrations(
        base,
        {
            "op": "configure",
            "id": "ldap",
            "enable": True,
            "settings": _settings(directory),
            "secrets": {"bind_password": "wrong"},
        },
    )
    assert not out["check"]["ok"] and out["integration"]["state"] == "needs_setup"
    assert _call(base, "/api/v1/meta")[1]["directory_sign_in"] is False

    # The right one turns it on; the stored file never holds it in clear text.
    out = _integrations(
        base,
        {
            "op": "configure",
            "id": "ldap",
            "enable": True,
            "settings": _settings(directory),
            "secrets": {"bind_password": SVC_PW},
        },
    )
    assert out["check"]["ok"] and out["integration"]["state"] == "on", out
    assert out["integration"]["secrets_set"] == {"bind_password": True}
    store = Path(os.environ["CWCLI_HOME"]) / "integrations.json"
    assert SVC_PW not in store.read_text()
    assert _call(base, "/api/v1/meta")[1]["directory_sign_in"] is True

    # Look up a user: what sign-in would decide, without a password.
    def lookup(name):
        return _integrations(
            base,
            {"op": "action", "id": "ldap", "action": "lookup_user", "args": {"username": name}},
        )["result"]

    assert lookup("alice")["role"] == "admin" and lookup("alice")["allowed"]
    assert lookup("carol")["role"] == "viewer"  # posixGroup memberUid
    assert (
        lookup("dave")["found"] and not lookup("dave")["allowed"] and lookup("dave")["role"] is None
    )
    assert lookup("eve")["disabled"] and not lookup("eve")["allowed"]
    assert lookup("nobody") == {"found": False, "username": "nobody"}

    # Sign-in: each allowed group gets its role; everyone else is refused.
    for user, role in (("alice", "admin"), ("bob", "operator"), ("carol", "viewer")):
        status, body, _ = _sign_in(base, user, f"{user}-pw-123")
        assert status == 200, body
        assert (body["data"]["role"], body["data"]["source"]) == (role, "ldap")
    status, body, _ = _sign_in(base, "dave", "dave-pw-123")
    assert status == 403 and _code(body) == "auth.directory_no_group"
    status, body, _ = _sign_in(base, "eve", "eve-pw-123")  # the directory refuses the bind
    assert status in (401, 403) and _code(body) in ("auth.invalid", "auth.directory_disabled")
    assert _sign_in(base, "alice", "wrong")[0] == 401

    # A directory viewer reads but changes nothing.
    _s, _b, viewer = _sign_in(base, "carol", "carol-pw-123")
    assert _call(base, "/api/v1/instances", cookie=viewer)[0] == 200
    status, body, _ = _call(base, "/api/v1/settings", {"setting": "tips.enable"}, cookie=viewer)
    assert status == 403 and _code(body) == "auth.role_forbidden"

    # The local admin is the break-glass way in while the directory is on.
    status, body, _ = _sign_in(base, "admin", ADMIN_PW)
    assert status == 200 and body["data"]["source"] == "local"

    # Moving the server needs the service password typed again, so a stored one never
    # goes to a server the request names.
    starttls = {"op": "configure", "id": "ldap", "enable": True}
    starttls["settings"] = _settings(directory, starttls=True)
    status, body, _ = _call(base, "/api/v1/integrations", starttls, bearer=True)
    assert status == 400 and _code(body) == "integration.secret_retype", body

    # StartTLS on ldap:// works the same.
    out = _integrations(base, {**starttls, "secrets": {"bind_password": SVC_PW}})
    assert out["check"]["ok"] and "StartTLS" in out["check"]["message"], out
    assert _sign_in(base, "bob", "bob-pw-123")[0] == 200

    # Saving signed directory users out; turning it off keeps them out.
    assert _call(base, "/api/v1/instances", cookie=viewer)[0] == 401
    _s, _b, viewer = _sign_in(base, "carol", "carol-pw-123")
    _integrations(base, {"op": "disable", "id": "ldap"})
    assert _call(base, "/api/v1/instances", cookie=viewer)[0] == 401
    assert _sign_in(base, "alice", "alice-pw-123")[0] == 401
    assert _sign_in(base, "admin", ADMIN_PW)[0] == 200
