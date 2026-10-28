"""A small LDAPv3 client on the standard library, for directory sign-in: simple bind,
search, StartTLS and ``ldaps://``. It is deliberately not ``ldap3`` or ``python-ldap``
- the same reasoning as ``core/s3.py``: cwcli ships as a uv tool, and the handful of
operations sign-in needs fit in a few hundred lines of BER over ``socket`` and ``ssl``.

What it speaks (RFC 4511): BindRequest (simple), SearchRequest with a filter parsed
from its RFC 4515 string form (and, or, not, equality, presence, substrings, >=, <=,
~= and extensible match, so Active Directory's ``member:1.2.840.113556.1.4.1941:=``
in-chain rule works), the StartTLS extended operation, and UnbindRequest. Search
references (Active Directory returns them for its partitions) are skipped, never
followed. Certificates are always verified, against the configured CA when one is
given and the system trust store otherwise; there is no switch that turns that off.

``escape`` is the one way a caller puts a user-typed value into a filter.
"""

from __future__ import annotations

import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field

SCOPE_BASE, SCOPE_ONE, SCOPE_SUB = 0, 1, 2
INVALID_CREDENTIALS = 49
NO_SUCH_OBJECT = 32
_STARTTLS_OID = b"1.3.6.1.4.1.1466.20037"
_MAX_MESSAGE = 16 * 1024 * 1024

# Universal and LDAP application tags.
_BOOL, _INT, _OCTETS, _ENUM, _SEQ, _SET = 0x01, 0x02, 0x04, 0x0A, 0x30, 0x31
_BIND_REQ, _BIND_RES, _UNBIND_REQ = 0x60, 0x61, 0x42
_SEARCH_REQ, _SEARCH_ENTRY, _SEARCH_DONE, _SEARCH_REF = 0x63, 0x64, 0x65, 0x73
_EXT_REQ, _EXT_RES = 0x77, 0x78


