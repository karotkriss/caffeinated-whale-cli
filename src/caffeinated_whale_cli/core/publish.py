"""Publishing a site at its public name, through the ``dns_record`` and ``proxy_host``
integrations (``core.cloudflare``, ``core.npm``, or community ones).

``publish_site`` runs four steps in order, each reading what exists before it writes,
so running it again (Retry) repeats only what is missing and the same call publishes
a site that already exists:

1. the DNS record (``dns_record``): an A record at the site's name pointing at the
   address the DNS integration's settings name (or this site's own override), plus an
   AAAA record when an IPv6 address is set;
2. the proxy host (``proxy_host``): the site's name forwarded to the bench's
   published web port, with a ``/socket.io`` location on its socket.io port;
3. the certificate: Let's Encrypt through the proxy, last because Let's Encrypt
   looks the name up and it is the step most likely to fail;
4. the check: ``https://<site>/api/method/ping`` answers Frappe's ``pong``
   (``http://<site>:<web port>`` when there is no proxy).

A step that fails stops the run; the steps after it wait. The site itself is never
touched, so a failed publish never removes it. Either integration works alone: with
DNS only the site answers at its bench port, and with the proxy only the name must
already point at this machine.

What cwcli created is recorded in ``<cwcli home>/publications.json`` (keyed by the
public name, so one name is published in one place), next to what it wrote. Only
an object named there is ever changed or deleted, and only while its live state
still matches what cwcli wrote: anything changed by hand is left and reported.
``check_site`` compares the two without writing anything (drift: a record pointed
elsewhere, a proxy host deleted, a certificate whose renewal is failing).
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..utils import config_utils, shared_home
from . import docker as core_docker
from . import integrations, resolvers, webapi
from .envelope import Message, Result, Status
from .errors import CwcliError, ErrorKind

OnEvent = Callable[[Any], None]
PARTS = ("dns", "proxy", "certificate", "verify")
TITLES = {
    "dns": "DNS record",
    "proxy": "Proxy host",
    "certificate": "Certificate",
    "verify": "Check the site answers",
}
# Nginx Proxy Manager renews 30 days before expiry; fewer days left means it failed.
RENEWAL_FAILING_DAYS = 21
_WAITING = "Waits for the step before it."


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishStepStart:
    phase: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishStepEnd:
    phase: str
    status: str = "ok"  # ok | failed | skipped
    message: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishNotice:
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishReport:
    ok: bool
    domain: str
    url: str | None
    parts: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class UnpublishReport:
    ok: bool
    domain: str
    removed: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- the store


def store_path() -> Path:
    return config_utils.cwcli_home() / "publications.json"


def _load() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "sites": {}}
    except (OSError, ValueError) as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "publications.unreadable", f"{store_path()} is unreadable: {e}"
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("sites"), dict):
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "publications.unreadable",
            f"{store_path()} is not a publications store",
        )
    return data


def _put(domain: str, rec: dict | None) -> None:
    """Write one site's record (None removes it) under the store's file lock."""
    from .schedules import StoreLock

    path = store_path()
    with StoreLock(path):
        data = _load()
        if rec is None:
            data["sites"].pop(domain, None)
        else:
            data["sites"][domain] = rec
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, shared_home.file_mode(0o600))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        os.chmod(path, shared_home.file_mode(0o600))
        shared_home.apply_group(path)


def entries() -> dict[str, dict]:
    return dict(_load()["sites"])


def entry(domain: str) -> dict | None:
    return entries().get(domain)


def entry_at(domain: str, project: str | None, bench: str | None) -> dict | None:
    """``domain``'s publication only when it is published from ``project``'s ``bench``:
    a same-named copy elsewhere is not the published site."""
    rec = entry(domain)
    if project is None or rec is None or rec.get("project") != project:
        return None
    try:
        found = resolvers.resolve_bench(project, bench, None)
    except CwcliError:
        return None
    if found is None or found.status is not Status.OK:
        return None
    return rec if found.data == rec.get("bench_path") else None


def view() -> dict[str, Any]:
    """What the Sites list shows: every published site's parts, and which DNS and
    proxy integrations are on (no secret, and no call to either)."""

    def named(hook: str) -> dict | None:
        found = integrations.serving(hook)
        return {"id": found[0], "name": found[1]} if found else None

    return {"sites": entries(), "dns": named("dns_record"), "proxy": named("proxy_host")}


# ------------------------------------------------------------------- helpers


def check_domain(domain: str) -> None:
    """A public name: a lowercase hostname with a dot, not a ``.localhost`` one."""
    resolvers.validate_site_name(domain)
    labels = domain.split(".")
    if (
        domain != domain.lower()
        or len(labels) < 2
        or domain.endswith(".localhost")
        or not labels[-1].isalpha()
        or any(not label or len(label) > 63 for label in labels)
    ):
        raise CwcliError(
            ErrorKind.USAGE,
            "publish.name",
            f"{domain} is not a public name; a published site's name is its domain, like "
            "erp.example.com (a .localhost site cannot be published).",
        )


def _zone_for(zones: list[dict], domain: str) -> dict | None:
    """The zone holding ``domain``: the longest zone name it ends in."""
    held = [z for z in zones if domain == z["name"] or domain.endswith("." + z["name"])]
    return max(held, key=lambda z: len(z["name"]), default=None)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _proxy_allows_proxied(proxy_client: Any) -> bool:
    return getattr(proxy_client, "challenge", "http") == "dns"


def _addresses(prior: dict, dns: tuple | None, address: str | None) -> tuple[str, str, str]:
    """The site's address override (this call's, else the one it was published with)
    and the IPv4 and IPv6 addresses its records point at."""
    override = (address if address is not None else prior.get("address_override") or "").strip()
    settings = dns[1] if dns else {}
    ipv4 = override or str(settings.get("ipv4") or "").strip()
    ipv6 = "" if override else str(settings.get("ipv6") or "").strip()
    return override, ipv4, ipv6


def _proxied(prior: dict, dns: tuple | None, proxy_client: Any, proxied: bool | None) -> tuple:
    """Whether the site wants its record proxied (this call's choice, else the one it
    was published with, else the DNS integration's default), and whether it may be."""
    if proxied is None:
        proxied = (
            prior["proxied"] if "proxied" in prior else bool(dns and dns[1].get("proxied_default"))
        )
    return bool(proxied), proxy_client is not None and _proxy_allows_proxied(proxy_client)


def _dns_decide(
    live_all: list[dict],
    held: dict[str, dict],
    rtype: str,
    content: str,
    proxied: bool,
    domain: str,
    zone_name: str,
    client_name: str,
) -> tuple[str, dict | None]:
    """What one wanted record needs: ``keep`` or ``update`` cwcli's own record,
    ``adopt`` an identical one cwcli did not create, or ``create`` it. Raises a
    ``CONFLICT`` for a name that is taken or a record of cwcli's changed by hand.
    Shared by the preflight and the DNS step, so both decide alike."""
    if any(r["type"] == "CNAME" for r in live_all):
        raise CwcliError(
            ErrorKind.CONFLICT,
            "publish.dns_taken",
            f"{domain} is a CNAME in {zone_name}; remove it in {client_name} or choose "
            "another name.",
        )
    live = [r for r in live_all if r["type"] == rtype]
    mine = next((r for r in live if r["id"] in held), None)
    if mine is not None:
        wrote = held[mine["id"]]
        if (mine["content"], mine["proxied"]) == (content, proxied):
            return "keep", mine
        if (mine["content"], mine["proxied"]) == (wrote["content"], wrote["proxied"]):
            return "update", mine
        raise CwcliError(
            ErrorKind.CONFLICT,
            "publish.dns_changed",
            f"The {rtype} record for {domain} was changed outside cwcli (it points "
            f"at {mine['content']} now); cwcli leaves it. Change it back to "
            f"{wrote['content']} or delete it, then retry.",
        )
    if not live:
        return "create", None
    if len(live) == 1 and (live[0]["content"], live[0]["proxied"]) == (content, proxied):
        return "adopt", live[0]
    raise CwcliError(
        ErrorKind.CONFLICT,
        "publish.dns_taken",
        f"{domain} already has an {rtype} record "
        f"({', '.join(r['content'] for r in live)}) that cwcli did not create; "
        f"remove it in {client_name} or choose another name.",
    )


def _held(part: dict, zone_id: str) -> dict[str, dict]:
    """The records cwcli created in ``zone_id``, by id."""
    return {
        r["id"]: r
        for r in part.get("records") or []
        if r.get("owned") and part.get("zone_id") == zone_id
    }


@dataclass(slots=True)
class _Run:
    domain: str
    rec: dict
    address: str
    address6: str
    proxied: bool
    access_list_id: int
    dns: tuple | None
    proxy: tuple | None
    proxy_client: Any = None
    emit: OnEvent = lambda _e: None


def _host_spec(run: _Run) -> dict[str, Any]:
    settings = run.proxy[1] if run.proxy else {}
    host = settings.get("forward_host")
    return {
        "domain_names": [run.domain],
        "forward_scheme": "http",
        "forward_host": host,
        "forward_port": run.rec["web_port"],
        "allow_websocket_upgrade": True,
        "block_exploits": settings.get("block_exploits", True) is not False,
        "access_list_id": run.access_list_id,
        "locations": [
            {
                "path": "/socket.io",
                "forward_scheme": "http",
                "forward_host": host,
                "forward_port": run.rec["socketio_port"],
            }
        ],
    }


def _recorded_forwarding(part: dict) -> tuple:
    """``_forwarding`` of the host as cwcli last wrote it."""
    host = part.get("forward_host")
    return _forwarding(
        {
            "forward_host": host,
            "forward_port": part.get("forward_port"),
            "access_list_id": part.get("access_list_id"),
            "locations": [
                {
                    "path": "/socket.io",
                    "forward_host": host,
                    "forward_port": part.get("socketio_port"),
                }
            ],
        }
    )


def _forwarding_changes(live: dict, part: dict) -> list[str]:
    """How the live proxy host differs from what cwcli last wrote (empty: unchanged)."""
    now, was = _forwarding(live), _recorded_forwarding(part)
    changed = []
    if now[:2] != was[:2]:
        changed.append(f"it forwards to {now[0]}:{now[1]}, not {was[0]}:{was[1]}")
    if now[2] != was[2]:
        changed.append(f"its access list is {now[2] or 'none'}, not {was[2] or 'none'}")
    if now[3] != was[3]:
        changed.append("its /socket.io location was changed")
    return changed


def _forwarding(host: dict) -> tuple:
    locations = tuple(
        (loc.get("path"), loc.get("forward_host"), int(loc.get("forward_port") or 0))
        for loc in host.get("locations") or []
    )
    return (
        host.get("forward_host"),
        int(host.get("forward_port") or 0),
        int(host.get("access_list_id") or 0),
        locations,
    )


# ------------------------------------------------------------------ the steps


def _dns_step(run: _Run, part: dict) -> dict:
    if run.dns is None:
        return {
            "state": "off",
            "message": "No DNS integration is on; the name must already " "point at this machine.",
        }
    integ, settings, secrets = run.dns
    client = integ.hooks["dns_record"](settings, secrets)
    if not run.address:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "publish.no_address",
            f"Set this machine's public IPv4 address in the {integ.name} integration first.",
        )
    zone = _zone_for(client.zones(), run.domain)
    if zone is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "publish.no_zone",
            f"No zone the {client.name} token can see holds {run.domain}; add the zone to the "
            "token's Zone Resources or choose a name in one of its zones.",
        )
    wanted = [("A", run.address)] + ([("AAAA", run.address6)] if run.address6 else [])
    held = _held(part, zone["id"])
    live_all = client.records(zone["id"], run.domain)
    comment = f"cwcli {socket.gethostname()} {run.rec['bench']} {run.domain}"[:100]
    out: list[dict] = []

    def remember(record: dict) -> None:
        # Recorded as soon as it is written, so a later write that fails keeps it named.
        part.update(provider=integ.id, zone_id=zone["id"], zone=zone["name"])
        part["records"] = [r for r in part.get("records") or [] if r["id"] != record["id"]]
        part["records"].append(record)

    adopted = False
    for rtype, content in wanted:
        action, found = _dns_decide(
            live_all, held, rtype, content, run.proxied, run.domain, zone["name"], client.name
        )
        desired = {
            "type": rtype,
            "name": run.domain,
            "content": content,
            "proxied": run.proxied,
            "comment": comment,
        }
        if action == "adopt":
            out.append({**(found or {}), "owned": False})
            adopted = True
            continue
        if action == "keep":
            kept = found or {}
        elif action == "update":
            kept = client.update(zone["id"], (found or {})["id"], desired)
        else:
            kept = client.create(zone["id"], desired)
        out.append({**kept, "owned": True})
        remember(out[-1])
    types = {rtype for rtype, _ in wanted}
    for rid, wrote in held.items():
        # A record cwcli made that is no longer wanted (its IPv6 address was cleared):
        # deleted while it still matches what cwcli wrote, else kept named and left.
        if wrote["type"] in types:
            continue
        live = next((r for r in live_all if r["id"] == rid), None)
        if live is None:
            continue
        if (live["content"], live["proxied"]) == (wrote["content"], wrote["proxied"]):
            client.delete(zone["id"], rid)
            part["records"] = [r for r in part.get("records") or [] if r["id"] != rid]
        else:
            out.append(wrote)
    mode = "proxied" if run.proxied else "DNS only"
    message = f"{run.domain} points at {run.address} ({mode})"
    if adopted:
        message += "; the record was already there, so Unpublish leaves it"
    return {
        "state": "done",
        "provider": integ.id,
        "zone_id": zone["id"],
        "zone": zone["name"],
        "records": out,
        "message": message,
    }


