"""``core.app_catalog`` - every Frappe app the user has installed or can reach, with logos.

One catalog, two halves, merged on app identity:

* **Installed** - every bench app cwcli remembers (``core.inspect``'s cache),
  plus, for each RUNNING instance, one exec that reads the real git remote, the
  checked-out ref and the ``hooks.py`` logo of every app directory. The cache
  records app names and install-time ``name version branch`` lines only, so the
  remote (the identity a remote repo can be matched on) is live-only and marked
  ``verified`` when it came from that exec, ``remembered`` otherwise.
* **Remote** - every repository the host's signed-in ``gh`` and ``glab`` can
  reach that is a Frappe app: a ``pyproject.toml`` ``[project].name`` (or the
  repo name) naming a top-level directory that holds ``hooks.py``. GitHub is one
  GraphQL page loop; each GitLab host is a threaded REST walk, with a project
  whose ``last_activity_at`` has not moved reusing its previous answer.

Credentials come from the same place the credential bridge gets them
(``<tool> auth git-credential get``), held in this process's memory for the scan
and never written, logged or sent to the page. Remote URLs are stored with any
userinfo stripped.

Logos, in order: the app's own ``hooks.py`` logo (``app_logo_url`` or
``add_to_apps_screen[].logo``; read from the bench's built assets or source when
installed, raw from the forge when remote), then the GitLab project avatar, then
the GitHub social card (kept as ``kind: "card"`` so a page can show a banner as a
banner rather than pose it as an icon), else none (the page draws a lettered
tile). Every logo is content-addressed under ``<cwcli home>/cache/app-catalog/
logos/`` and served same-origin by the daemon, so the browser never talks to a
forge and the CSP's ``img-src 'self'`` stays intact.

UI-pure: no rich/typer/questionary; returns plain dataclasses.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..utils import config_utils, db_utils
from ..utils.config_utils import cwcli_home
from . import docker as core_docker
from . import editors as core_editors
from . import inspect as core_inspect
from .errors import CwcliError, ErrorKind
from .list import list_instances

CATALOG_VERSION = 1
LOGO_MAX_BYTES = 2 * 1024 * 1024
_HTTP_TIMEOUT = 30
_SCAN_WORKERS = 6
_UA = "cwcli-app-catalog"
_LOGO_FILE_RE = re.compile(r"^[0-9a-f]{16}\.(svg|png|jpg|gif|webp|ico)$")
_HOOKS_LOGO_RE = re.compile(r"""^\s*app_logo_url\s*=\s*["']([^"']+)["']""", re.M)
_SCREEN_LOGO_RE = re.compile(r"""["']logo["']\s*:\s*["']([^"']+)["']""")
_HOOKS_TITLE_RE = re.compile(r"""^\s*app_title\s*=\s*["']([^"']+)["']""", re.M)
_HOOKS_DESC_RE = re.compile(r"""^\s*app_description\s*=\s*["']([^"']+)["']""", re.M)
_PYPROJECT_NAME_RE = re.compile(r"""^\s*name\s*=\s*["']([^"']+)["']""", re.M)
_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ------------------------------------------------------------------- DTOs


@dataclass(slots=True, kw_only=True)
class SourceState:
    """One forge the catalog reads: what it is, whether it answered, what it found."""

    kind: str  # github | gitlab
    host: str
    account: str | None = None
    signed_in: bool = False
    repos: int = 0
    apps: int = 0
    scanned_at: float | None = None
    duration_s: float | None = None
    error: str | None = None
    state: str = "unverified"  # verified | remembered | unverified
    scope: str = ""  # what is scanned on this host, in words
    progress: dict[str, int] | None = None  # {"done", "total"} while a scan runs
    icon: dict[str, Any] | None = None  # the host's own favicon, cached like a logo


@dataclass(slots=True, kw_only=True)
class AppInstall:
    """One bench that holds the app, and the sites on it that have it installed."""

    project: str
    project_state: str  # present | absent | unverified
    bench_index: int
    bench_label: str | None
    bench_path: str
    sites: list[dict[str, Any]]  # {name, version, branch}
    remote: str | None = None  # normalized host/path, live-read only
    checked_out: str | None = None  # live git ref, live-read only
    verified: bool = False  # True when remote/ref came from the live exec


@dataclass(slots=True, kw_only=True)
class CatalogApp:
    key: str  # github.com/owner/repo | dev.egov.gy/group/proj | local:<name>
    name: str  # the Frappe app (module) name
    title: str
    description: str | None = None
    state: str = "installed"  # installed | remote | both
    source: dict[str, Any] | None = None  # kind, host, path, url, private, default_branch, ...
    installs: list[AppInstall] = field(default_factory=list)
    logo: dict[str, Any] | None = None  # {url, source, kind, content_type}
    cover: dict[str, Any] | None = None  # the GitHub social card when it is a real banner
    remote_state: str = "unverified"  # verified | remembered | unverified
    hooks_logo: str | None = None  # the declared hooks.py logo path, for honesty
    official: bool = False  # owned by the frappe GitHub organisation (github.com/frappe/*)


OFFICIAL_PREFIX = "github.com/frappe/"


@dataclass(slots=True, kw_only=True)
class Catalog:
    scanned_at: float | None
    scanning: bool
    sources: list[SourceState]
    apps: list[CatalogApp]
    installed_read_at: float | None
    warnings: list[dict[str, str]] = field(default_factory=list)
    extra_sources: list[str] = field(default_factory=list)
    editors: list[str] = field(default_factory=list)  # host editors `cwcli open` can hand over to
    editor_paths: dict[str, str] = field(default_factory=dict)


def installed_editors() -> list[str]:
    return core_editors.installed_editors()


# ------------------------------------------------------- scan scope (config)
# [app_catalog] extra_sources = ["github:<org>", "gitlab.com:<group/path>"]
# GitHub and gitlab.com scan the repositories the signed-in account is a MEMBER of,
# plus these. A self-hosted GitLab host scans EVERYTHING the account can see and
# keeps only what is proven to be a Frappe app (the captain's rule, 2026-10-27).
_SOURCE_RE = re.compile(r"^(github|[a-z0-9.-]+\.[a-z]{2,}):([A-Za-z0-9._/-]+)$")


def extra_sources() -> list[str]:
    try:
        raw = (config_utils.load_config().get("app_catalog") or {}).get("extra_sources") or []
    except Exception:
        return []
    return [str(x) for x in raw if isinstance(x, str) and _SOURCE_RE.match(x)]


def set_extra_sources(values: list[str]) -> list[str]:
    clean = sorted({v.strip() for v in values if _SOURCE_RE.match(v.strip())})
    config = config_utils.load_config()
    config.setdefault("app_catalog", {})["extra_sources"] = clean
    config_utils.save_config(config)
    return clean


def add_extra_source(value: str) -> list[str]:
    value = value.strip()
    if not _SOURCE_RE.match(value):
        raise CwcliError(
            ErrorKind.USAGE,
            "catalog.source_invalid",
            "A source is 'github:<org>' or '<gitlab host>:<group/path>'.",
        )
    return set_extra_sources([*extra_sources(), value])


def remove_extra_source(value: str) -> list[str]:
    return set_extra_sources([v for v in extra_sources() if v != value.strip()])


def is_self_hosted(host: str) -> bool:
    return host not in {"github.com", "gitlab.com"}


def as_json(catalog: Catalog) -> dict[str, Any]:
    return asdict(catalog)


# --------------------------------------------------------------- storage


def catalog_dir() -> Path:
    return cwcli_home() / "cache" / "app-catalog"


def logos_dir() -> Path:
    return catalog_dir() / "logos"


def _catalog_file() -> Path:
    return catalog_dir() / "catalog.json"


def logo_path(name: str) -> Path | None:
    """The on-disk file for a logo name the page was handed, or None for anything else."""
    if not _LOGO_FILE_RE.match(name):
        return None
    path = logos_dir() / name
    return path if path.is_file() else None


_CONTENT_TYPES = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/x-icon",
}


def content_type_for(name: str) -> str:
    return _CONTENT_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")


def _sniff(raw: bytes) -> str | None:
    """The image kind by its bytes, never by a name a forge handed us."""
    head = raw[:64]
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head.startswith(b"GIF8"):
        return "gif"
    if head.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    text = raw[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith(b"<") and b"<svg" in raw[:4096].lower():
        return "svg"
    return None


def store_logo(raw: bytes | None, *, source: str, kind: str = "icon") -> dict[str, Any] | None:
    """Content-address an image into the logo cache; None for not-an-image or too big."""
    if not raw or len(raw) > LOGO_MAX_BYTES:
        return None
    ext = _sniff(raw)
    if ext is None:
        return None
    name = f"{hashlib.sha256(raw).hexdigest()[:16]}.{ext}"
    logos_dir().mkdir(parents=True, exist_ok=True)
    path = logos_dir() / name
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    return {
        "file": name,
        "url": f"/api/v1/apps/logo/{name}",
        "source": source,
        "kind": kind,
        "content_type": _CONTENT_TYPES[ext],
    }


def _load_disk() -> dict[str, Any]:
    try:
        data = json.loads(_catalog_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == CATALOG_VERSION else {}


def _save_disk(data: dict[str, Any]) -> None:
    catalog_dir().mkdir(parents=True, exist_ok=True)
    tmp = _catalog_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, _catalog_file())


# ----------------------------------------------------------- identities


def normalize_remote(url: str | None) -> tuple[str, str] | None:
    """``(host, path)`` for any git remote spelling, userinfo dropped, ``.git`` stripped."""
    if not url:
        return None
    url = url.strip()
    m = re.match(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/](.+)$", url)
    if url.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(url)
        host, path = parsed.hostname or "", parsed.path
    elif m:
        host, path = m.group(1), m.group(2)
    else:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return host.lower(), path.lower()


def remote_key(url: str | None) -> str | None:
    norm = normalize_remote(url)
    return f"{norm[0]}/{norm[1]}" if norm else None


def _safe_remote_url(url: str | None) -> str | None:
    """The remote with any ``user:token@`` dropped - never store a credential-bearing URL."""
    norm = normalize_remote(url)
    return f"https://{norm[0]}/{norm[1]}" if norm else None


def _parse_app_line(entry: str) -> dict[str, Any]:
    parts = str(entry).split()
    return {
        "name": parts[0] if parts else "",
        "version": parts[1] if len(parts) > 1 else None,
        "branch": " ".join(parts[2:]) or None,
    }


# ------------------------------------------------------------ credentials


def credential(host: str) -> str | None:
    """The host tool's token for ``host``, in memory only. None when not signed in."""
    tool = "gh" if host.endswith("github.com") else "glab"
    try:
        out = subprocess.run(
            [tool, "auth", "git-credential", "get"],
            input=f"protocol=https\nhost={host}\n\n".encode(),
            capture_output=True,
            timeout=20,
        ).stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password=") :].strip() or None
    return None


def gitlab_hosts() -> list[str]:
    """Every host glab is configured for (names only; the token is never read here)."""
    path = (
        Path(os.environ.get("GLAB_CONFIG_DIR") or Path.home() / ".config" / "glab-cli")
        / "config.yml"
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    hosts: list[str] = []
    in_hosts = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_hosts = line.strip() == "hosts:"
            continue
        if in_hosts and re.match(r"^ {2,4}[A-Za-z0-9.-]+:\s*$", line):
            hosts.append(line.strip()[:-1])
    return hosts


class _Http:
    def __init__(self, token: str | None, auth: str) -> None:
        self._token = token
        self._auth = auth  # bearer | private-token

    def _headers(self, accept: str | None) -> dict[str, str]:
        headers = {"User-Agent": _UA}
        if accept:
            headers["Accept"] = accept
        if self._token:
            if self._auth == "private-token":
                headers["PRIVATE-TOKEN"] = self._token
            else:
                headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def get(self, url: str, *, accept: str | None = None) -> tuple[int, bytes, dict[str, str]]:
        req = urllib.request.Request(url, headers=self._headers(accept))
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:  # noqa: S310
                return (
                    r.status,
                    r.read(LOGO_MAX_BYTES + 1),
                    {k.lower(): v for k, v in r.headers.items()},
                )
        except urllib.error.HTTPError as e:
            return e.code, b"", {}

    def json(self, url: str) -> Any:
        status, raw, _ = self.get(url, accept="application/json")
        if status != 200:
            raise CatalogHTTPError(status, url)
        return json.loads(raw.decode("utf-8"))

    def post_json(self, url: str, payload: dict) -> Any:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={**self._headers("application/json"), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=max(_HTTP_TIMEOUT, 90)) as r:  # noqa: S310
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise CatalogHTTPError(e.code, url) from e


class CatalogHTTPError(Exception):
    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} from {urllib.parse.urlparse(url).netloc}")
        self.status = status


def _fetch_public(url: str) -> bytes | None:
    """An unauthenticated fetch of a public image URL (the GitHub social card)."""
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=_HTTP_TIMEOUT
        ) as r:  # noqa: S310
            return bytes(r.read(LOGO_MAX_BYTES + 1))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        # HTTPException covers a body cut short mid-read (IncompleteRead), which is not an OSError.
        return None


def _owner_avatar_memo() -> Callable[[str, str], dict[str, Any] | None]:
    """A per-scan memo: the GitHub org/user avatar (public) fetched once per owner."""
    memo: dict[str, dict[str, Any] | None] = {}
    lock = threading.Lock()

    def get(owner: str, url: str) -> dict[str, Any] | None:
        key = owner.lower()
        with lock:
            if key in memo:
                return memo[key]
        logo = store_logo(_fetch_public(url), source="github_owner_avatar", kind="group")
        with lock:
            memo[key] = logo
        return logo

    return get


# ---------------------------------------------------------------- host icons
_ICON_LINK_RE = re.compile(
    r"""<link[^>]+rel=["'](?:shortcut icon|icon|apple-touch-icon)["'][^>]*>""", re.I
)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)