class LdapError(Exception):
    """A directory answer or transport failure. ``code`` is the LDAP result code, or
    -1 when the server could not be reached or the TLS handshake failed."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Entry:
    dn: str
    attrs: dict[str, list[bytes]] = field(default_factory=dict)  # keys lower-cased

    def values(self, name: str) -> list[str]:
        return [v.decode("utf-8", "replace") for v in self.attrs.get(name.lower(), [])]

    def first(self, name: str) -> str | None:
        vals = self.values(name)
        return vals[0] if vals else None


# --------------------------------------------------------------------------- BER


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int, tag: int = _INT) -> bytes:
    return _tlv(tag, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _octets(value: bytes | str, tag: int = _OCTETS) -> bytes:
    return _tlv(tag, value.encode("utf-8") if isinstance(value, str) else value)


def _bool(value: bool, tag: int = _BOOL) -> bytes:
    return _tlv(tag, b"\xff" if value else b"\x00")


def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """(tag, value, next position) of the TLV at ``pos``."""
    if pos + 2 > len(data):
        raise LdapError(-1, "Malformed answer from the directory (truncated).")
    tag, first = data[pos], data[pos + 1]
    pos += 2
    if first & 0x80:
        n = first & 0x7F
        if not 0 < n <= 4 or pos + n > len(data):
            raise LdapError(-1, "Malformed answer from the directory (length).")
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    else:
        length = first
    end = pos + length
    if end > len(data):
        raise LdapError(-1, "Malformed answer from the directory (truncated).")
    return tag, data[pos:end], end


def _children(value: bytes) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(value):
        tag, inner, pos = _read_tlv(value, pos)
        out.append((tag, inner))
    return out


# ------------------------------------------------------------------- filters


def escape(value: str) -> str:
    """``value`` made safe to sit inside a filter (RFC 4515 section 3)."""
    out = []
    for ch in value:
        if ch in "\\*()\0":
            out.append(f"\\{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def _unescape(raw: str) -> bytes:
    data = raw.encode("utf-8")
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == 0x5C:  # backslash
            if i + 3 > len(data):
                raise LdapError(-2, f"Bad escape in filter value {raw!r}.")
            try:
                out.append(int(data[i + 1 : i + 3].decode("ascii"), 16))
            except ValueError:
                raise LdapError(-2, f"Bad escape in filter value {raw!r}.") from None
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def parse_filter(text: str) -> bytes:
    """The BER encoding of an RFC 4515 filter string. A filter without its outer
    parentheses (``uid=jo``) is accepted and wrapped."""
    text = text.strip()
    if not text.startswith("("):
        text = f"({text})"
    encoded, pos = _parse(text, 0)
    if pos != len(text):
        raise LdapError(-2, f"Unexpected text after the filter: {text[pos:]!r}.")
    return encoded


def _parse(s: str, pos: int) -> tuple[bytes, int]:
    if pos >= len(s) or s[pos] != "(":
        raise LdapError(-2, f"A filter must start with '(' at position {pos}.")
    pos += 1
    if pos >= len(s):
        raise LdapError(-2, "The filter ends early.")
    op = s[pos]
    if op in "&|":
        pos += 1
        parts = []
        while pos < len(s) and s[pos] == "(":
            part, pos = _parse(s, pos)
            parts.append(part)
        if not parts:
            raise LdapError(-2, f"'{op}' needs at least one filter.")
        body = b"".join(parts)
        tag = 0xA0 if op == "&" else 0xA1
    elif op == "!":
        body, pos = _parse(s, pos + 1)
        tag = 0xA2
    else:
        end = s.find(")", pos)
        if end < 0:
            raise LdapError(-2, "The filter is missing a ')'.")
        tag, body = _item(s[pos:end])
        pos = end
    if pos >= len(s) or s[pos] != ")":
        raise LdapError(-2, "The filter is missing a ')'.")
    return _tlv(tag, body), pos + 1


def _item(item: str) -> tuple[int, bytes]:
    for op, tag in (("~=", 0xA8), (">=", 0xA5), ("<=", 0xA6), (":=", 0xA9)):
        left, sep, value = item.partition(op)
        if sep and "=" not in left:
            if tag == 0xA9:
                return tag, _extensible(left, value)
            return tag, _octets(left) + _octets(_unescape(value))
    attr, sep, value = item.partition("=")
    if not sep or not attr:
        raise LdapError(-2, f"Not a filter item: {item!r}.")
    if value == "*":
        return 0x87, attr.encode("utf-8")
    if "*" not in value:
        return 0xA3, _octets(attr) + _octets(_unescape(value))
    pieces = value.split("*")
    subs = b""
    for i, piece in enumerate(pieces):
        if not piece:
            continue
        sub_tag = 0x80 if i == 0 else 0x82 if i == len(pieces) - 1 else 0x81
        subs += _octets(_unescape(piece), sub_tag)
    return 0xA4, _octets(attr) + _tlv(_SEQ, subs)


def _extensible(left: str, value: str) -> bytes:
    parts = left.split(":")
    attr, rest = parts[0], [p for p in parts[1:] if p]
    dn_attrs = any(p.lower() == "dn" for p in rest)
    rules = [p for p in rest if p.lower() != "dn"]
    if len(rules) > 1 or (not attr and not rules):
        raise LdapError(-2, f"Not an extensible match: {left}:={value}.")
    body = _octets(rules[0], 0x81) if rules else b""
    body += _octets(attr, 0x82) if attr else b""
    body += _octets(_unescape(value), 0x83)
    if dn_attrs:
        body += _bool(True, 0x84)
    return body


# -------------------------------------------------------------------- client


def parse_url(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of an ``ldap://`` or ``ldaps://`` URL."""
    u = urllib.parse.urlsplit(url.strip())
    scheme = (u.scheme or "").lower()
    if scheme not in ("ldap", "ldaps") or not u.hostname:
        raise LdapError(-2, f"Not an LDAP server URL: {url!r} (use ldaps://host or ldap://host).")
    try:
        port = u.port or (636 if scheme == "ldaps" else 389)
    except ValueError:
        raise LdapError(-2, f"Not a valid port in {url!r}.") from None
    return scheme, u.hostname, port