def _proxy_step(run: _Run, part: dict) -> dict:
    if run.proxy is None:
        return {
            "state": "off",
            "message": "No proxy integration is on; the site answers at " "its bench port.",
        }
    client = run.proxy_client
    me = client.me()
    spec = _host_spec(run)
    live = client.host(part["id"]) if part.get("id") else None
    if live is None:
        live = next(
            (h for h in client.hosts() if run.domain in (h.get("domain_names") or [])), None
        )
        if live is not None and live.get("owner_user_id") not in (None, me.get("id")):
            raise CwcliError(
                ErrorKind.CONFLICT,
                "publish.proxy_taken",
                f"{run.domain} is already used by a proxy host another {client.name} user "
                "made; cwcli leaves it.",
            )
    if live is None:
        try:
            live = client.create_host(spec)
        except CwcliError as e:
            if "already in use" in e.message:
                raise CwcliError(
                    ErrorKind.CONFLICT,
                    "publish.proxy_taken",
                    f"{run.domain} is already in use in {client.name}, by a host cwcli cannot "
                    "see. Remove it there or choose another name.",
                ) from None
            raise
    elif _forwarding(live) != _forwarding(spec):
        changed = _forwarding_changes(live, part)
        if part.get("id") == live["id"] and changed:
            raise CwcliError(
                ErrorKind.CONFLICT,
                "publish.proxy_changed",
                f"The proxy host for {run.domain} was changed outside cwcli "
                f"({'; '.join(changed)}); cwcli leaves it. Change it back or delete it in "
                f"{client.name}, then retry.",
            )
        live = client.update_host(live["id"], spec)
    return {
        "state": "done",
        "provider": run.proxy[0].id,
        "id": int(live["id"]),
        "owned": True,
        "forward_host": spec["forward_host"],
        "forward_port": spec["forward_port"],
        "socketio_port": run.rec["socketio_port"],
        "access_list_id": spec["access_list_id"],
        "message": f"{run.domain} forwards to {spec['forward_host']}:{spec['forward_port']} "
        f"(socket.io :{run.rec['socketio_port']})",
    }


