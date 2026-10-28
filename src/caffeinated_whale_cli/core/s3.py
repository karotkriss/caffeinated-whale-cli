"""A small S3 client on the standard library (SigV4, path-style addressing), for the
backup store's off-site copies: put, get, delete, list. It is deliberately not boto3
- cwcli ships as a uv tool and the four calls it needs are a few hundred lines - and
it speaks to any S3-compatible endpoint (AWS, MinIO, Garage, Ceph RGW). Every
request is signed with ``AWS4-HMAC-SHA256`` over the real payload hash; bodies are
streamed from files, never held in memory.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import io
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree

from .errors import CwcliError, ErrorKind

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class S3Target:
    endpoint: str  # https://s3.eu-west-1.amazonaws.com or http://127.0.0.1:9000
    bucket: str
    region: str
    access_key: str
    secret_key: str
    prefix: str = ""

    def key(self, *parts: str) -> str:
        return "/".join(p.strip("/") for p in (self.prefix, *parts) if p and p.strip("/"))


def _quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    k = _sign(("AWS4" + secret).encode("utf-8"), date)
    k = _sign(k, region)
    k = _sign(k, "s3")
    return _sign(k, "aws4_request")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class S3Client:
    def __init__(self, target: S3Target, *, timeout: int = 120, now=None) -> None:
        self.t = target
        self.timeout = timeout
        self._now = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
        u = urllib.parse.urlsplit(target.endpoint)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise CwcliError(
                ErrorKind.USAGE, "s3.endpoint", f"Not an S3 endpoint URL: {target.endpoint!r}"
            )
        self._scheme, self._host = u.scheme, u.netloc

    # ---------------------------------------------------------------- signing
    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: BinaryIO | bytes | None = None,
        content_sha256: str | None = None,
        content_length: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> urllib.request.Request:
        now = self._now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        path = (
            "/"
            + _quote(self.t.bucket, safe="-_.~")
            + ("/" + _quote(key, safe="-_.~/") if key else "")
        )
        canonical_query = "&".join(
            f"{_quote(k)}={_quote(v)}" for k, v in sorted((query or {}).items())
        )
        payload_hash = content_sha256 or (
            hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else _EMPTY_SHA256
        )
        hdrs = {
            "host": self._host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **{k.lower(): v for k, v in (headers or {}).items()},
        }
        signed = ";".join(sorted(hdrs))
        canonical_headers = "".join(f"{k}:{hdrs[k].strip()}\n" for k in sorted(hdrs))
        canonical = "\n".join(
            [method, path, canonical_query, canonical_headers, signed, payload_hash]
        )
        scope = f"{date}/{self.t.region}/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
        )
        signature = hmac.new(
            _signing_key(self.t.secret_key, date, self.t.region), to_sign.encode(), hashlib.sha256
        ).hexdigest()
        auth = (
            f"AWS4-HMAC-SHA256 Credential={self.t.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )
        url = f"{self._scheme}://{self._host}{path}" + (
            f"?{canonical_query}" if canonical_query else ""
        )
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in hdrs.items():
            if k != "host":
                req.add_header(k, v)
        req.add_header("Authorization", auth)
        if content_length is not None:
            req.add_header("Content-Length", str(content_length))
        return req

    def _send(self, req: urllib.request.Request) -> tuple[int, bytes, dict[str, str]]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:600]
            code = ""
            try:
                code = ElementTree.fromstring(text).findtext("Code") or ""
            except ElementTree.ParseError:
                pass
            raise CwcliError(
                ErrorKind.PRECONDITION,
                "s3.http",
                f"S3 answered HTTP {e.code}{' ' + code if code else ''} for {req.get_method()} {req.full_url}",
                detail={"output": text},
            ) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CwcliError(
                ErrorKind.PRECONDITION, "s3.unreachable", f"S3 endpoint unreachable: {e}"
            ) from None

    # ---------------------------------------------------------------- verbs
    def put_file(self, key: str, path: Path, *, sha256: str | None = None) -> None:
        size = path.stat().st_size
        digest = sha256 or sha256_file(path)
        with open(path, "rb") as f:
            req = self._request("PUT", key, body=f, content_sha256=digest, content_length=size)
            self._send(req)

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> None:
        req = self._request(
            "PUT", key, body=data, content_length=len(data), headers={"content-type": content_type}
        )
        self._send(req)

    def get_to_file(self, key: str, path: Path) -> int:
        req = self._request("GET", key)
        try:
            with (
                urllib.request.urlopen(req, timeout=self.timeout) as r,
                open(path, "wb") as out,
            ):  # noqa: S310
                n = 0
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    out.write(chunk)
                    n += len(chunk)
                return n
        except urllib.error.HTTPError as e:
            raise CwcliError(
                ErrorKind.PRECONDITION, "s3.http", f"S3 answered HTTP {e.code} for GET {key}"
            ) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CwcliError(
                ErrorKind.PRECONDITION, "s3.unreachable", f"S3 endpoint unreachable: {e}"
            ) from None

    def get_bytes(self, key: str) -> bytes:
        _s, body, _h = self._send(self._request("GET", key))
        return body

    def delete(self, key: str) -> None:
        self._send(self._request("DELETE", key))

    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        """Every object under ``prefix`` (``{key, size, last_modified}``), all pages."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            _s, body, _h = self._send(self._request("GET", "", query=q))
            root = ElementTree.fromstring(body)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for c in root.findall(f"{ns}Contents"):
                out.append(
                    {
                        "key": c.findtext(f"{ns}Key") or "",
                        "size": int(c.findtext(f"{ns}Size") or 0),
                        "last_modified": c.findtext(f"{ns}LastModified") or "",
                    }
                )
            token = root.findtext(f"{ns}NextContinuationToken") or None
            if not token:
                return out

    def ensure_bucket(self) -> None:
        """Create the bucket when it does not exist (a 404 on HEAD)."""
        try:
            self._send(self._request("HEAD", ""))
        except CwcliError as e:
            if "HTTP 404" not in e.message:
                raise
            self._send(self._request("PUT", ""))


def probe(target: S3Target) -> dict[str, Any]:
    """Can the target be listed? Never raises; the answer carries the error."""
    try:
        client = S3Client(target, timeout=20)
        client.ensure_bucket()
        objects = client.list(target.key(""))
        return {"ok": True, "objects": len(objects), "error": None}
    except CwcliError as e:
        return {"ok": False, "objects": 0, "error": e.message}


__all__ = ["S3Client", "S3Target", "probe", "sha256_file", "io"]