class Connection:
    """One connection to a directory. TLS is required: ``ldaps://``, or ``ldap://``
    with ``starttls``; a cleartext connection can be opened only to run StartTLS."""

    def __init__(
        self, url: str, *, starttls: bool = False, ca_pem: str = "", timeout: float = 10.0
    ) -> None:
        scheme, host, port = parse_url(url)
        if scheme == "ldap" and not starttls:
            raise LdapError(
                -2,
                "Sign-in passwords must not cross the network in clear text: use an "
                "ldaps:// URL or turn on StartTLS.",
            )
        self._msg_id = 0
        self._host = host
        try:
            context = ssl.create_default_context(cadata=ca_pem.strip() or None)
        except (ssl.SSLError, ValueError) as e:
            raise LdapError(-2, f"The CA certificate is not valid PEM: {e}") from None
        # Python 3.13 turned on X509_STRICT, which refuses a CA certificate without a
        # key-usage extension - common for an organisation's own directory CA. The
        # chain and the host name are still verified.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        try:
            raw = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise LdapError(-1, f"Cannot reach {host}:{port}: {e}") from None
        self._sock: socket.socket = raw
        try:
            if scheme == "ldaps":
                self._sock = context.wrap_socket(raw, server_hostname=host)
            else:
                self._starttls(context)
        except ssl.SSLError as e:
            raw.close()
            raise LdapError(-1, f"TLS with {host} failed: {e.reason or e}") from None
        except (OSError, LdapError):
            raw.close()
            raise

    # ------------------------------------------------------------ framing
    def _send(self, op: bytes) -> int:
        self._msg_id += 1
        try:
            self._sock.sendall(_tlv(_SEQ, _int(self._msg_id) + op))
        except OSError as e:
            raise LdapError(-1, f"The connection to {self._host} failed: {e}") from None
        return self._msg_id

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except (OSError, ssl.SSLError) as e:
                raise LdapError(-1, f"The connection to {self._host} failed: {e}") from None
            if not chunk:
                raise LdapError(-1, f"{self._host} closed the connection.")
            buf += chunk
        return bytes(buf)

    def _recv(self, expect_id: int) -> tuple[int, bytes]:
        """The next message for ``expect_id``: (protocol op tag, op value)."""
        while True:
            head = self._recv_exact(2)
            if head[1] & 0x80:
                n = head[1] & 0x7F
                if not 0 < n <= 4:
                    raise LdapError(-1, "Malformed answer from the directory (length).")
                length = int.from_bytes(self._recv_exact(n), "big")
            else:
                length = head[1]
            if length > _MAX_MESSAGE:
                raise LdapError(-1, "The directory's answer is too large.")
            body = self._recv_exact(length)
            parts = _children(body)
            if len(parts) < 2 or parts[0][0] != _INT:
                raise LdapError(-1, "Malformed answer from the directory.")
            msg_id = int.from_bytes(parts[0][1], "big", signed=True)
            if msg_id == expect_id:
                return parts[1]
            # msg_id 0 is an unsolicited notice (the server is closing the connection).
            if msg_id == 0:
                raise LdapError(-1, f"{self._host} ended the session.")

    @staticmethod
    def _result(value: bytes) -> tuple[int, str]:
        parts = _children(value)
        if len(parts) < 3:
            raise LdapError(-1, "Malformed result from the directory.")
        code = int.from_bytes(parts[0][1], "big")
        return code, parts[2][1].decode("utf-8", "replace")

    # ---------------------------------------------------------- operations
    def _starttls(self, context: ssl.SSLContext) -> None:
        msg = self._send(_tlv(_EXT_REQ, _octets(_STARTTLS_OID, 0x80)))
        tag, value = self._recv(msg)
        code, diag = self._result(value) if tag == _EXT_RES else (-1, "unexpected answer")
        if code != 0:
            raise LdapError(code, f"{self._host} refused StartTLS: {diag or code}")
        self._sock = context.wrap_socket(self._sock, server_hostname=self._host)

    def bind(self, dn: str, password: str) -> None:
        """A simple bind. An empty password is refused here: RFC 4513 makes a bind with
        a name and no password an *unauthenticated* bind that servers answer success."""
        if not password:
            raise LdapError(INVALID_CREDENTIALS, "A password is required.")
        msg = self._send(_tlv(_BIND_REQ, _int(3) + _octets(dn) + _octets(password, 0x80)))
        tag, value = self._recv(msg)
        if tag != _BIND_RES:
            raise LdapError(-1, "Unexpected answer to a bind.")
        code, diag = self._result(value)
        if code != 0:
            raise LdapError(code, diag or f"bind refused (result {code})")

    def search(
        self,
        base: str,
        filter_text: str,
        *,
        scope: int = SCOPE_SUB,
        attributes: tuple[str, ...] = (),
        size_limit: int = 0,
    ) -> list[Entry]:
        op = (
            _octets(base)
            + _int(scope, _ENUM)
            + _int(0, _ENUM)  # never dereference aliases
            + _int(size_limit)
            + _int(15)  # seconds the server may spend
            + _bool(False)
            + parse_filter(filter_text)
            + _tlv(_SEQ, b"".join(_octets(a) for a in attributes))
        )
        msg = self._send(_tlv(_SEARCH_REQ, op))
        entries: list[Entry] = []
        while True:
            tag, value = self._recv(msg)
            if tag == _SEARCH_ENTRY:
                parts = _children(value)
                attrs: dict[str, list[bytes]] = {}
                for _t, attr in _children(parts[1][1]) if len(parts) > 1 else []:
                    pair = _children(attr)
                    if len(pair) == 2:
                        name = pair[0][1].decode("utf-8", "replace").lower()
                        attrs[name] = [v for _vt, v in _children(pair[1][1])]
                entries.append(Entry(parts[0][1].decode("utf-8", "replace"), attrs))
            elif tag == _SEARCH_REF:
                continue
            elif tag == _SEARCH_DONE:
                code, diag = self._result(value)
                if code != 0:
                    raise LdapError(code, diag or f"search failed (result {code})")
                return entries
            else:
                raise LdapError(-1, "Unexpected answer to a search.")

    def close(self) -> None:
        try:
            self._send(_tlv(_UNBIND_REQ, b""))
        except LdapError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