def _certificate_step(run: _Run, part: dict) -> dict:
    if run.proxy is None:
        return {"state": "off", "message": "A certificate needs a proxy integration."}
    client = run.proxy_client
    host_id = run.rec["parts"]["proxy"]["id"]
    cert = client.certificate(part["id"]) if part.get("id") else None
    if cert is not None and run.domain not in (cert.get("domain_names") or []):
        cert = None
    owned = bool(part.get("owned", True))
    if cert is None:
        cert = next(
            (
                c
                for c in client.certificates()
                if c.get("provider") == "letsencrypt" and c.get("domain_names") == [run.domain]
            ),
            None,
        )
        owned = cert is None or cert.get("owner_user_id") in (None, client.me().get("id"))
    if cert is None:
        how = "the DNS challenge" if client.challenge == "dns" else "the HTTP challenge"
        run.emit(
            PublishNotice(
                text=f"Asking Let's Encrypt for a certificate ({how}); this can " "take a minute."
            )
        )
        try:
            cert = client.request_certificate(run.domain)
        except CwcliError as e:
            if client.challenge == "dns":
                fix = "check the DNS-challenge token can edit the zone"
            else:
                fix = (
                    f"is port 80 open to the internet, and does {run.domain} point at this "
                    "machine?"
                )
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "publish.certificate",
                f"Let's Encrypt could not validate {run.domain} through {how}: {fix} "
                f"({e.message})",
            ) from None
    settings = run.proxy[1]
    tls = {
        "certificate_id": int(cert["id"]),
        "ssl_forced": settings.get("ssl_forced", True) is not False,
        "http2_support": settings.get("http2", True) is not False,
        "hsts_enabled": settings.get("hsts") is True,
    }
    host = client.host(host_id) or {}
    if any(host.get(k) != v for k, v in tls.items()):
        client.update_host(host_id, tls)
    expires = _parse_time(cert.get("expires_on"))
    return {
        "state": "done",
        "id": int(cert["id"]),
        "owned": owned,
        "expires_on": cert.get("expires_on"),
        "challenge": client.challenge,
        "message": "Let's Encrypt certificate"
        + (f", valid until {expires:%Y-%m-%d}" if expires else ""),
    }


