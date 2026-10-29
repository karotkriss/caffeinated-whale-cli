"""Cloudflare DNS: the ``cloudflare`` integration, which provides ``dns_record``.

``core.publish`` writes a published site's A record (and an AAAA record when an
IPv6 address is set) through it. The settings say where records point: this
machine's public address, filled by **Detect** (Cloudflare's own
``/cdn-cgi/trace``, so no third party sees the request) and confirmed by an Admin
when saving. Nothing detects the address while a job runs, because behind NAT, a
VPN or two uplinks the outgoing address can differ from the one visitors reach.

The token is the dashboard's "Edit zone DNS" template with Zone Resources limited
to the zones cwcli may publish in. Its test is read-only (the token is active, and
which zones it can see); Cloudflare has no read that proves a token may EDIT a zone,
so **Check edit access** is the one check that writes: it creates and deletes a TXT
record named ``_cwcli-check.<zone>`` in each zone.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.parse
from typing import Any

from . import integrations, webapi
from .errors import CwcliError, ErrorKind

API = "https://api.cloudflare.com/client/v4"
# Cloudflare's own trace endpoint on an IPv4 address, so the answer is this
# machine's outgoing IPv4 address and no third party is asked.
TRACE = "https://1.1.1.1/cdn-cgi/trace"
CHECK_NAME = "_cwcli-check"
CHECK_CONTENT = "cwcli edit-access check"
_MAX_PAGES = 20
# Cloudflare's "not allowed" codes: 10000 Authentication error, 9109 Unauthorized.
_DENIED = {10000, 9109}


def _denied(e: CwcliError, zone: str) -> CwcliError:
    """A refusal that is about permissions, worded as the fix."""
    if (e.detail or {}).get("status") == 403 or set((e.detail or {}).get("codes") or ()) & _DENIED:
        return CwcliError(
            ErrorKind.PRECONDITION,
            "cloudflare.cannot_edit",
            f"The Cloudflare token cannot edit {zone}: it needs the Zone > DNS > Edit "
            "permission on that zone.",
            detail=e.detail,
        )
    return e


class Cloudflare:
    """The ``dns_record`` hook point over Cloudflare's v4 API."""

    name = "Cloudflare"

    def __init__(self, token: str) -> None:
        self._token = token
        self._zone_names: dict[str, str] = {}  # id -> name, for messages

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        try:
            r = webapi.call(
                method, API + path, headers={"Authorization": f"Bearer {self._token}"}, body=body
            )
        except webapi.UnreachableError as e:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "cloudflare.unreachable",
                f"Cloudflare's API did not answer: {e}",
            ) from None
        answer = r.body if isinstance(r.body, dict) else {}
        if r.status < 300 and answer.get("success"):
            return answer
        errors = [e for e in answer.get("errors") or [] if isinstance(e, dict)]
        text = "; ".join(str(e.get("message")) for e in errors if e.get("message"))
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "cloudflare.refused",
            f"Cloudflare refused: {text or f'HTTP {r.status}'}",
            detail={"status": r.status, "codes": sorted({e.get("code") for e in errors} - {None})},
        )

    def verify(self) -> str:
        """The token's status: ``active``, ``disabled`` or ``expired``."""
        return str((self._call("GET", "/user/tokens/verify").get("result") or {}).get("status"))

    def zones(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            answer = self._call("GET", f"/zones?per_page=50&page={page}")
            out += [
                {"id": str(z["id"]), "name": str(z["name"]), "status": z.get("status")}
                for z in answer.get("result") or []
            ]
            if page >= int((answer.get("result_info") or {}).get("total_pages") or 1):
                break
        self._zone_names.update({z["id"]: z["name"] for z in out})
        return out

    def records(self, zone_id: str, name: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"name": name, "per_page": 100})
        answer = self._call("GET", f"/zones/{_q(zone_id)}/dns_records?{query}")
        return [_record(r) for r in answer.get("result") or []]

    def record(self, zone_id: str, record_id: str) -> dict[str, Any] | None:
        try:
            answer = self._call("GET", f"/zones/{_q(zone_id)}/dns_records/{_q(record_id)}")
        except CwcliError as e:
            if (e.detail or {}).get("status") == 404 or 81044 in (e.detail or {}).get("codes", []):
                return None
            raise
        return _record(answer.get("result") or {})

    def _write(self, method: str, zone_id: str, path: str, body: Any = None) -> dict[str, Any]:
        try:
            return self._call(method, f"/zones/{_q(zone_id)}/dns_records{path}", body)
        except CwcliError as e:
            raise _denied(e, self._zone_names.get(zone_id, "this zone")) from None

    def create(self, zone_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return _record(self._write("POST", zone_id, "", _body(record)).get("result") or {})

    def update(self, zone_id: str, record_id: str, record: dict[str, Any]) -> dict[str, Any]:
        answer = self._write("PATCH", zone_id, f"/{_q(record_id)}", _body(record))
        return _record(answer.get("result") or {})

    def delete(self, zone_id: str, record_id: str) -> None:
        self._write("DELETE", zone_id, f"/{_q(record_id)}")


def _q(part: str) -> str:
    return urllib.parse.quote(str(part), safe="")


def _record(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r.get("id") or ""),
        "type": str(r.get("type") or ""),
        "name": str(r.get("name") or ""),
        "content": str(r.get("content") or ""),
        "proxied": bool(r.get("proxied")),
        "comment": str(r.get("comment") or ""),
    }


def _body(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: record[k] for k in ("type", "name", "content", "proxied", "comment") if k in record}
    out["ttl"] = record.get("ttl", 1)  # 1 is Cloudflare's "automatic"
    return out


