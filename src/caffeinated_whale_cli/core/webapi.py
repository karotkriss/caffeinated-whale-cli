"""JSON over HTTP for the publishing integrations (``core.cloudflare``, ``core.npm``).

Stdlib only. A request never logs its headers, so a bearer token or password sent
here stays out of every event, log and error message; callers build their own
messages from the parsed answer, never from the request.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class UnreachableError(Exception):
    """No HTTP answer at all: a refused connection, a timeout, a TLS or DNS failure."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any  # the parsed JSON, or None
    text: str


def call(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 20.0,
) -> Response:
    """One request; any HTTP status is an answer, only no answer raises ``UnreachableError``."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent = {"Accept": "application/json", "User-Agent": "cwcli"}
    if data is not None:
        sent["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, method=method, headers={**sent, **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UnreachableError(str(getattr(e, "reason", None) or e)) from None
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    return Response(status, parsed, text)