def _verify_step(run: _Run, part: dict) -> dict:
    url = f"{run.rec['url']}/api/method/ping"
    try:
        r = webapi.call("GET", url, timeout=15)
    except webapi.UnreachableError as e:
        raise CwcliError(
            ErrorKind.PRECONDITION, "publish.unreachable", f"{url} did not answer: {e}"
        ) from None
    if r.status != 200 or not isinstance(r.body, dict) or r.body.get("message") != "pong":
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "publish.unreachable",
            f"{url} answered HTTP {r.status}, not Frappe's pong.",
        )
    return {"state": "done", "message": f"{run.rec['url']} answers"}


_STEPS = {
    "dns": _dns_step,
    "proxy": _proxy_step,
    "certificate": _certificate_step,
    "verify": _verify_step,
}


# ----------------------------------------------------------------- the verbs


def publish_site(
    project: str,
    site: str,
    *,
    bench: str | None = None,
    address: str | None = None,
    proxied: bool | None = None,
    access_list_id: int | None = None,
    on_event: OnEvent | None = None,
) -> Result[PublishReport]:
    """Publish ``site`` (its name is its public domain). Idempotent: see the module
    docstring. The report's ``ok`` is False when any step failed."""
    emit = on_event or (lambda _e: None)
    check_domain(site)
    dns = integrations.provider("dns_record")
    proxy = integrations.provider("proxy_host")
    if dns is None and proxy is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "publish.no_integration",
            "Turn on Cloudflare DNS or Nginx Proxy Manager in Settings > Integrations first.",
        )
    resolved = resolvers.resolve_container_and_bench(project, bench, None)
    if isinstance(resolved, Result):
        return resolved
    container, bench_path, warnings = resolved
    resolvers.require_site_dir(container, bench_path, site)
    ports = resolvers.resolve_host_ports(container, bench_path)
    if ports is None:
        raise CwcliError(
            ErrorKind.PRECONDITION,
            "publish.port_unknown",
            f"cwcli cannot tell which host ports bench {bench_path} is published on, so "
            "nothing would reach it.",
            hint=f"Run cwcli scale {project} if the bench has no published port.",
        )
    # A choice this call does not make is the one the site was published with, so a
    # Retry or a Move keeps the site's own address, proxy status and access list.
    prior = entry(site) or {}
    proxy_client = proxy[0].hooks["proxy_host"](proxy[1], proxy[2]) if proxy else None
    override, ipv4, ipv6 = _addresses(prior, dns, address)
    wants_proxied, allowed = _proxied(prior, dns, proxy_client, proxied)
    if access_list_id is None:
        access_list_id = int(prior.get("access_list_id") or 0)
    if wants_proxied and not allowed:
        warnings.append(
            Message(
                "publish.proxied_refused",
                "Proxied needs Nginx Proxy Manager with the DNS challenge; the record stays "
                "DNS only.",
            )
        )
    now = time.time()
    rec: dict[str, Any] = {
        "domain": site,
        "project": project,
        "bench_path": bench_path,
        "bench": bench_path.rstrip("/").rsplit("/", 1)[-1],
        "web_port": ports[0],
        "socketio_port": ports[1],
        "address": ipv4,
        "address_override": override,
        "proxied": wants_proxied and allowed,
        "access_list_id": int(access_list_id or 0),
        "url": f"https://{site}" if proxy else f"http://{site}:{ports[0]}",
        "parts": dict(prior.get("parts") or {}),
        "published_at": prior.get("published_at") or now,
        "updated_at": now,
    }
    run = _Run(
        domain=site,
        rec=rec,
        address=rec["address"],
        address6=ipv6,
        proxied=rec["proxied"],
        access_list_id=rec["access_list_id"],
        dns=dns,
        proxy=proxy,
        proxy_client=proxy_client,
        emit=emit,
    )
    failed: str | None = None
    ran: set[str] = set()
    for name in PARTS:
        prior_part = dict(rec["parts"].get(name) or {})
        off = (name == "dns" and dns is None) or (name in ("proxy", "certificate") and not proxy)
        if failed and not off:
            rec["parts"][name] = {**prior_part, "state": "waiting", "message": _WAITING}
            continue
        if not off:  # a part no integration serves is recorded, not shown as a step
            emit(PublishStepStart(phase=name, message=TITLES[name]))
        # Laid over what was recorded, so an object cwcli created stays named (and
        # removable by Unpublish) even while its integration is off.
        ran.add(name)
        try:
            part = {**prior_part, **_STEPS[name](run, prior_part)}
        except CwcliError as e:
            failed = name
            part = {**prior_part, "state": "failed", "message": e.message}
        rec["parts"][name] = {**part, "at": time.time()}
        _put(site, rec)
        if not off:
            status = "ok" if part["state"] == "done" else "failed"
            emit(PublishStepEnd(phase=name, status=status, message=part["message"]))
    _put(site, rec)
    # Only an integration this run called: one whose steps waited keeps its last outcome.
    for found, parts in ((dns, ("dns",)), (proxy, ("proxy", "certificate"))):
        if found is not None and ran.intersection(parts):
            errors = [
                rec["parts"][p]["message"]
                for p in parts
                if p in ran and rec["parts"][p]["state"] == "failed"
            ]
            integrations.record_outcome(found[0].id, errors[0] if errors else None)
    report = PublishReport(ok=failed is None, domain=site, url=rec["url"], parts=rec["parts"])
    return Result(status=Status.OK, data=report, warnings=warnings)


