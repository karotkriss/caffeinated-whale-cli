"""Local fakes of the Cloudflare v4 API and Nginx Proxy Manager's API, served over real
HTTP on 127.0.0.1, so the publishing integrations' own clients are exercised end to
end with no credential and no DNS. Each fake keeps its objects in memory and records
every request it answered (``calls``) for assertions."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CF_TOKEN = "cf-token-not-real-0123456789"
NPM_EMAIL = "cwcli@example.com"
NPM_PASSWORD = "npm-password-not-real"


class _Server:
    """One HTTP server whose requests go to ``handle(method, path, query, body, headers)``."""

    def __init__(self, handle) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                status, answer = handle(self.command, parts.path, query, body, self.headers)
                data = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeCloudflare:
    def __init__(self, zones=("example.com", "example.org"), token: str = CF_TOKEN) -> None:
        self.token = token
        self.status = "active"
        self.zones = {f"zone{i}": name for i, name in enumerate(zones, start=1)}
        self.records: dict[str, dict] = {}
        self.deny_writes = False
        self.calls: list[tuple[str, str, dict | None]] = []
        self.trace_ip = "203.0.113.7"
        self._next = 0
        self._srv = _Server(self.handle)
        self.api = f"{self._srv.url}/client/v4"
        self.trace = f"{self._srv.url}/cdn-cgi/trace"

    def close(self) -> None:
        self._srv.close()

    def add(self, zone_name: str, rtype: str, name: str, content: str, proxied=False) -> str:
        zone = next(z for z, n in self.zones.items() if n == zone_name)
        self._next += 1
        rid = f"rec{self._next}"
        self.records[rid] = {
            "id": rid,
            "zone_id": zone,
            "type": rtype,
            "name": name,
            "content": content,
            "proxied": proxied,
            "comment": "",
            "ttl": 1,
        }
        return rid

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _b in self.calls if m in ("POST", "PATCH", "DELETE")]

    @staticmethod
    def _fail(status, code, message):
        return status, {"success": False, "errors": [{"code": code, "message": message}]}

    def handle(self, method, path, query, body, headers):
        if path == "/cdn-cgi/trace":
            return 200, f"fl=1\nh=1.1.1.1\nip={self.trace_ip}\nts=1\n".encode()
        self.calls.append((method, path, body))
        if headers.get("Authorization") != f"Bearer {self.token}":
            return self._fail(400, 1000, "Invalid API Token")
        path = path.removeprefix("/client/v4")
        ok = lambda result, **extra: (
            200,
            {"success": True, "result": result, **extra},
        )  # noqa: E731
        if path == "/user/tokens/verify":
            return ok({"id": "t1", "status": self.status})
        if path == "/zones":
            zones = [{"id": z, "name": n, "status": "active"} for z, n in self.zones.items()]
            return ok(zones, result_info={"page": 1, "total_pages": 1})
        parts = path.strip("/").split("/")
        zone = parts[1]
        if zone not in self.zones:
            return self._fail(404, 7003, "Could not route")
        if len(parts) == 3:
            if method == "GET":
                found = [
                    r
                    for r in self.records.values()
                    if r["zone_id"] == zone and r["name"] == query.get("name")
                ]
                return ok(found)
            if self.deny_writes:
                return self._fail(403, 10000, "Authentication error")
            self._next += 1
            rid = f"rec{self._next}"
            self.records[rid] = {"id": rid, "zone_id": zone, **body}
            return ok(self.records[rid])
        rid = parts[3]
        if rid not in self.records:
            return self._fail(404, 81044, "Record does not exist.")
        if method == "GET":
            return ok(self.records[rid])
        if self.deny_writes:
            return self._fail(403, 10000, "Authentication error")
        if method == "PATCH":
            self.records[rid].update(body)
            return ok(self.records[rid])
        del self.records[rid]
        return ok({"id": rid})


class FakeNpm:
    """Nginx Proxy Manager with two users: cwcli's (id 2) and a person (id 1)."""

    def __init__(self) -> None:
        self.me = {
            "id": 2,
            "email": NPM_EMAIL,
            "roles": [],
            "permissions": {
                "visibility": "user",
                "proxy_hosts": "manage",
                "certificates": "manage",
            },
        }
        self.requires_2fa = False
        self.hosts: dict[int, dict] = {}
        self.certs: dict[int, dict] = {}
        self.access_lists = [{"id": 3, "name": "Office only"}]
        self.cert_error: str | None = None
        self.calls: list[tuple[str, str, dict | None]] = []
        self._next = 0
        self._srv = _Server(self.handle)
        self.url = self._srv.url

    def close(self) -> None:
        self._srv.close()

    def _id(self) -> int:
        self._next += 1
        return self._next

    def add_host(self, domain: str, owner: int = 1, **fields) -> int:
        hid = self._id()
        self.hosts[hid] = {
            "id": hid,
            "owner_user_id": owner,
            "domain_names": [domain],
            "forward_host": "10.0.0.9",
            "forward_port": 80,
            "certificate_id": 0,
            "access_list_id": 0,
            "locations": [],
            **fields,
        }
        return hid

    def writes(self) -> list[tuple[str, str]]:
        return [
            (m, p)
            for m, p, _b in self.calls
            if m in ("POST", "PUT", "DELETE") and p != "/api/tokens"
        ]

    @staticmethod
    def _fail(status, message):
        return status, {"error": {"code": status, "message": message}}

    def _visible(self, items: dict[int, dict]) -> list[dict]:
        if self.me["permissions"].get("visibility") == "all":
            return list(items.values())
        return [i for i in items.values() if i["owner_user_id"] == self.me["id"]]

    def handle(self, method, path, query, body, headers):
        self.calls.append((method, path, body))
        if path == "/api/" and method == "GET":
            return 200, {
                "status": "OK",
                "setup": True,
                "version": {"major": 2, "minor": 16, "revision": 0},
            }
        if path == "/api/tokens":
            if body != {"identity": NPM_EMAIL, "secret": NPM_PASSWORD}:
                return self._fail(401, "Invalid email or password")
            if self.requires_2fa:
                return 200, {"requires_2fa": True, "challenge_token": "c"}
            return 200, {"token": "npm-session-token", "expires": "2099-01-01T00:00:00Z"}
        if headers.get("Authorization") != "Bearer npm-session-token":
            return self._fail(401, "Token expired")
        if path == "/api/users/me":
            return 200, self.me
        if path == "/api/nginx/access-lists":
            return 200, self.access_lists
        kind, _, rest = path.removeprefix("/api/nginx/").partition("/")
        items = self.hosts if kind == "proxy-hosts" else self.certs
        if not rest:
            if method == "GET":
                return 200, self._visible(items)
            if kind == "proxy-hosts":
                for h in self.hosts.values():
                    if set(h["domain_names"]) & set(body["domain_names"]):
                        return self._fail(400, f"{body['domain_names'][0]} is already in use")
                hid = self._id()
                self.hosts[hid] = {
                    "id": hid,
                    "owner_user_id": self.me["id"],
                    "certificate_id": 0,
                    **body,
                }
                return 201, self.hosts[hid]
            if self.cert_error:
                return self._fail(400, self.cert_error)
            cid = self._id()
            expires = datetime.now(timezone.utc) + timedelta(days=90)
            self.certs[cid] = {
                "id": cid,
                "owner_user_id": self.me["id"],
                "expires_on": expires.strftime("%Y-%m-%d %H:%M:%S"),
                **body,
            }
            return 201, self.certs[cid]
        item = items.get(int(rest))
        if item is None or item not in self._visible(items):
            return self._fail(404, "Not Found")
        if method == "GET":
            return 200, item
        if method == "PUT":
            item.update(body)
            return 200, item
        del items[int(rest)]
        return 200, True
