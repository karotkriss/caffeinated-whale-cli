"""LDAP / Active Directory sign-in for the web UI: the ``ldap`` integration.

Sign-in works the way directory sign-in usually does:

1. Bind as the service account and find the user with the search filter
   (``{username}`` is the typed name, escaped); exactly one entry must match.
2. Read the user's groups (Active Directory: every group the user is in, nested ones
   included, through the in-chain matching rule; OpenLDAP: ``groupOfNames``,
   ``groupOfUniqueNames`` and ``posixGroup`` membership) and whether the account is
   disabled.
3. Bind as the user with the typed password on its own connection. The directory
   owns the password, so cwcli never stores or changes it.
4. Refuse a disabled account and anyone outside the allowed groups; otherwise the
   role is the highest one their allowed groups map to.

All of it runs at every sign-in, so a user removed from a group or disabled in the
directory cannot sign in again (their current session runs to its normal expiry;
saving or turning off this integration signs every directory user out).
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass

from . import accounts, integrations
from . import ldap_client as ldap
from .errors import CwcliError, ErrorKind

AD, OPENLDAP = "Active Directory", "OpenLDAP"
_DEFAULTS = {
    AD: {
        "user_filter": "(sAMAccountName={username})",
        "username_attribute": "sAMAccountName",
        "email_attribute": "mail",
        "display_name_attribute": "displayName",
    },
    OPENLDAP: {
        "user_filter": "(uid={username})",
        "username_attribute": "uid",
        "email_attribute": "mail",
        "display_name_attribute": "cn",
    },
}
_IN_CHAIN = "1.2.840.113556.1.4.1941"
_SERVICE_REFUSED = -4  # LdapError code: the service account's own bind was refused
# Active Directory's sub-codes in a refused bind's diagnostic ("... data 533, ...").
# It reports them only once the password itself was right.
_AD_REFUSALS = {
    "533": ("auth.directory_disabled", "Your directory account is disabled."),
    "701": ("auth.directory_disabled", "Your directory account has expired."),
    "775": ("auth.directory_locked", "Your directory account is locked."),
    "532": ("auth.directory_password_expired", "Your directory password has expired."),
    "773": ("auth.directory_password_expired", "Your directory password must be changed."),
}


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    username: str
    dn: str
    name: str
    email: str
    role: str


# --------------------------------------------------------------------- settings


def _normalize(settings: dict) -> dict:
    kind = settings.get("directory_type") or AD
    if kind not in _DEFAULTS:
        raise CwcliError(
            ErrorKind.USAGE, "ldap.directory_type", f"Directory must be {AD} or {OPENLDAP}."
        )
    out: dict = {"directory_type": kind}
    for key in ("server_url", "bind_dn", "base_dn", "ca_certificate"):
        out[key] = str(settings.get(key) or "").strip()
    out["starttls"] = settings.get("starttls") is True
    for key in _DEFAULTS[kind]:
        out[key] = str(settings.get(key) or "").strip()
    if out["user_filter"] and not out["user_filter"].startswith("("):
        out["user_filter"] = f"({out['user_filter']})"
    if out["server_url"]:
        try:
            scheme, _host, _port = ldap.parse_url(out["server_url"])
        except ldap.LdapError as e:
            raise CwcliError(ErrorKind.USAGE, "ldap.server_url", e.message) from None
        if scheme == "ldaps":
            out["starttls"] = False
        elif not out["starttls"]:
            raise CwcliError(
                ErrorKind.USAGE,
                "ldap.cleartext",
                "Passwords must not cross the network in clear text: use an ldaps:// URL "
                "or turn on StartTLS.",
            )
    if out["ca_certificate"]:
        try:
            ssl.create_default_context(cadata=out["ca_certificate"])
        except (ssl.SSLError, ValueError):
            raise CwcliError(
                ErrorKind.USAGE,
                "ldap.ca_certificate",
                "The CA certificate is not a PEM certificate (-----BEGIN CERTIFICATE-----).",
            ) from None
    user_filter = _with_defaults(out)["user_filter"]
    if "{username}" not in user_filter:
        raise CwcliError(
            ErrorKind.USAGE,
            "ldap.user_filter",
            "The user search filter must contain {username}, e.g. (sAMAccountName={username}).",
        )
    try:
        ldap.parse_filter(user_filter.replace("{username}", "x"))
    except ldap.LdapError as e:
        raise CwcliError(ErrorKind.USAGE, "ldap.user_filter", e.message) from None
    out["group_roles"] = _group_roles(settings.get("group_roles"))
    return out


def _with_defaults(settings: dict) -> dict:
    """The settings with the directory type's own value for each search field left
    empty, so a change of type never keeps the other type's filter and attributes."""
    defaults = _DEFAULTS[settings["directory_type"]]
    return {**settings, **{k: settings.get(k) or v for k, v in defaults.items()}}