def unpublish_site(domain: str, *, on_event: OnEvent | None = None) -> Result[UnpublishReport]:
    """Remove what cwcli created for ``domain``: the proxy host, then the certificate
    when no other host uses it, then the DNS records. Each only while it still matches
    what cwcli wrote; anything else is left and reported. A part that cannot be
    removed now (its integration off, its service unreachable) stays recorded, so a
    retry finishes it."""
    emit = on_event or (lambda _e: None)
    rec = entry(domain)
    if rec is None:
        return Result(status=Status.OK, data=UnpublishReport(ok=True, domain=domain))
    parts = {k: dict(v) for k, v in (rec.get("parts") or {}).items()}
    report = UnpublishReport(ok=True, domain=domain)
    proxy = integrations.provider("proxy_host")
    dns = integrations.provider("dns_record")

    def client_for(found: tuple | None, hook: str, part: dict):
        if found is None or found[0].id != part.get("provider", found[0].id):
            made_by = part.get("provider") or ""
            known = integrations.official().get(made_by)
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "publish.integration_off",
                f"Turn {known.name if known else made_by or 'its integration'} back on so cwcli "
                "can remove what it made.",
            )
        return found[0].hooks[hook](found[1], found[2])

    def step(name: str, title: str, fn: Callable[[], str | None]) -> bool:
        emit(PublishStepStart(phase=name, message=title))
        try:
            left = fn()
        except CwcliError as e:
            report.failed.append(f"{title}: {e.message}")
            emit(PublishStepEnd(phase=name, status="failed", message=e.message))
            return False
        if left:
            report.left.append(f"{title}: {left}")
        else:
            report.removed.append(title)
        parts.pop(name, None)
        emit(PublishStepEnd(phase=name, status="ok", message=left or f"{title} removed"))
        return True

    proxy_part, cert_part, dns_part = (parts.get(k) or {} for k in ("proxy", "certificate", "dns"))
    host_gone = True

    def remove_host() -> str | None:
        nonlocal host_gone
        client = client_for(proxy, "proxy_host", proxy_part)
        live = client.host(proxy_part["id"])
        if live is None:
            return None
        if _forwarding_changes(live, proxy_part) or live.get("domain_names") != [domain]:
            host_gone = False
            return "changed in the proxy since cwcli made it, so it is left as it is"
        client.delete_host(proxy_part["id"])
        return None

    def remove_certificate() -> str | None:
        client = client_for(proxy, "proxy_host", proxy_part or cert_part)
        if client.certificate(cert_part["id"]) is None:
            return None
        users = [
            h for h in client.hosts() if int(h.get("certificate_id") or 0) == int(cert_part["id"])
        ]
        if users or not host_gone:
            return "still used by a proxy host, so it is left"
        client.delete_certificate(cert_part["id"])
        return None

    def remove_records() -> str | None:
        client = client_for(dns, "dns_record", dns_part)
        left = []
        for wrote in [r for r in dns_part.get("records") or [] if r.get("owned")]:
            live = client.record(dns_part["zone_id"], wrote["id"])
            if live is None:
                continue
            if (live["content"], live["proxied"]) != (wrote["content"], wrote["proxied"]):
                left.append(f"the {wrote['type']} record now points at {live['content']}")
                continue
            client.delete(dns_part["zone_id"], wrote["id"])
        return "; ".join(left) + ", so it is left" if left else None

    host_removed = True
    if proxy_part.get("id") and proxy_part.get("owned"):
        host_removed = step("proxy", "Proxy host", remove_host)
    # A host that could not be removed still uses its certificate: keep it recorded,
    # so the retry that removes the host removes the certificate too.
    if cert_part.get("id") and cert_part.get("owned") and host_removed:
        step("certificate", "Certificate", remove_certificate)
    if any(r.get("owned") for r in dns_part.get("records") or []):
        step("dns", "DNS record", remove_records)
    if report.failed:
        _put(domain, {**rec, "parts": parts, "updated_at": time.time()})
    else:
        _put(domain, None)
    warnings = [Message("publish.left", text) for text in report.left]
    done = UnpublishReport(
        ok=not report.failed,
        domain=domain,
        removed=report.removed,
        left=report.left,
        failed=report.failed,
    )
    return Result(status=Status.OK, data=done, warnings=warnings)


