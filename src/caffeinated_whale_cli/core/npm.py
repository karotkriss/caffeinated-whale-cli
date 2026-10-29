"""Nginx Proxy Manager: the ``npm`` integration, which provides ``proxy_host``.

``core.publish`` makes a published site's proxy host here (the site's public name
forwarded to its bench's published web port, with a ``/socket.io`` location on the
bench's socket.io port) and requests its Let's Encrypt certificate.

Nginx Proxy Manager has no API keys: cwcli signs in as a dedicated user each time
(``POST /api/tokens``) with the password stored sealed here. That user should not
be an administrator, should see only the items it created (visibility "Created
items only"), may Manage proxy hosts and SSL certificates, and must not use
two-factor sign-in. With that visibility cwcli can never change a host a person
made.

Certificates use the HTTP challenge by default. The DNS challenge goes through
Cloudflare with a token of its own that Nginx Proxy Manager keeps for renewals;
cwcli never copies its Cloudflare integration's token into it.

When Nginx Proxy Manager runs in a container on this machine, **Detect** reads the
container (version, name, admin port, the Docker bridge gateway it reaches the
benches through) and the test also checks, from inside that container, that a
bench's published port answers at the forward address.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from . import integrations, webapi
from .errors import CwcliError, ErrorKind

HTTP_CHALLENGE = "HTTP"
DNS_CHALLENGE = "DNS through Cloudflare"
IMAGE = "jc21/nginx-proxy-manager"
_CERT_TIMEOUT_S = 300.0  # certbot runs inside the request
_HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]+$")


class ProxyManager:
    """The ``proxy_host`` hook point over Nginx Proxy Manager's API."""

    name = "Nginx Proxy Manager"

    def __init__(self, settings: dict[str, Any], secrets: dict[str, str]) -> None:
        self.url = str(settings.get("url") or "")
        self._email = str(settings.get("email") or "")
        self._password = secrets.get("password") or ""
        self._dns_token = secrets.get("dns_token") or ""
        self.challenge = "dns" if settings.get("challenge") == DNS_CHALLENGE else "http"
        self._token: str | None = None

    def _redact(self, text: str) -> str:
        for secret in (self._password, self._dns_token, self._token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def _send(self, method: str, path: str, body: Any, headers: dict, timeout: float):
        try:
            return webapi.call(
                method, self.url + "/api" + path, headers=headers, body=body, timeout=timeout
            )
        except webapi.UnreachableError as e:
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "npm.unreachable",
                f"Nginx Proxy Manager did not answer at {self.url}: {self._redact(str(e))}",
            ) from None

    def _sign_in(self) -> str:
        if self._token:
            return self._token
        r = self._send(
            "POST", "/tokens", {"identity": self._email, "secret": self._password}, {}, 20.0
        )
        answer = r.body if isinstance(r.body, dict) else {}
        if answer.get("requires_2fa"):
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "npm.two_factor",
                f"{self._email} signs in with two-factor authentication; cwcli needs a "
                "dedicated Nginx Proxy Manager user without it.",
            )
        if r.status >= 300 or not answer.get("token"):
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "npm.sign_in",
                f"Nginx Proxy Manager refused the email and password for {self._email}.",
            )
        self._token = str(answer["token"])
        return self._token

    def _call(
        self, method: str, path: str, body: Any = None, *, timeout: float = 30.0, auth: bool = True
    ) -> Any:
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._sign_in()}"} if auth else {}
            r = self._send(method, path, body, headers, timeout)
            if r.status == 401 and auth and attempt == 1:
                self._token = None  # expired: sign in once more
                continue
            break
        if r.status < 300:
            return r.body
        error = (r.body or {}).get("error") if isinstance(r.body, dict) else None
        message = self._redact(str((error or {}).get("message") or f"HTTP {r.status}"))
        raise CwcliError(
            ErrorKind.NOT_FOUND if r.status == 404 else ErrorKind.PRECONDITION,
            "npm.not_found" if r.status == 404 else "npm.refused",
            f"Nginx Proxy Manager refused: {message}",
            detail={"status": r.status, "message": message},
        )

    def _maybe(self, path: str) -> dict[str, Any] | None:
        try:
            found = self._call("GET", path)
        except CwcliError as e:
            if e.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        return dict(found) if found else None

    def health(self) -> dict[str, Any]:
        answer = self._call("GET", "/", auth=False)
        return answer if isinstance(answer, dict) else {}

    def me(self) -> dict[str, Any]:
        return dict(self._call("GET", "/users/me?expand=permissions") or {})

    def hosts(self) -> list[dict[str, Any]]:
        return list(self._call("GET", "/nginx/proxy-hosts") or [])

    def host(self, host_id: int) -> dict[str, Any] | None:
        return self._maybe(f"/nginx/proxy-hosts/{int(host_id)}")

    def create_host(self, spec: dict[str, Any]) -> dict[str, Any]:
        return dict(self._call("POST", "/nginx/proxy-hosts", spec))

    def update_host(self, host_id: int, spec: dict[str, Any]) -> dict[str, Any]:
        return dict(self._call("PUT", f"/nginx/proxy-hosts/{int(host_id)}", spec))

    def delete_host(self, host_id: int) -> None:
        self._call("DELETE", f"/nginx/proxy-hosts/{int(host_id)}")

    def certificates(self) -> list[dict[str, Any]]:
        return list(self._call("GET", "/nginx/certificates") or [])

    def certificate(self, cert_id: int) -> dict[str, Any] | None:
        return self._maybe(f"/nginx/certificates/{int(cert_id)}")

    def request_certificate(self, domain: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"dns_challenge": self.challenge == "dns"}
        if self.challenge == "dns":
            meta["dns_provider"] = "cloudflare"
            meta["dns_provider_credentials"] = f"dns_cloudflare_api_token={self._dns_token}"
        spec = {"provider": "letsencrypt", "domain_names": [domain], "meta": meta}
        return dict(self._call("POST", "/nginx/certificates", spec, timeout=_CERT_TIMEOUT_S))

    def delete_certificate(self, cert_id: int) -> None:
        self._call("DELETE", f"/nginx/certificates/{int(cert_id)}")

    def access_lists(self) -> list[dict[str, Any]]:
        return [
            {"id": int(a["id"]), "name": str(a.get("name") or a["id"])}
            for a in self._call("GET", "/nginx/access-lists") or []
        ]