def fetch_host_icon(host: str) -> dict[str, Any] | None:
    """The host's own favicon (from its ``<link rel=icon>``, else ``/favicon.ico``),
    content-addressed into the logo cache. Anonymous: a sign-in page still links its icon."""
    base = f"https://{host}/"
    candidates: list[str] = []
    try:
        with urllib.request.urlopen(
            urllib.request.Request(base, headers={"User-Agent": _UA}), timeout=_HTTP_TIMEOUT
        ) as r:  # noqa: S310
            html = r.read(300_000).decode("utf-8", "replace")
            final = r.geturl()
        for tag in _ICON_LINK_RE.findall(html):
            m = _HREF_RE.search(tag)
            if m:
                candidates.append(urllib.parse.urljoin(final, m.group(1)))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    candidates.append(base + "favicon.ico")
    for url in candidates:
        stored = store_logo(_fetch_public(url), source="host_icon")
        if stored:
            return stored
    return None


# ---------------------------------------------------------------- hooks.py


def _hooks_facts(text: str) -> dict[str, str | None]:
    logo = _HOOKS_LOGO_RE.search(text)
    if not logo:
        logo = _SCREEN_LOGO_RE.search(text)
    title = _HOOKS_TITLE_RE.search(text)
    desc = _HOOKS_DESC_RE.search(text)
    return {
        "logo": logo.group(1).strip() if logo else None,
        "title": title.group(1).strip() if title else None,
        "description": desc.group(1).strip() if desc else None,
    }