def _group_roles(raw) -> list[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise CwcliError(
            ErrorKind.USAGE, "ldap.group_roles", "group_roles must be a list of {group, role}."
        )
    out, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise CwcliError(ErrorKind.USAGE, "ldap.group_roles", "Each group needs a role.")
        group = str(row.get("group") or "").strip()
        role = str(row.get("role") or "").strip().lower()
        if not group:
            continue
        if role not in accounts.ROLES:
            raise CwcliError(
                ErrorKind.USAGE,
                "ldap.group_roles",
                f"'{group}' needs a role: Admin, Operator or Viewer.",
            )
        if _norm(group) in seen:
            continue
        seen.add(_norm(group))
        out.append({"group": group, "role": role})
    return out


# ------------------------------------------------------------------- the reads


def _norm(name: str) -> str:
    """A group name or DN compared the way directories compare them."""
    return re.sub(r"\s*([,=])\s*", r"\1", name.strip()).lower()


def _cn(dn: str) -> str:
    first = re.split(r"(?<!\\),", dn, maxsplit=1)[0]
    _attr, _sep, value = first.partition("=")
    return value.strip()


def _connect(settings: dict) -> ldap.Connection:
    return ldap.Connection(
        settings["server_url"], starttls=settings["starttls"], ca_pem=settings["ca_certificate"]
    )


def _service(settings: dict, secrets: dict) -> ldap.Connection:
    conn = _connect(settings)
    try:
        conn.bind(settings["bind_dn"], secrets.get("bind_password", ""))
    except ldap.LdapError as e:
        conn.close()
        if e.code == ldap.INVALID_CREDENTIALS:
            raise ldap.LdapError(
                _SERVICE_REFUSED, "The directory refused the service account's name or password."
            ) from None
        raise
    return conn


def _find(conn: ldap.Connection, settings: dict, username: str) -> ldap.Entry | None:
    attrs = (
        settings["username_attribute"],
        settings["email_attribute"],
        settings["display_name_attribute"],
        "memberOf",
        "userAccountControl",
        "pwdAccountLockedTime",
        "nsAccountLock",
    )
    found = conn.search(
        settings["base_dn"],
        settings["user_filter"].replace("{username}", ldap.escape(username)),
        attributes=attrs,
    )
    if len(found) > 1:
        raise ldap.LdapError(
            -3,
            f"The user search filter matches {len(found)} directory entries for "
            f"'{username}'; make it match exactly one.",
        )
    return found[0] if found else None


def _groups(conn: ldap.Connection, settings: dict, entry: ldap.Entry) -> list[dict]:
    """Every group the user is in: ``[{dn, cn}]``."""
    dn = ldap.escape(entry.dn)
    if settings["directory_type"] == AD:
        flt = f"(member:{_IN_CHAIN}:={dn})"
    else:
        uid = ldap.escape(entry.first(settings["username_attribute"]) or "")
        flt = f"(|(member={dn})(uniqueMember={dn})(memberUid={uid}))"
    groups = {
        _norm(g.dn): {"dn": g.dn, "cn": g.first("cn") or _cn(g.dn)}
        for g in conn.search(settings["base_dn"], flt, attributes=("cn",))
    }
    for member_of in entry.values("memberOf"):
        groups.setdefault(_norm(member_of), {"dn": member_of, "cn": _cn(member_of)})
    return sorted(groups.values(), key=lambda g: g["dn"].lower())


def _disabled(entry: ldap.Entry) -> bool:
    try:
        if int(entry.first("userAccountControl") or 0) & 0x2:
            return True
    except ValueError:
        pass
    if entry.first("pwdAccountLockedTime"):
        return True
    return (entry.first("nsAccountLock") or "").lower() == "true"


def _role(settings: dict, groups: list[dict]) -> tuple[str | None, list[str]]:
    """The highest role the user's groups map to, and the mapped groups they are in."""
    names = {_norm(g["dn"]) for g in groups} | {_norm(g["cn"]) for g in groups}
    matched = [m for m in settings["group_roles"] if _norm(m["group"]) in names]
    if not matched:
        return None, []
    best = max(matched, key=lambda m: accounts.ROLE_RANK[m["role"]])
    return best["role"], [m["group"] for m in matched]


def _describe(e: ldap.LdapError, settings: dict) -> str:
    if e.code == ldap.NO_SUCH_OBJECT:
        return (
            f"The base DN '{settings['base_dn']}' does not exist, or the service account "
            "cannot read it."
        )
    return e.message


# ---------------------------------------------------------------- the contract


def _test(settings: dict, secrets: dict) -> integrations.Check:
    try:
        with _service(settings, secrets) as conn:
            conn.search(
                settings["base_dn"], "(objectClass=*)", scope=ldap.SCOPE_BASE, attributes=("1.1",)
            )
    except ldap.LdapError as e:
        return integrations.Check(False, _describe(e, settings))
    tls = "StartTLS" if settings["starttls"] else "LDAPS"
    return integrations.Check(
        True,
        f"Connected over {tls}, signed in as the service account, and found the base DN.",
        {"tls": tls},
    )


def lookup_user(settings: dict, secrets: dict, args: dict) -> dict:
    """What sign-in would decide for ``args['username']``, without a password: found
    or not, the groups, whether the account is disabled, and the role."""
    username = str(args.get("username") or "").strip()
    if not username:
        raise CwcliError(ErrorKind.USAGE, "ldap.username", "Type a username to look up.")
    settings = _with_defaults(settings)
    try:
        with _service(settings, secrets) as conn:
            entry = _find(conn, settings, username)
            if entry is None:
                return {"found": False, "username": username}
            groups = _groups(conn, settings, entry)
    except ldap.LdapError as e:
        raise CwcliError(ErrorKind.PRECONDITION, "ldap.lookup", _describe(e, settings)) from None
    role, matched = _role(settings, groups)
    disabled = _disabled(entry)
    reserved = accounts.is_local(entry.first(settings["username_attribute"]) or username)
    return {
        "found": True,
        "dn": entry.dn,
        "username": entry.first(settings["username_attribute"]) or username,
        "name": entry.first(settings["display_name_attribute"]) or "",
        "email": entry.first(settings["email_attribute"]) or "",
        "groups": groups,
        "matched_groups": matched,
        "disabled": disabled,
        "role": role,
        "allowed": bool(role) and not disabled and not reserved,
        "reason": (
            "The account is disabled in the directory."
            if disabled
            else (
                "The name belongs to the local break-glass account."
                if reserved
                else None if role else "The user is in none of the allowed groups."
            )
        ),
    }


def current_role(username: str) -> str | None:
    """The role ``username`` would sign in with now, without a password, or None (not
    found, disabled, outside the allowed groups, or directory sign-in off). Raises
    ``CwcliError`` when the directory cannot be asked."""
    found = integrations.active("ldap")
    if found is None:
        return None
    out = lookup_user(*found, {"username": username})
    return out["role"] if out.get("allowed") else None


def _refused(code: str, message: str) -> CwcliError:
    return CwcliError(ErrorKind.PRECONDITION, code, message)


def authenticate(username: str, password: str) -> DirectoryUser:
    """Sign a directory user in, or raise: ``auth.invalid`` for an unknown name or a
    wrong password (the same answer for both), ``auth.directory_disabled`` /
    ``_locked`` / ``_password_expired`` / ``_no_group`` once the password was right,
    ``auth.directory_unreachable`` when the directory cannot be asked."""
    found = integrations.active("ldap")
    if found is None:
        raise _refused("auth.invalid", "That username and password do not match.")
    settings, secrets = _with_defaults(found[0]), found[1]
    if not username or not password:
        raise _refused("auth.invalid", "That username and password do not match.")
    try:
        with _service(settings, secrets) as conn:
            entry = _find(conn, settings, username)
            groups = _groups(conn, settings, entry) if entry is not None else []
        if entry is None:
            integrations.record_outcome("ldap", None)
            raise _refused("auth.invalid", "That username and password do not match.")
        with _connect(settings) as conn:
            conn.bind(entry.dn, password)
    except ldap.LdapError as e:
        if e.code == ldap.INVALID_CREDENTIALS:
            integrations.record_outcome("ldap", None)
            sub = re.search(r"data ([0-9a-f]{3})", e.message)
            if sub and sub.group(1) in _AD_REFUSALS:
                raise _refused(*_AD_REFUSALS[sub.group(1)]) from None
            raise _refused("auth.invalid", "That username and password do not match.") from None
        message = _describe(e, settings)
        integrations.record_outcome("ldap", message)
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "auth.directory_unreachable",
            "The directory could not check your sign-in.",
            hint="Try again shortly; an administrator can sign in with the local admin "
            "account and check Settings > Integrations.",
        ) from None
    integrations.record_outcome("ldap", None)
    canonical = entry.first(settings["username_attribute"]) or username
    if _disabled(entry):
        raise _refused("auth.directory_disabled", "Your directory account is disabled.")
    if accounts.is_local(canonical):
        raise _refused(
            "auth.directory_reserved",
            f"'{canonical}' is the name of the local break-glass account; sign in with "
            "its own password.",
        )
    role, _matched = _role(settings, groups)
    if role is None:
        raise _refused(
            "auth.directory_no_group",
            "Your directory account is not in a group allowed to sign in to cwcli.",
        )
    return DirectoryUser(
        username=canonical,
        dn=entry.dn,
        name=entry.first(settings["display_name_attribute"]) or canonical,
        email=entry.first(settings["email_attribute"]) or "",
        role=role,
    )