def check_site(domain: str, *, now: float | None = None) -> dict[str, Any]:
    """Read-only: compare what cwcli recorded for ``domain`` with what is live, part by
    part (``ok``, ``drift`` or ``unknown``). Never writes or repairs anything."""
    rec = entry(domain)
    if rec is None:
        raise CwcliError(
            ErrorKind.NOT_FOUND, "publish.not_published", f"{domain} is not published."
        )
    now = time.time() if now is None else now
    parts = rec.get("parts") or {}
    out: dict[str, dict] = {}

    def check(name: str, hook: str, fn: Callable[[Any, dict], tuple[str, str]]) -> None:
        part = parts.get(name) or {}
        if part.get("state") not in ("done",):
            out[name] = {"state": "unknown", "message": "Not published yet."}
            return
        found = integrations.provider(hook)
        if found is None:
            out[name] = {"state": "unknown", "message": "Its integration is off."}
            return
        try:
            state, message = fn(found[0].hooks[hook](found[1], found[2]), part)
        except CwcliError as e:
            state, message = "unknown", e.message
        out[name] = {"state": state, "message": message}

    def dns(client, part) -> tuple[str, str]:
        drifts = []
        for wrote in part.get("records") or []:
            live = client.record(part["zone_id"], wrote["id"])
            if live is None:
                drifts.append(f"the {wrote['type']} record was deleted")
            elif live["content"] != wrote["content"]:
                drifts.append(
                    f"the {wrote['type']} record points at {live['content']}, not "
                    f"{wrote['content']}"
                )
            elif live["proxied"] != wrote["proxied"]:
                drifts.append(f"the {wrote['type']} record's proxy status was changed")
        return ("drift", "; ".join(drifts)) if drifts else ("ok", "Records match.")

    def proxy(client, part) -> tuple[str, str]:
        live = client.host(part["id"])
        if live is None:
            return "drift", "The proxy host was deleted."
        changed = _forwarding_changes(live, part)
        if changed:
            return "drift", f"The proxy host was changed: {'; '.join(changed)}."
        return "ok", "The proxy host matches."

    def certificate(client, part) -> tuple[str, str]:
        live = client.certificate(part["id"])
        if live is None:
            return "drift", "The certificate was deleted."
        expires = _parse_time(live.get("expires_on"))
        if expires is None:
            return "unknown", "The certificate's expiry is unreadable."
        days = (expires.timestamp() - now) / 86400
        if days < 0:
            return "drift", f"The certificate expired on {expires:%Y-%m-%d}."
        if days < RENEWAL_FAILING_DAYS:
            return "drift", (
                f"The certificate expires on {expires:%Y-%m-%d}; it renews 30 days ahead, so "
                "its renewal is failing."
            )
        return "ok", f"Valid until {expires:%Y-%m-%d}."

    check("dns", "dns_record", dns)
    check("proxy", "proxy_host", proxy)
    check("certificate", "proxy_host", certificate)
    return {
        "domain": domain,
        "ok": all(p["state"] != "drift" for p in out.values()),
        "parts": out,
        "checked_at": now,
    }