def _logo_source_path(module: str, logo: str) -> str | None:
    """``/assets/<app>/<rest>`` -> ``<module>/public/<rest>`` (the repo path of the asset)."""
    m = re.match(r"^/?assets/([^/]+)/(.+)$", logo)
    if not m:
        return None
    return f"{module}/public/{m.group(2)}"


# ------------------------------------------------------------- GitHub scan

_GH_REPO_FIELDS = """
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner isPrivate isArchived isFork description url pushedAt stargazerCount
        owner { login avatarUrl(size: 200) }
        openGraphImageUrl usesCustomOpenGraphImage
        defaultBranchRef { name }
        pyproject: object(expression: "HEAD:pyproject.toml") { ... on Blob { text } }
        root: object(expression: "HEAD:") { ... on Tree { entries { name type } } }
      }
"""
_GH_REPOS_QUERY = (
    """
query($after: String) {
  viewer {
    login
    repositories(first: 100, after: $after,
      affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
      ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {"""
    + _GH_REPO_FIELDS
    + """
    }
  }
}
"""
)
_GH_ORG_QUERY = (
    """
query($org: String!, $after: String) {
  organization(login: $org) {
    repositories(first: 100, after: $after) {"""
    + _GH_REPO_FIELDS
    + """
    }
  }
}
"""
)


def _module_for(repo_name: str, pyproject_text: str | None, dirs: set[str]) -> str | None:
    """Rule A (Pilot's): pyproject ``name`` names a top-level dir; rule B: the repo name does."""
    if pyproject_text:
        m = _PYPROJECT_NAME_RE.search(pyproject_text)
        if m and m.group(1).replace("-", "_") in dirs:
            return m.group(1).replace("-", "_")
    candidate = repo_name.replace("-", "_")
    return candidate if candidate in dirs else None


def scan_github(
    previous: dict[str, Any], extra_orgs: list[str] | None = None
) -> tuple[SourceState, dict[str, dict[str, Any]]]:
    host = "github.com"
    source = SourceState(
        kind="github",
        host=host,
        scope="member repositories"
        + (f" + {len(extra_orgs)} organisation(s)" if extra_orgs else ""),
    )
    token = credential(host)
    if not token:
        source.error = "gh is not signed in"
        return source, {}
    source.signed_in = True
    source.icon = fetch_host_icon(host)
    http = _Http(token, "bearer")
    started = time.monotonic()
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    try:
        while True:
            data = http.post_json(
                "https://api.github.com/graphql",
                {"query": _GH_REPOS_QUERY, "variables": {"after": after}},
            )
            viewer = (data.get("data") or {}).get("viewer") or {}
            source.account = viewer.get("login")
            page = viewer.get("repositories") or {}
            nodes.extend(page.get("nodes") or [])
            if not (page.get("pageInfo") or {}).get("hasNextPage"):
                break
            after = page["pageInfo"]["endCursor"]
        for org in extra_orgs or []:
            after = None
            while True:
                data = http.post_json(
                    "https://api.github.com/graphql",
                    {"query": _GH_ORG_QUERY, "variables": {"org": org, "after": after}},
                )
                page = ((data.get("data") or {}).get("organization") or {}).get(
                    "repositories"
                ) or {}
                nodes.extend(page.get("nodes") or [])
                if not (page.get("pageInfo") or {}).get("hasNextPage"):
                    break
                after = page["pageInfo"]["endCursor"]
    except (CatalogHTTPError, urllib.error.URLError, OSError, ValueError) as e:
        source.error = f"GitHub listing failed: {e}"
        return source, {}
    seen: set[str] = set()
    unique_nodes = []
    for n in nodes:
        if n and n["nameWithOwner"] not in seen:
            seen.add(n["nameWithOwner"])
            unique_nodes.append(n)
    nodes = unique_nodes
    source.repos = len(nodes)

    candidates: list[tuple[dict[str, Any], str]] = []
    for n in nodes:
        dirs = {
            e["name"]
            for e in ((n.get("root") or {}).get("entries") or [])
            if e.get("type") == "tree"
        }
        module = _module_for(
            n["nameWithOwner"].split("/")[1], (n.get("pyproject") or {}).get("text"), dirs
        )
        if module:
            candidates.append((n, module))

    # One batched query confirms hooks.py (and reads its logo/title) for every candidate.
    hooks_text: dict[str, str | None] = {}
    if candidates:
        parts = []
        for i, (n, module) in enumerate(candidates):
            owner, name = n["nameWithOwner"].split("/")
            parts.append(
                f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f'{{ hooks: object(expression: {json.dumps(f"HEAD:{module}/hooks.py")}) {{ ... on Blob {{ text }} }} }}'
            )
        try:
            data = http.post_json(
                "https://api.github.com/graphql", {"query": "{ " + " ".join(parts) + " }"}
            )
            for i, (n, _module) in enumerate(candidates):
                hooks_text[n["nameWithOwner"]] = ((data.get("data") or {}).get(f"r{i}") or {}).get(
                    "hooks"
                ) or None
                blob = hooks_text[n["nameWithOwner"]]
                hooks_text[n["nameWithOwner"]] = (
                    blob.get("text") if isinstance(blob, dict) else None
                )
        except (CatalogHTTPError, urllib.error.URLError, OSError, ValueError) as e:
            source.error = f"GitHub hooks.py check failed: {e}"
            return source, {}

    apps: dict[str, dict[str, Any]] = {}
    prev_apps = (previous.get("remote") or {}) if previous else {}
    owner_avatar = _owner_avatar_memo()
    for n, module in candidates:
        full = n["nameWithOwner"]
        text = hooks_text.get(full)
        if text is None:
            continue
        key = f"{host}/{full.lower()}"
        facts = _hooks_facts(text)
        prev = prev_apps.get(key) or {}
        logo = None
        cover = None
        prev_logo = (
            prev.get("logo") if (prev.get("logo") or {}).get("source") != "github_card" else None
        )
        if prev.get("pushed_at") == n.get("pushedAt") and prev_logo:
            logo, cover = prev_logo, prev.get("cover")
        else:
            owner, name = full.split("/")
            if facts["logo"]:
                rel = _logo_source_path(module, facts["logo"])
                if rel:
                    status, raw, _ = http.get(
                        f"https://api.github.com/repos/{owner}/{name}/contents/{urllib.parse.quote(rel)}",
                        accept="application/vnd.github.raw",
                    )
                    if status == 200:
                        logo = store_logo(raw, source="hooks")
            # A social card is NEVER the tile logo (the captain's rule): only a repo
            # with a custom card gets it, as an optional cover on the detail panel.
            if n.get("openGraphImageUrl") and n.get("usesCustomOpenGraphImage"):
                cover = store_logo(
                    _fetch_public(n["openGraphImageUrl"]), source="github_card", kind="card"
                )
            if logo is None and (n.get("owner") or {}).get("avatarUrl"):
                logo = owner_avatar(owner, n["owner"]["avatarUrl"])
        apps[key] = {
            "key": key,
            "name": module,
            "title": facts["title"] or full.split("/")[1],
            "description": n.get("description") or facts["description"],
            "source": {
                "kind": "github",
                "host": host,
                "path": full,
                "url": n.get("url"),
                "private": bool(n.get("isPrivate")),
                "archived": bool(n.get("isArchived")),
                "fork": bool(n.get("isFork")),
                "default_branch": (n.get("defaultBranchRef") or {}).get("name"),
                "stars": n.get("stargazerCount"),
                "custom_card": bool(n.get("usesCustomOpenGraphImage")),
            },
            "hooks_logo": facts["logo"],
            "logo": logo,
            "cover": cover,
            "pushed_at": n.get("pushedAt"),
        }
    source.apps = len(apps)
    source.scanned_at = time.time()
    source.duration_s = round(time.monotonic() - started, 2)
    source.state = "verified"
    return source, apps