INTEGRATION = integrations.Integration(
    id="ldap",
    name="LDAP / Active Directory sign-in",
    publisher=integrations.OFFICIAL,
    description="Directory users sign in to the web UI with their own password; their "
    "groups decide their role. The local admin account keeps working.",
    provides=("sign_in",),
    fields=(
        integrations.Field(
            "server_url",
            "Server URL",
            required=True,
            placeholder="ldaps://dc1.example.com",
            help="ldaps:// is preferred. For ldap://, turn on StartTLS.",
        ),
        integrations.Field("starttls", "Use StartTLS", kind="bool", default=False),
        integrations.Field(
            "ca_certificate",
            "CA certificate",
            kind="pem",
            help="The PEM certificate of the CA that signed the directory's certificate. "
            "Leave empty to use this machine's trusted CAs.",
        ),
        integrations.Field(
            "directory_type", "Directory", kind="select", options=(AD, OPENLDAP), default=AD
        ),
        integrations.Field(
            "bind_dn",
            "Service account",
            required=True,
            placeholder="CN=svc-cwcli,OU=Service Accounts,DC=example,DC=com",
            help="A read-only account cwcli signs in as to find users and groups.",
        ),
        integrations.Field(
            "bind_password", "Service account password", kind="secret", required=True
        ),
        integrations.Field("base_dn", "Base DN", required=True, placeholder="DC=example,DC=com"),
        integrations.Field(
            "user_filter",
            "User search filter",
            placeholder="(sAMAccountName={username})",
            help="{username} is replaced by the name typed on the sign-in page.",
        ),
        integrations.Field("username_attribute", "Username attribute"),
        integrations.Field("email_attribute", "Email attribute"),
        integrations.Field("display_name_attribute", "Display name attribute"),
        integrations.Field(
            "group_roles",
            "Allowed groups",
            kind="group_roles",
            required=True,
            help="Only members of these groups can sign in. A group is its name (cn) or "
            "full DN; someone in several gets the highest role.",
        ),
    ),
    normalize=_normalize,
    test=_test,
    actions={"lookup_user": lookup_user},
    on_change=accounts.close_directory_sessions,
    secrets_bound_to=("server_url", "starttls", "ca_certificate", "bind_dn"),
)