# ------------------------------------------------------------------ the contract


def _normalize(settings: dict[str, Any]) -> dict[str, Any]:
    out = {
        "ipv4": str(settings.get("ipv4") or "").strip(),
        "ipv6": str(settings.get("ipv6") or "").strip(),
        "proxied_default": settings.get("proxied_default") is True,
    }
    if out["ipv4"]:
        try:
            out["ipv4"] = str(ipaddress.IPv4Address(out["ipv4"]))
        except ValueError:
            raise CwcliError(
                ErrorKind.USAGE, "cloudflare.ipv4", f"{out['ipv4']!r} is not an IPv4 address."
            ) from None
    if out["ipv6"]:
        try:
            out["ipv6"] = str(ipaddress.IPv6Address(out["ipv6"]))
        except ValueError:
            raise CwcliError(
                ErrorKind.USAGE, "cloudflare.ipv6", f"{out['ipv6']!r} is not an IPv6 address."
            ) from None
    return out


def _test(settings: dict[str, Any], secrets: dict[str, str]) -> integrations.Check:
    token = secrets.get("api_token") or ""
    if not token:
        return integrations.Check(False, "Paste the API token.")
    client = Cloudflare(token)
    try:
        status = client.verify()
    except CwcliError as e:
        return integrations.Check(False, e.message)
    if status != "active":
        return integrations.Check(False, f"The token is {status}; make a new one.")
    try:
        zones = client.zones()
    except CwcliError as e:
        return integrations.Check(False, f"The token is active, but listing zones failed: {e}")
    if not zones:
        return integrations.Check(
            False,
            "The token is active but can see no zone. Give it Zone Resources: Include, "
            "Specific zone, for each zone cwcli may publish in.",
        )
    names = [z["name"] for z in zones]
    return integrations.Check(
        True,
        f"Token is active. It can see {len(names)} zone{'s' if len(names) != 1 else ''}: "
        f"{', '.join(names)}.",
        {"zones": names},
    )


def detect_ip(settings: dict[str, Any], secrets: dict[str, str], args: dict) -> dict:
    """This machine's outgoing IPv4 address as Cloudflare sees it, for the Admin to
    confirm. Saves nothing."""
    try:
        answer = webapi.call("GET", TRACE, timeout=10)
    except webapi.UnreachableError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "cloudflare.trace", f"Cloudflare did not answer: {e}"
        ) from None
    fields = dict(line.split("=", 1) for line in answer.text.splitlines() if "=" in line)
    try:
        return {"ipv4": str(ipaddress.IPv4Address(fields.get("ip", "").strip()))}
    except ValueError:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "cloudflare.trace",
            "Cloudflare's answer named no IPv4 address; type the address instead.",
        ) from None


def check_edit_access(settings: dict[str, Any], secrets: dict[str, str], args: dict) -> dict:
    """Create and delete ``_cwcli-check.<zone>`` (TXT) in each zone the token can see,
    or only ``args['zone']``: the one proof that the token may edit a zone."""
    client = Cloudflare(secrets.get("api_token") or "")
    wanted = str(args.get("zone") or "").strip().lower()
    zones = [z for z in client.zones() if not wanted or z["name"] == wanted]
    if wanted and not zones:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "cloudflare.zone", f"The token cannot see the zone {wanted}."
        )
    out = []
    for zone in zones[:20]:
        name = f"{CHECK_NAME}.{zone['name']}"
        try:
            for stale in client.records(zone["id"], name):
                if stale["type"] == "TXT" and stale["content"].strip('"') == CHECK_CONTENT:
                    client.delete(zone["id"], stale["id"])
            made = client.create(
                zone["id"], {"type": "TXT", "name": name, "content": CHECK_CONTENT, "ttl": 60}
            )
            client.delete(zone["id"], made["id"])
            out.append({"zone": zone["name"], "ok": True, "message": "can edit"})
        except CwcliError as e:
            out.append(
                {"zone": zone["name"], "ok": False, "message": _denied(e, zone["name"]).message}
            )
    return {"zones": out, "checked_at": time.time()}


INTEGRATION = integrations.Integration(
    id="cloudflare",
    name="Cloudflare DNS",
    publisher=integrations.OFFICIAL,
    description="Publishing a site writes its A record in the Cloudflare zone that holds its "
    "name, pointing at this machine.",
    provides=("dns_record",),
    fields=(
        integrations.Field(
            "api_token",
            "API token",
            kind="secret",
            required=True,
            help='Make it from the "Edit zone DNS" template with Zone Resources limited to the '
            "zones cwcli may publish in.",
        ),
        integrations.Field(
            "ipv4",
            "This machine's public IPv4 address",
            required=True,
            placeholder="203.0.113.7",
            help="Where A records point: the address visitors reach this machine at. Detect "
            "reads it from Cloudflare; confirm it before saving.",
        ),
        integrations.Field(
            "ipv6",
            "Public IPv6 address (optional)",
            placeholder="2001:db8::7",
            help="Adds an AAAA record beside each A record.",
        ),
        integrations.Field(
            "proxied_default",
            "Proxy new records through Cloudflare",
            kind="bool",
            default=False,
            help="Offered per site only when Nginx Proxy Manager requests certificates with the "
            "DNS challenge; otherwise every record stays DNS only.",
        ),
    ),
    normalize=_normalize,
    test=_test,
    actions={"detect_ip": detect_ip, "check_edit_access": check_edit_access},
    hooks={"dns_record": lambda settings, secrets: Cloudflare(secrets.get("api_token") or "")},
)