# ------------------------------------------------------------- GitLab scan


def _gitlab_project(
    http: _Http, base: str, project: dict[str, Any], prev: dict[str, Any] | None
) -> dict[str, Any] | None:
    """One project's verdict: a catalog entry, or None when it is not a Frappe app."""
    pid = project["id"]
    ref = project.get("default_branch") or "HEAD"
    if prev and prev.get("last_activity_at") == project.get("last_activity_at"):
        return prev  # unchanged since last scan: the previous answer (logo file included) stands
    if project.get("empty_repo"):
        return None
    try:
        tree = http.json(
            f"{base}/projects/{pid}/repository/tree?per_page=100&ref={urllib.parse.quote(ref)}"
        )
    except (CatalogHTTPError, urllib.error.URLError, OSError, ValueError):
        return None
    dirs = {e["name"] for e in tree if e.get("type") == "tree"}
    pyproject = None
    status, raw, _ = http.get(
        f"{base}/projects/{pid}/repository/files/pyproject.toml/raw?ref={urllib.parse.quote(ref)}"
    )
    if status == 200:
        pyproject = raw.decode("utf-8", "replace")
    module = _module_for(project["path"], pyproject, dirs)
    if not module:
        return None
    status, raw, _ = http.get(
        f"{base}/projects/{pid}/repository/files/{urllib.parse.quote(f'{module}/hooks.py', safe='')}/raw?ref={urllib.parse.quote(ref)}"
    )
    if status != 200:
        return None
    facts = _hooks_facts(raw.decode("utf-8", "replace"))
    logo = None
    if facts["logo"]:
        rel = _logo_source_path(module, facts["logo"])
        if rel:
            status, raw, _ = http.get(
                f"{base}/projects/{pid}/repository/files/{urllib.parse.quote(rel, safe='')}/raw?ref={urllib.parse.quote(ref)}"
            )
            if status == 200:
                logo = store_logo(raw, source="hooks")
    if logo is None and project.get("avatar_url"):
        status, raw, _ = http.get(f"{base}/projects/{pid}/avatar")
        if status == 200:
            logo = store_logo(raw, source="gitlab_avatar")
    host = urllib.parse.urlparse(base).hostname or ""
    full = project["path_with_namespace"]
    return {
        "key": f"{host}/{full.lower()}",
        "name": module,
        "title": facts["title"] or project.get("name") or project["path"],
        "description": project.get("description") or facts["description"],
        "source": {
            "kind": "gitlab",
            "host": host,
            "path": full,
            "url": project.get("web_url"),
            "private": project.get("visibility") != "public",
            "archived": bool(project.get("archived")),
            "fork": bool(project.get("forked_from_project")),
            "default_branch": project.get("default_branch"),
            "stars": project.get("star_count"),
            "group_avatar": bool((project.get("namespace") or {}).get("avatar_url")),
        },
        "hooks_logo": facts["logo"],
        "logo": logo,
        "cover": None,
        "last_activity_at": project.get("last_activity_at"),
    }


def _gitlab_group_logo(
    http: _Http, base: str, group_id: int, memo: dict[int, dict[str, Any] | None]
) -> dict[str, Any] | None:
    """The nearest ancestor group avatar (the group itself, else its parent, ...), memoised."""
    if group_id in memo:
        return memo[group_id]
    memo[group_id] = None
    try:
        group = http.json(f"{base}/groups/{group_id}?with_projects=false")
    except (CatalogHTTPError, urllib.error.URLError, OSError, ValueError):
        return None
    if group.get("avatar_url"):
        status, raw, _ = http.get(f"{base}/groups/{group_id}/avatar")
        if status == 200:
            memo[group_id] = store_logo(raw, source="gitlab_group_avatar", kind="group")
    elif group.get("parent_id"):
        memo[group_id] = _gitlab_group_logo(http, base, group["parent_id"], memo)
    return memo[group_id]


def _gitlab_namespace_avatar(
    http: _Http, base: str, ns: dict[str, Any], memo: dict[int, dict[str, Any] | None]
) -> dict[str, Any] | None:
    """The owning group's (or user's) avatar: a group logo, marked as such, never the app's own."""
    if ns.get("kind") == "group":
        return _gitlab_group_logo(http, base, ns["id"], memo)
    source = "gitlab_user_avatar"
    url = str(ns.get("avatar_url") or "")
    origin = f"{urllib.parse.urlparse(base).scheme}://{urllib.parse.urlparse(base).netloc}"
    if url.startswith("/"):
        status, raw, _ = http.get(origin + url)
        return store_logo(raw, source=source, kind="group") if status == 200 else None
    if url.startswith(origin + "/"):
        status, raw, _ = http.get(url)
        return store_logo(raw, source=source, kind="group") if status == 200 else None
    # A foreign host (gravatar): never send the GitLab token there.
    return (
        store_logo(_fetch_public(url), source=source, kind="group")
        if url.startswith("https://")
        else None
    )