def version_of(health: dict[str, Any]) -> str:
    v = health.get("version") or {}
    return ".".join(str(v.get(k, 0)) for k in ("major", "minor", "revision")) if v else "?"


# ----------------------------------------------------------- the local container


def _host_port(container, port: int) -> int | None:
    for entry in (getattr(container, "ports", None) or {}).get(f"{port}/tcp") or []:
        if (entry or {}).get("HostPort"):
            return int(entry["HostPort"])
    return None


def local_container():
    """The running Nginx Proxy Manager container on this machine, or None."""
    import docker
    from docker.errors import DockerException

    try:
        for container in docker.from_env().containers.list():
            image = str((container.attrs.get("Config") or {}).get("Image") or "")
            if image.startswith(IMAGE):
                return container
    except DockerException:
        return None
    return None


def _gateway(container) -> str | None:
    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    for net in networks.values():
        if (net or {}).get("Gateway"):
            return str(net["Gateway"])
    return None


def _is_local(url: str, container) -> bool:
    parts = urlsplit(url)
    return parts.hostname in ("127.0.0.1", "localhost", "::1") and (parts.port or 80) == _host_port(
        container, 81
    )


def _a_bench_port() -> int | None:
    """A published bench web port on this machine, for the reachability check."""
    import docker
    from docker.errors import DockerException

    try:
        containers = docker.from_env().containers.list(
            filters={"label": "com.docker.compose.service=frappe"}
        )
    except DockerException:
        return None
    for container in containers:
        port = _host_port(container, 8000)
        if port:
            return port
    return None