# ------------------------------------------------------------- for New site


def options() -> dict[str, Any]:
    """What New site's Publish section offers: the zones the DNS token can see, the
    address records point at, the proxy's challenge and access lists."""
    out: dict[str, Any] = {"dns": None, "proxy": None, "proxied_allowed": False}
    dns = integrations.provider("dns_record")
    proxy = integrations.provider("proxy_host")
    if dns is not None:
        integ, settings, secrets = dns
        d: dict[str, Any] = {
            "id": integ.id,
            "name": integ.name,
            "ipv4": settings.get("ipv4") or "",
            "ipv6": settings.get("ipv6") or "",
            "proxied_default": bool(settings.get("proxied_default")),
            "zones": [],
            "error": None,
        }
        try:
            d["zones"] = sorted(
                z["name"] for z in integ.hooks["dns_record"](settings, secrets).zones()
            )
        except CwcliError as e:
            d["error"] = e.message
        out["dns"] = d
    if proxy is not None:
        integ, settings, secrets = proxy
        client = integ.hooks["proxy_host"](settings, secrets)
        p: dict[str, Any] = {
            "id": integ.id,
            "name": integ.name,
            "challenge": getattr(client, "challenge", "http"),
            "access_lists": [],
            "error": None,
        }
        lister = getattr(client, "access_lists", None)
        if lister is not None:
            try:
                p["access_lists"] = lister()
            except CwcliError as e:
                p["error"] = e.message
        out["proxy"] = p
        out["proxied_allowed"] = dns is not None and _proxy_allows_proxied(client)
    return out