def scan_gitlab(
    host: str,
    previous: dict[str, Any],
    extra_groups: list[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[SourceState, dict[str, dict[str, Any]]]:
    everything = is_self_hosted(host)
    source = SourceState(
        kind="gitlab",
        host=host,
        scope=(
            "everything the account can see"
            if everything
            else "member projects" + (f" + {len(extra_groups)} group(s)" if extra_groups else "")
        ),
    )
    token = credential(host)
    if not token:
        source.error = f"glab has no credential for {host}"
        return source, {}
    source.signed_in = True
    source.icon = fetch_host_icon(host)
    base = f"https://{host}/api/v4"
    http = _Http(token, "bearer")
    started = time.monotonic()
    projects: list[dict[str, Any]] = []
    try:
        try:
            source.account = http.json(f"{base}/user").get("username")
        except CatalogHTTPError as e:
            if e.status == 401:  # a PAT rather than an OAuth token: GitLab wants the other header
                http = _Http(token, "private-token")
                source.account = http.json(f"{base}/user").get("username")
            else:
                raise
        page = 1
        while True:
            batch = http.json(
                f"{base}/projects?{'' if everything else 'membership=true&'}per_page=100&page={page}&order_by=last_activity_at"
            )
            projects.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        for group in [] if everything else extra_groups or []:
            page = 1
            while True:
                batch = http.json(
                    f"{base}/groups/{urllib.parse.quote(group, safe='')}/projects?include_subgroups=true&per_page=100&page={page}"
                )
                projects.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        seen: set[int] = set()
        unique = []
        for proj in projects:
            if proj["id"] not in seen:
                seen.add(proj["id"])
                unique.append(proj)
        projects = unique
    except (CatalogHTTPError, urllib.error.URLError, OSError, ValueError) as e:
        source.error = f"GitLab listing failed: {e}"
        return source, {}
    source.repos = len(projects)
    prev_apps = (previous.get("remote") or {}) if previous else {}
    prev_by_key = {k: v for k, v in prev_apps.items() if k.startswith(f"{host}/")}

    def work(project: dict[str, Any]) -> dict[str, Any] | None:
        key = f"{host}/{project['path_with_namespace'].lower()}"
        try:
            return _gitlab_project(http, base, project, prev_by_key.get(key))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    apps: dict[str, dict[str, Any]] = {}
    done = 0
    namespace_of = {
        f"{host}/{p['path_with_namespace'].lower()}": (p.get("namespace") or {}) for p in projects
    }
    with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
        for entry in pool.map(work, projects):
            done += 1
            if entry:
                apps[entry["key"]] = entry
            if progress is not None and (done % 20 == 0 or done == len(projects)):
                progress(done, len(projects))
    group_memo: dict[int, dict[str, Any] | None] = {}
    for key, entry in apps.items():
        ns = namespace_of.get(key) or {}
        if (
            entry.get("logo")
            or ns.get("id") is None
            or (ns.get("kind") != "group" and not ns.get("avatar_url"))
        ):
            continue
        logo = _gitlab_namespace_avatar(http, base, ns, group_memo)
        if logo:
            apps[key] = {**entry, "logo": logo}
    source.apps = len(apps)
    source.scanned_at = time.time()
    source.duration_s = round(time.monotonic() - started, 2)
    source.state = "verified"
    return source, apps


# ------------------------------------------------------ marketplace registry
# Frappe's public app index (the one Pilot's Marketplace reads): 126 public apps with
# title, description, repo, logo, website, docs, categories and stars. Every entry is
# a remote tile; an installed or member app with the same repository or name is
# enriched (logo, description, links) rather than duplicated.
MARKETPLACE_HOST = "marketplace"
_MARKETPLACE_URL = "https://raw.githubusercontent.com/frappe/marketplace/main/apps.json"


def scan_marketplace(previous: dict[str, Any]) -> tuple[SourceState, dict[str, dict[str, Any]]]:
    source = SourceState(
        kind="marketplace",
        host=MARKETPLACE_HOST,
        account="frappe/marketplace",
        signed_in=True,
        scope="Frappe's public app index",
    )
    started = time.monotonic()
    try:
        raw = _fetch_public(_MARKETPLACE_URL)
        entries = json.loads((raw or b"[]").decode("utf-8"))
        if not isinstance(entries, list):
            raise ValueError("index is not a list")
    except (ValueError, OSError) as e:
        source.error = f"marketplace index unreadable: {e}"
        return source, {}
    source.icon = fetch_host_icon("frappe.io")
    prev_apps = (previous.get("remote") or {}) if previous else {}
    apps: dict[str, dict[str, Any]] = {}
    owner_avatar = _owner_avatar_memo()

    def work(entry: dict[str, Any]) -> dict[str, Any] | None:
        key = remote_key(entry.get("repo"))
        name = str(entry.get("name") or "").strip()
        if not key or not _APP_NAME_RE.match(name):
            return None
        prev = prev_apps.get(key) or {}
        logo = None
        if entry.get("logo_url"):
            if prev.get("registry_logo_url") == entry["logo_url"] and prev.get("logo"):
                logo = prev["logo"]
            else:
                logo = store_logo(
                    _fetch_public(str(entry["logo_url"]).replace(" ", "%20")), source="marketplace"
                )
        owner_repo = "/".join(key.split("/")[1:])
        if logo is None and key.startswith("github.com/") and "/" in owner_repo:
            owner = owner_repo.split("/")[0]
            logo = owner_avatar(owner, f"https://github.com/{owner}.png?size=200")
        return {
            "key": key,
            "name": name,
            "title": entry.get("title") or name,
            "description": entry.get("description"),
            "source": {
                "kind": "marketplace",
                "host": key.split("/")[0],
                "path": owner_repo,
                "url": entry.get("repo"),
                "private": False,
                "default_branch": None,
                "stars": entry.get("stars"),
                "categories": [c for c in (entry.get("categories") or []) if isinstance(c, str)],
                "website": entry.get("website") or None,
                "docs": entry.get("documentation") or None,
                "registry": True,
            },
            "hooks_logo": None,
            "logo": logo,
            "cover": None,
            "registry_logo_url": entry.get("logo_url"),
        }

    with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
        for row in pool.map(work, entries):
            if row:
                apps[row["key"]] = row
    source.repos = len(entries)
    source.apps = len(apps)
    source.scanned_at = time.time()
    source.duration_s = round(time.monotonic() - started, 2)
    source.state = "verified"
    return source, apps


def _enrich_from_registry(target: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """A member-repo or installed entry keeps its own facts; the registry fills gaps."""
    out = dict(target)
    if not out.get("logo") and registry.get("logo"):
        out["logo"] = registry["logo"]
    if not out.get("description") and registry.get("description"):
        out["description"] = registry["description"]
    src = dict(out.get("source") or {})
    for k in ("categories", "website", "docs", "stars"):
        if not src.get(k) and (registry.get("source") or {}).get(k):
            src[k] = registry["source"][k]
    src["registry"] = True
    out["source"] = src
    return out


# ---------------------------------------------------------- installed side

_LIVE_SCRIPT = r"""
for b in "$@"; do
  for a in "$b"/apps/*/; do
    a=${a%/}; n=${a##*/}
    [ -d "$a" ] || continue
    r=$(git -c safe.directory='*' -C "$a" remote get-url upstream 2>/dev/null || git -c safe.directory='*' -C "$a" remote get-url origin 2>/dev/null)
    ref=$(git -c safe.directory='*' -C "$a" rev-parse --abbrev-ref HEAD 2>/dev/null)
    h=$(ls -d "$a"/*/hooks.py 2>/dev/null | head -n 1)
    l=""; t=""
    if [ -n "$h" ]; then
      l=$(grep -hE '^[[:space:]]*app_logo_url[[:space:]]*=' "$h" 2>/dev/null | head -n 1)
      t=$(grep -hE '^[[:space:]]*app_title[[:space:]]*=' "$h" 2>/dev/null | head -n 1)
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$b" "$n" "$r" "$ref" "$h" "$l" "$t"
  done
done
"""


def _live_app_facts(container, bench_paths: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(bench_path, app) -> {remote, ref, hooks, logo, title}`` from ONE exec per container."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not bench_paths:
        return out
    try:
        code, raw = container.exec_run(["sh", "-c", _LIVE_SCRIPT, "sh", *bench_paths], demux=False)
    except Exception:  # docker errors: the live half is a bonus, never a failure
        return out
    if raw is None:
        return out
    for line in raw.decode("utf-8", "replace").splitlines():
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        bench, app, remote, ref, hooks, logo_line, title_line = cols[:7]
        logo = _HOOKS_LOGO_RE.search(logo_line + "\n") if logo_line else None
        title = _HOOKS_TITLE_RE.search(title_line + "\n") if title_line else None
        out[(bench, app)] = {
            "remote": _safe_remote_url(remote),
            "ref": ref or None,
            "hooks": hooks or None,
            "logo": logo.group(1).strip() if logo else None,
            "title": title.group(1).strip() if title else None,
        }
    return out


def _read_bench_file(container, path: str) -> bytes | None:
    try:
        code, raw = container.exec_run(["sh", "-c", 'cat "$1"', "sh", path], demux=False)
    except Exception:
        return None
    return raw if code == 0 and raw else None


def _installed_logo(
    container, bench: str, app: str, module_dir: str | None, logo: str | None
) -> dict[str, Any] | None:
    """The bench's own copy of the hooks.py logo: built asset first, source second."""
    if not logo:
        return None
    m = re.match(r"^/?assets/([^/]+)/(.+)$", logo)
    if not m:
        return None
    rest = m.group(2)
    candidates = [f"{bench}/sites/assets/{m.group(1)}/{rest}"]
    if module_dir:
        candidates.append(f"{module_dir}/public/{rest}")
    for path in candidates:
        stored = store_logo(_read_bench_file(container, path), source="hooks")
        if stored:
            return stored
    return None


def _remotes_file() -> Path:
    return catalog_dir() / "installed-remotes.json"


def _load_remotes() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_remotes_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_remotes(remotes: dict[str, dict[str, Any]]) -> None:
    try:
        catalog_dir().mkdir(parents=True, exist_ok=True)
        tmp = _remotes_file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(remotes), encoding="utf-8")
        os.replace(tmp, _remotes_file())
    except OSError:
        pass


# --------------------------------------------------- the bench on the host disk
# The bench workspace is a bind mount under the project dir (``../data:/workspace``,
# or the older ``..:/workspace``), so a STOPPED instance's apps are still readable
# here: the git remote from ``.git/config``, the ``hooks.py`` facts, and the logo
# from the built ``sites/assets`` (which carries build-output logos the repository
# source does not have) or the app's ``public/`` dir. Read-only, never written.
_GIT_REMOTE_RE = re.compile(r'^\[remote "([^"]+)"\]\s*$')
_GIT_URL_RE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$")


def _host_bench_dir(project: str, bench_path: str) -> Path | None:
    base = cwcli_home() / "projects" / project
    name = Path(bench_path).name
    for candidate in (base / "data" / name, base / name):
        if (candidate / "apps").is_dir():
            return candidate
    return None


def _git_remote_from_config(app_dir: Path) -> str | None:
    try:
        lines = (
            (app_dir / ".git" / "config").read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return None
    urls: dict[str, str] = {}
    current = None
    for line in lines:
        m = _GIT_REMOTE_RE.match(line.strip())
        if m:
            current = m.group(1)
            continue
        if line.strip().startswith("["):
            current = None
            continue
        if current:
            u = _GIT_URL_RE.match(line)
            if u and current not in urls:
                urls[current] = u.group(1)
    return (
        urls.get("upstream") or urls.get("origin") or (next(iter(urls.values())) if urls else None)
    )


def _host_app_facts(project: str, bench_path: str, app: str) -> dict[str, Any] | None:
    """``{remote, hooks, logo, title, logo_bytes}`` for one app of a bench on the host disk."""
    bench = _host_bench_dir(project, bench_path)
    if bench is None:
        return None
    app_dir = bench / "apps" / app
    if not app_dir.is_dir():
        return None
    hooks = next(iter(sorted(app_dir.glob("*/hooks.py"))), None)
    facts: dict[str, Any] = {
        "remote": _safe_remote_url(_git_remote_from_config(app_dir)),
        "hooks": str(hooks) if hooks else None,
        "logo": None,
        "title": None,
        "logo_bytes": None,
    }
    if hooks:
        try:
            parsed = _hooks_facts(hooks.read_text(encoding="utf-8", errors="replace"))
            facts["logo"], facts["title"] = parsed["logo"], parsed["title"]
        except OSError:
            pass
    m = re.match(r"^/?assets/([^/]+)/(.+)$", facts["logo"] or "")
    if m:
        rest = m.group(2)
        for candidate in (
            bench / "sites" / "assets" / m.group(1) / rest,
            (hooks.parent / "public" / rest) if hooks else None,
        ):
            if candidate and candidate.is_file():
                try:
                    facts["logo_bytes"] = candidate.read_bytes()[: LOGO_MAX_BYTES + 1]
                    break
                except OSError:
                    pass
    return facts


def _cached_report(project: str) -> list[dict[str, Any]]:
    """The benches cwcli remembers for ``project``: live-tiered when it is on Docker, cache-only otherwise."""
    try:
        report = core_inspect.inspect(project, refresh="auto", offer_choice=False).data
        if report is not None:
            return [
                {
                    "index": b.index,
                    "path": b.path,
                    "label": b.label,
                    "available_apps": list(b.available_apps),
                    "sites": [
                        {"name": s.name, "installed_apps": list(s.installed_apps)} for s in b.sites
                    ],
                }
                for b in report.benches
            ]
    except CwcliError:
        pass
    data = db_utils.get_cached_project_data(project)
    return [
        {
            "index": b.get("index"),
            "path": b.get("path"),
            "label": b.get("label"),
            "available_apps": list(b.get("available_apps") or []),
            "sites": [
                {"name": s["name"], "installed_apps": list(s.get("installed_apps") or [])}
                for s in b.get("sites") or []
            ],
        }
        for b in (data or {}).get("bench_instances") or []
    ]


def installed_apps(
    *, logo_for: Callable[[str, str], dict[str, Any] | None] | None = None
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """``app key -> {name, title, installs, logo}`` across every remembered project.

    The key is the normalized git remote when the live exec read one, else
    ``local:<name>``; the merge step later folds a ``local:`` app onto a remote
    one of the same name when no remote was readable, flagged ``remembered``.
    """
    warnings: list[dict[str, str]] = []
    try:
        live = {d.project_name: d.status for d in (list_instances().data or [])}
    except CwcliError as e:
        live = None
        warnings.append({"code": "catalog.docker_unreachable", "text": e.message})
    names = sorted({p.name for p in db_utils.get_all_cached_projects()} | set(live or {}))
    # The cache records no git remote, so the identity read live from a running
    # container is REMEMBERED here (never claimed verified) for the stopped case.
    remembered = _load_remotes()
    remembered_changed = False
    apps: dict[str, dict[str, Any]] = {}
    for project in names:
        state = "unverified" if live is None else ("present" if project in live else "absent")
        running = bool(live and live.get(project) == "running")
        benches = _cached_report(project)
        facts: dict[tuple[str, str], dict[str, Any]] = {}
        container = None
        if running and benches:
            try:
                container = core_docker.get_frappe_container(project)
                facts = _live_app_facts(container, [b["path"] for b in benches if b.get("path")])
            except CwcliError as e:
                warnings.append(
                    {"code": "catalog.live_read_failed", "text": f"{project}: {e.message}"}
                )
        for bench in benches:
            by_site: dict[str, list[dict[str, Any]]] = {}
            for site in bench["sites"]:
                for entry in site["installed_apps"]:
                    parsed = _parse_app_line(entry)
                    if parsed["name"]:
                        by_site.setdefault(parsed["name"], []).append(
                            {
                                "name": site["name"],
                                "version": parsed["version"],
                                "branch": parsed["branch"],
                            }
                        )
            for app in sorted(set(bench["available_apps"]) | set(by_site)):
                if not _APP_NAME_RE.match(app):
                    continue  # a stray file or a non-app entry in apps/, not an app
                fact = facts.get((bench["path"], app), {})
                host_fact = None if fact else _host_app_facts(project, bench["path"], app)
                if host_fact:
                    fact = {
                        "remote": host_fact["remote"],
                        "ref": None,
                        "hooks": host_fact["hooks"],
                        "logo": host_fact["logo"],
                        "title": host_fact["title"],
                        "from_disk": True,
                    }
                mem_key = f"{project}|{bench['path']}|{app}"
                if fact:
                    if (
                        fact.get("remote")
                        and remembered.get(mem_key, {}).get("remote") != fact["remote"]
                    ):
                        remembered[mem_key] = {"remote": fact["remote"], "ref": fact.get("ref")}
                        remembered_changed = True
                elif mem_key in remembered:
                    fact = {
                        "remote": remembered[mem_key]["remote"],
                        "ref": remembered[mem_key].get("ref"),
                        "remembered": True,
                    }
                key = remote_key(fact.get("remote")) or f"local:{app}"
                row = apps.setdefault(
                    key,
                    {
                        "key": key,
                        "name": app,
                        "title": app,
                        "installs": [],
                        "logo": None,
                        "hooks_logo": None,
                    },
                )
                if fact.get("title") and row["title"] == app:
                    row["title"] = fact["title"]
                if fact.get("logo") and not row["hooks_logo"]:
                    row["hooks_logo"] = fact["logo"]
                if (
                    row["logo"] is None
                    and container is not None
                    and fact.get("logo")
                    and not fact.get("from_disk")
                ):
                    module_dir = os.path.dirname(fact["hooks"]) if fact.get("hooks") else None
                    row["logo"] = _installed_logo(
                        container, bench["path"], app, module_dir, fact["logo"]
                    )
                if row["logo"] is None and host_fact and host_fact.get("logo_bytes"):
                    row["logo"] = store_logo(host_fact["logo_bytes"], source="hooks")
                row["installs"].append(
                    asdict(
                        AppInstall(
                            project=project,
                            project_state=state,
                            bench_index=bench["index"],
                            bench_label=bench["label"],
                            bench_path=bench["path"],
                            sites=by_site.get(app, []),
                            remote=key if not key.startswith("local:") else None,
                            checked_out=fact.get("ref"),
                            verified=bool(fact)
                            and not fact.get("remembered")
                            and not fact.get("from_disk"),
                        )
                    )
                )
    if remembered_changed:
        _save_remotes(remembered)
    return apps, warnings


# ------------------------------------------------------------------ merge


_OWNER_AVATARS: dict[str, dict[str, Any] | None] = (
    {}
)  # ponytail: process-lifetime memo; a miss is not retried until restart


def _github_owner_avatar_cached(owner: str) -> dict[str, Any] | None:
    """One public fetch per GitHub owner for an installed-only app with no logo of its own."""
    key = owner.lower()
    if key not in _OWNER_AVATARS:
        _OWNER_AVATARS[key] = store_logo(
            _fetch_public(f"https://github.com/{owner}.png?size=200"),
            source="github_owner_avatar",
            kind="group",
        )
    return _OWNER_AVATARS[key]


def _merge(
    remote: dict[str, dict[str, Any]],
    remote_state: dict[str, str],
    installed: dict[str, dict[str, Any]],
) -> list[CatalogApp]:
    apps: dict[str, CatalogApp] = {}
    for key, r in remote.items():
        host = key.split("/", 1)[0]
        if (r.get("logo") or {}).get("source") == "github_card":
            r = {**r, "logo": None}  # pre-rule cache entries: a card is never a logo
        apps[key] = CatalogApp(
            key=key,
            name=r["name"],
            title=r.get("title") or r["name"],
            description=r.get("description"),
            state="remote",
            source=r.get("source"),
            logo=r.get("logo"),
            cover=r.get("cover"),
            remote_state=remote_state.get(host, "remembered"),
            hooks_logo=r.get("hooks_logo"),
            official=key.startswith(OFFICIAL_PREFIX),
        )
    by_name: dict[str, list[str]] = {}
    for key, app in apps.items():
        by_name.setdefault(app.name, []).append(key)
    # Remote-keyed installs attach first, so a bench whose files are gone (a
    # ``local:`` key) folds onto the remote its sibling benches resolved to.
    ordered = sorted(installed.items(), key=lambda kv: kv[0].startswith("local:"))
    for key, local in ordered:
        target = apps.get(key)
        if target is None and key.startswith("local:"):
            # No remote was readable (stopped instance, or no git remote): fold onto the
            # ONE remote app of the same name, honestly flagged by installs[].verified=False.
            candidates = by_name.get(local["name"], [])
            official = [k for k in candidates if k.startswith(OFFICIAL_PREFIX)]
            if len(candidates) == 1:
                target = apps[candidates[0]]
            elif candidates:
                # Several remotes carry this app name (forks and the upstream): the one a
                # live instance is actually installed from wins; else the official frappe
                # upstream; a tie between forks stays separate.
                ranked = sorted(candidates, key=lambda k: -len(apps[k].installs))
                if apps[ranked[0]].installs and len(apps[ranked[0]].installs) > len(
                    apps[ranked[1]].installs
                ):
                    target = apps[ranked[0]]
                elif len(official) == 1:
                    target = apps[official[0]]
                else:
                    exact = [k for k in candidates if k.rsplit("/", 1)[-1] == local["name"]]
                    if len(exact) == 1:
                        target = apps[exact[0]]
        if target is None:
            logo = local.get("logo")
            if logo is None and key.startswith("github.com/") and key.count("/") >= 2:
                logo = _github_owner_avatar_cached(key.split("/")[1])
            target = apps[key] = CatalogApp(
                key=key,
                name=local["name"],
                title=local.get("title") or local["name"],
                state="installed",
                logo=logo,
                remote_state="verified" if not key.startswith("local:") else "unverified",
                hooks_logo=local.get("hooks_logo"),
                source=_source_from_key(key),
            )
        else:
            target.state = "both"
            if target.logo is None and local.get("logo"):
                target.logo = local["logo"]
            if target.title == target.name and local.get("title"):
                target.title = local["title"]
        target.installs.extend(AppInstall(**i) for i in local["installs"])
    order = {"both": 0, "installed": 1, "remote": 2}

    def registry_only(a: CatalogApp) -> int:
        return 1 if (a.state == "remote" and (a.source or {}).get("kind") == "marketplace") else 0

    return sorted(apps.values(), key=lambda a: (order[a.state], registry_only(a), a.title.lower()))


def _source_from_key(key: str) -> dict[str, Any] | None:
    """A remote key with no scanned entry (a host nobody is signed in to): host + path only."""
    if key.startswith("local:"):
        return None
    host, _, path = key.partition("/")
    kind = (
        "github"
        if host.endswith("github.com")
        else "gitlab" if "gitlab" in host or host == "dev.egov.gy" else "git"
    )
    return {
        "kind": kind,
        "host": host,
        "path": path,
        "url": f"https://{host}/{path}",
        "private": None,
        "default_branch": None,
        "unscanned": True,
    }


# --------------------------------------------------------------- the store


class CatalogStore:
    """The daemon's one catalog: remote half on disk + memory, installed half re-read on demand.

    ``refresh(publish)`` runs the forge scan in a background thread and calls
    ``publish`` when it lands, so the page can re-read. ``get()`` never blocks on
    the network: it merges whatever remote data is held with a fresh installed read
    (memoized for ``installed_ttl`` seconds, since the live exec costs ~0.5s per
    running instance).
    """

    def __init__(self, *, installed_ttl: float = 30.0) -> None:
        self._lock = threading.Lock()
        self._disk = _load_disk()
        self._remote_state: dict[str, str] = {
            h: "remembered" for h in {k.split("/", 1)[0] for k in (self._disk.get("remote") or {})}
        }
        self._scanning = False
        self._progress: dict[str, dict[str, int]] = {}
        self._installed: tuple[float, dict, list] | None = None
        self._installed_ttl = installed_ttl
        self._installed_inflight = False

    @property
    def scanning(self) -> bool:
        return self._scanning

    def scanned_at(self) -> float | None:
        return self._disk.get("scanned_at")

    def ensure_host_icons(self) -> None:
        """Fetch any source icon the disk copy lacks, once per process, and persist it."""
        if getattr(self, "_icons_checked", False):
            return
        self._icons_checked = True
        changed = False
        with self._lock:
            rows = list(self._disk.get("sources") or [])
        for row in rows:
            if row.get("signed_in") and not row.get("icon"):
                row["icon"] = fetch_host_icon(row["host"])
                changed = changed or bool(row["icon"])
        if changed:
            with self._lock:
                self._disk["sources"] = rows
                try:
                    _save_disk(self._disk)
                except OSError:
                    pass

    def sources(self) -> list[SourceState]:
        rows = [SourceState(**s) for s in self._disk.get("sources") or []]
        hosts = {s.host for s in rows}
        for host in ["github.com", *gitlab_hosts()]:
            if host not in hosts:
                rows.append(
                    SourceState(
                        kind="github" if host.endswith("github.com") else "gitlab", host=host
                    )
                )
        for s in rows:
            s.state = self._remote_state.get(s.host, s.state if s.scanned_at else "unverified")
            s.progress = self._progress.get(s.host) if self._scanning else None
            if not s.scope:
                s.scope = (
                    "everything the account can see"
                    if (s.kind == "gitlab" and is_self_hosted(s.host))
                    else "member repositories"
                )
        return rows

    def refresh(self, publish: Callable[[dict[str, Any]], None] | None = None) -> bool:
        """Start a background forge scan; False when one is already running."""
        with self._lock:
            if self._scanning:
                return False
            self._scanning = True
        threading.Thread(
            target=self._scan, args=(publish,), daemon=True, name="app-catalog-scan"
        ).start()
        return True

    def _scan(self, publish) -> None:
        try:
            sources: list[SourceState] = []
            remote: dict[str, dict[str, Any]] = dict(self._disk.get("remote") or {})
            previous = self._disk
            extras = extra_sources()
            gh_orgs = [v.split(":", 1)[1] for v in extras if v.startswith("github:")]
            gh_source, gh_apps = scan_github(previous, gh_orgs)
            sources.append(gh_source)
            if gh_source.error is None:
                remote = {k: v for k, v in remote.items() if not k.startswith("github.com/")}
                remote.update(gh_apps)
            mk_source, mk_apps = scan_marketplace(previous)
            sources.append(mk_source)
            if mk_source.error is None:
                remote = {
                    k: v
                    for k, v in remote.items()
                    if (v.get("source") or {}).get("kind") != "marketplace"
                }
                for key, entry in mk_apps.items():
                    remote[key] = (
                        _enrich_from_registry(remote[key], entry) if key in remote else entry
                    )
            for host in gitlab_hosts():
                groups = [v.split(":", 1)[1] for v in extras if v.startswith(f"{host}:")]

                def _progress(done: int, total: int, host=host) -> None:
                    self._progress[host] = {"done": done, "total": total}
                    if publish is not None:
                        try:
                            publish({"progress": {"host": host, "done": done, "total": total}})
                        except Exception:
                            pass

                gl_source, gl_apps = scan_gitlab(host, previous, groups, progress=_progress)
                sources.append(gl_source)
                if gl_source.error is None:
                    remote = {k: v for k, v in remote.items() if not k.startswith(f"{host}/")}
                    remote.update(gl_apps)
            with self._lock:
                for s in sources:
                    self._remote_state[s.host] = "verified" if s.error is None else "unverified"
                self._disk = {
                    "version": CATALOG_VERSION,
                    "scanned_at": time.time(),
                    "sources": [asdict(s) for s in sources],
                    "remote": remote,
                }
                try:
                    _save_disk(self._disk)
                except OSError:
                    pass
        finally:
            with self._lock:
                self._scanning = False
                self._progress = {}
            if publish is not None:
                try:
                    publish({"scanned_at": self._disk.get("scanned_at")})
                except Exception:
                    pass

    def _read_installed(self) -> None:
        try:
            installed, warnings = installed_apps()
            self._installed = (time.time(), installed, warnings)
        finally:
            self._installed_inflight = False

    def get(self, *, force_installed: bool = False) -> Catalog:
        """The merged catalog. The installed half is memoized; a stale memo is served
        at once while ONE background re-read refreshes it (the live exec costs
        ~0.5s per running instance, and a page should never wait on it twice)."""
        self.ensure_host_icons()
        now = time.time()
        cached = self._installed
        stale = cached is None or now - cached[0] > self._installed_ttl
        if cached is None or (force_installed and not self._installed_inflight):
            self._installed_inflight = True
            self._read_installed()
            cached = self._installed
        elif stale and not self._installed_inflight:
            self._installed_inflight = True
            threading.Thread(
                target=self._read_installed, daemon=True, name="app-catalog-installed"
            ).start()
        assert cached is not None
        read_at, installed, warnings = cached
        with self._lock:
            remote = dict(self._disk.get("remote") or {})
            state = dict(self._remote_state)
        apps = _merge(remote, state, installed)
        return Catalog(
            scanned_at=self._disk.get("scanned_at"),
            scanning=self._scanning,
            sources=self.sources(),
            apps=apps,
            installed_read_at=read_at,
            warnings=list(warnings),
            extra_sources=extra_sources(),
            editors=installed_editors(),
            editor_paths=core_editors.editor_paths(),
        )