def _reachability(settings: dict[str, Any]) -> tuple[bool, str]:
    container = local_container()
    if container is None or not _is_local(settings["url"], container):
        return True, (
            "The reachability check was skipped: Nginx Proxy Manager is not a container on "
            "this machine at that address."
        )
    port = _a_bench_port()
    if port is None:
        return True, (
            "The reachability check was skipped: no bench port is published on this machine yet."
        )
    target = f"http://{settings['forward_host']}:{port}/"
    code, out = container.exec_run(
        ["sh", "-c", 'curl -s -o /dev/null -m 5 -w "%{http_code}" "$1"; echo " $?"', "sh", target]
    )
    words = (out or b"").decode("utf-8", "replace").split()
    http = words[0] if len(words) > 1 else "000"
    exit_code = words[-1] if words else "1"
    # 7 refused and 28 timed out mean nothing answered; an empty reply (52) still
    # reached the published port.
    if http == "000" and exit_code in ("6", "7", "28"):
        return False, (
            f"From its container, Nginx Proxy Manager could not reach {target} (curl exit "
            f"{exit_code}). Check the forward address and any firewall in front of Docker."
        )
    return True, f"From its container, {target} answered."


# ------------------------------------------------------------------ the contract


def _normalize(settings: dict[str, Any]) -> dict[str, Any]:
    url = str(settings.get("url") or "").strip().rstrip("/").removesuffix("/api")
    out: dict[str, Any] = {
        "url": url,
        "email": str(settings.get("email") or "").strip(),
        "forward_host": str(settings.get("forward_host") or "").strip(),
        "challenge": str(settings.get("challenge") or HTTP_CHALLENGE),
        "ssl_forced": settings.get("ssl_forced", True) is not False,
        "http2": settings.get("http2", True) is not False,
        "block_exploits": settings.get("block_exploits", True) is not False,
        "hsts": settings.get("hsts") is True,
    }
    parts = urlsplit(url)
    if url and (parts.scheme not in ("http", "https") or not parts.netloc):
        raise CwcliError(
            ErrorKind.USAGE,
            "npm.url",
            f"Not an admin URL: {url!r} (http://127.0.0.1:81 or https://npm.example.com).",
        )
    if out["email"] and "@" not in out["email"]:
        raise CwcliError(ErrorKind.USAGE, "npm.email", "The email is not an email address.")
    if out["forward_host"] and not _HOST_RE.match(out["forward_host"]):
        raise CwcliError(
            ErrorKind.USAGE,
            "npm.forward_host",
            "The forward address is a host name or IP address only (no scheme or port).",
        )
    if out["challenge"] not in (HTTP_CHALLENGE, DNS_CHALLENGE):
        raise CwcliError(
            ErrorKind.USAGE,
            "npm.challenge",
            f"The certificate challenge is {HTTP_CHALLENGE} or {DNS_CHALLENGE}.",
        )
    return out


def _test(settings: dict[str, Any], secrets: dict[str, str]) -> integrations.Check:
    missing = [
        label
        for label, value in (
            ("admin URL", settings.get("url")),
            ("email", settings.get("email")),
            ("password", secrets.get("password")),
        )
        if not value
    ]
    if missing:
        return integrations.Check(False, f"Type the {' and the '.join(missing)}.")
    client = ProxyManager(settings, secrets)
    try:
        health = client.health()
    except CwcliError as e:
        return integrations.Check(False, e.message)
    if health.get("status") != "OK":
        return integrations.Check(
            False, f"{settings['url']} answered, but not as Nginx Proxy Manager."
        )
    version = version_of(health)
    if client.challenge == "dns" and not secrets.get("dns_token"):
        return integrations.Check(
            False, "Paste the Cloudflare token Nginx Proxy Manager uses for the DNS challenge."
        )
    try:
        me = client.me()
    except CwcliError as e:
        return integrations.Check(False, e.message)
    perms = me.get("permissions") or {}
    admin = "admin" in (me.get("roles") or [])
    hosts, certs = perms.get("proxy_hosts", "hidden"), perms.get("certificates", "hidden")
    if not admin and (hosts != "manage" or certs != "manage"):
        return integrations.Check(
            False,
            f"{settings['email']} may {hosts} proxy hosts and {certs} certificates; cwcli "
            "needs Manage for both.",
        )
    lines = [f"Nginx Proxy Manager {version} answers; signed in as {settings['email']}."]
    if admin:
        lines.append(
            "This user is an administrator and can change every host; a dedicated user that "
            "sees only its own hosts is safer."
        )
    elif perms.get("visibility") != "user":
        lines.append(
            "This user sees every proxy host; the recommended user sees only the items it "
            "created."
        )
    else:
        lines.append("It manages only the proxy hosts and certificates it created.")
    if not settings.get("forward_host"):
        return integrations.Check(False, " ".join([*lines, "Type the forward address."]))
    reached, text = _reachability(settings)
    lines.append(text)
    return integrations.Check(reached, " ".join(lines), {"version": version})