def preflight(
    project: str,
    bench: str | None,
    domain: str,
    *,
    address: str | None = None,
    proxied: bool | None = None,
) -> list[dict[str, Any]]:
    """Read-only checks before a site is created and published: one row per part,
    ``ok``, ``warn`` or ``fail``. A ``fail`` refuses the publish before the site
    exists."""
    rows: list[dict[str, Any]] = []

    def row(part: str, state: str, text: str, **detail: Any) -> None:
        rows.append({"part": part, "state": state, "text": text, **detail})

    try:
        check_domain(domain)
    except CwcliError as e:
        row("name", "fail", e.message)
        return rows
    dns = integrations.provider("dns_record")
    proxy = integrations.provider("proxy_host")
    if dns is None and proxy is None:
        row("name", "fail", "Turn on Cloudflare DNS or Nginx Proxy Manager first.")
        return rows
    recorded = entry(domain) or {}
    try:
        container = core_docker.get_frappe_container(project)
        if getattr(container, "status", "running") != "running":
            row("port", "warn", f"The bench's port is read once {project} starts.")
        else:
            bench_result = resolvers.resolve_bench(project, bench, None)
            bench_path = (
                bench_result.data
                if bench_result is not None and bench_result.status is not Status.NEEDS_CHOICE
                else resolvers.DEFAULT_BENCH_PATH
            )
            ports = resolvers.resolve_host_ports(container, str(bench_path))
            if ports is None:
                row(
                    "port",
                    "fail",
                    "cwcli cannot tell which host port this bench is published on; "
                    f"cwcli scale {project} widens the published range.",
                )
            else:
                row(
                    "port",
                    "ok",
                    f"The bench is published on port {ports[0]} (socket.io {ports[1]}).",
                    port=ports[0],
                )
    except CwcliError as e:
        row("port", "fail", e.message)
    if dns is not None:
        integ, settings, secrets = dns
        _override, target, target6 = _addresses(recorded, dns, address)
        proxy_client = proxy[0].hooks["proxy_host"](proxy[1], proxy[2]) if proxy else None
        wants_proxied, allowed = _proxied(recorded, dns, proxy_client, proxied)
        try:
            client = integ.hooks["dns_record"](settings, secrets)
            zone = _zone_for(client.zones(), domain)
            if not target:
                row("dns", "fail", f"Set this machine's public IPv4 address in {integ.name}.")
            elif zone is None:
                row("dns", "fail", f"No zone the {client.name} token can see holds {domain}.")
            else:
                held = _held((recorded.get("parts") or {}).get("dns") or {}, zone["id"])
                live = client.records(zone["id"], domain)
                wanted = [("A", target)] + ([("AAAA", target6)] if target6 else [])
                actions = {
                    _dns_decide(
                        live,
                        held,
                        rtype,
                        content,
                        wants_proxied and allowed,
                        domain,
                        zone["name"],
                        client.name,
                    )[0]
                    for rtype, content in wanted
                }
                if actions == {"create"}:
                    row(
                        "dns",
                        "ok",
                        f"{domain} is free in {zone['name']}; an A record pointing "
                        f"at {target} is created.",
                    )
                elif "adopt" in actions:
                    row(
                        "dns",
                        "ok",
                        f"{domain} already points at {target}; cwcli leaves that "
                        "record in place.",
                    )
                else:
                    row("dns", "ok", f"{domain} is already cwcli's record in {zone['name']}.")
        except CwcliError as e:
            row("dns", "fail", e.message)
    if proxy is not None:
        integ, settings, secrets = proxy
        try:
            client = integ.hooks["proxy_host"](settings, secrets)
            mine = (recorded.get("parts") or {}).get("proxy", {}).get("id")
            host = next(
                (h for h in client.hosts() if domain in (h.get("domain_names") or [])), None
            )
            if host is None:
                row(
                    "proxy",
                    "ok",
                    f"{domain} is not used by a proxy host cwcli can see; one is "
                    "created, then its certificate.",
                )
            elif host.get("id") == mine:
                row("proxy", "ok", f"cwcli's proxy host for {domain} is reused.")
            else:
                row("proxy", "fail", f"{domain} is already used by a proxy host in {client.name}.")
        except CwcliError as e:
            row("proxy", "fail", e.message)
        if dns is None:
            try:
                addresses = sorted({str(a[4][0]) for a in socket.getaddrinfo(domain, 443)})
                row(
                    "resolve",
                    "ok",
                    f"{domain} resolves to {', '.join(addresses)}; it must "
                    "reach this machine for the certificate.",
                )
            except OSError:
                row(
                    "resolve",
                    "warn",
                    f"{domain} does not resolve yet, so the certificate "
                    "request fails until it points at this machine (a wildcard record such as "
                    f"*.{domain.split('.', 1)[1]} covers every future site).",
                )
    return rows