def detect(settings: dict[str, Any], secrets: dict[str, str], args: dict) -> dict:
    """The local Nginx Proxy Manager container, for the setup form to fill from."""
    container = local_container()
    if container is None:
        return {"found": False}
    admin_port = _host_port(container, 81)
    url = f"http://127.0.0.1:{admin_port}" if admin_port else None
    version = None
    if url:
        try:
            version = version_of(ProxyManager({"url": url}, {}).health())
        except CwcliError:
            version = None
    return {
        "found": True,
        "container": container.name,
        "version": version,
        "admin_port": admin_port,
        "url": url,
        "forward_host": _gateway(container),
    }


INTEGRATION = integrations.Integration(
    id="npm",
    name="Nginx Proxy Manager",
    publisher=integrations.OFFICIAL,
    description="Publishing a site makes its proxy host here and requests its Let's Encrypt "
    "certificate, so the site answers over HTTPS.",
    provides=("proxy_host",),
    fields=(
        integrations.Field(
            "url",
            "Admin URL",
            required=True,
            default="http://127.0.0.1:81",
            help="On this machine use 127.0.0.1, so the admin port can stay closed to the "
            "network.",
        ),
        integrations.Field(
            "forward_host",
            "Forward address",
            required=True,
            placeholder="172.17.0.1",
            help="How it reaches the benches: the Docker bridge gateway when it runs here "
            "(filled in when it is found), else this machine's LAN address.",
        ),
        integrations.Field(
            "email",
            "Email",
            required=True,
            placeholder="cwcli@example.com",
            help="A dedicated user, not an administrator: Created items only, Manage proxy "
            "hosts and SSL certificates, no two-factor sign-in.",
        ),
        integrations.Field(
            "password",
            "Password",
            kind="secret",
            required=True,
            help="Stored encrypted. Let's Encrypt uses the user's email for its account.",
        ),
        integrations.Field(
            "challenge",
            "Certificate challenge",
            kind="select",
            options=(HTTP_CHALLENGE, DNS_CHALLENGE),
            default=HTTP_CHALLENGE,
            help="HTTP needs port 80 open to the internet. DNS works behind Cloudflare's proxy.",
        ),
        integrations.Field(
            "dns_token",
            "Cloudflare token for the DNS challenge",
            kind="secret",
            help='Its own "Edit zone DNS" token, kept by Nginx Proxy Manager for renewals; '
            "cwcli never copies its Cloudflare integration's token here.",
        ),
        integrations.Field("ssl_forced", "Force SSL", kind="bool", default=True),
        integrations.Field("http2", "HTTP/2", kind="bool", default=True),
        integrations.Field("block_exploits", "Block common exploits", kind="bool", default=True),
        integrations.Field("hsts", "HSTS", kind="bool", default=False),
    ),
    normalize=_normalize,
    test=_test,
    actions={"detect": detect},
    secrets_bound_to=("url", "email"),
    hooks={"proxy_host": lambda settings, secrets: ProxyManager(settings, secrets)},
)
