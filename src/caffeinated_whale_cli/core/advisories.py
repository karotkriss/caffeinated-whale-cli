"""Advisories: security feeds merged, deduplicated and matched against what the
machine's instances actually run, built on the research in
``firstmate/data/cwcli-advisories-research/report.md`` (which corrects the first
spec in places). Detection and matching only; the fix is always named, and a
one-click action is offered only where cwcli already has the verb.

What the research settled, and this module follows:

* Frappe-family advisories come from GitHub's PER-REPOSITORY advisories
  (``/repos/<owner>/<repo>/security-advisories``), reached from the app NAME
  (frappe -> frappe/frappe, erpnext -> frappe/erpnext, hrms, lms, ...), the git
  remote being only a hint because forks point elsewhere. The global database
  and GraphQL are not used.
* OSV.dev is the backbone: GIT queries by repository URL plus the ``v<version>``
  tag for Frappe-family apps, ``querybatch`` for the bench venv's Python packages
  and each app's installed Node packages, and per source package in the release
  ecosystems (``Debian:13``, ``Alpine:v3.23``, ``Ubuntu:24.04:LTS``) for the
  images' OS packages.
* Versions are read from the SOURCE TREE (the app's ``__version__`` at HEAD) and
  the bench venv, never from cwcli's cache, and only live instances count.
* A GitHub range with only an upper bound (``<15.121.1``) applies to that
  bound's major only; a row with a lower bound applies as written; an installed
  major with no row is ``possible`` ("not stated for your release line"), never
  safe and never affected. A commit or tag answer only raises confidence and
  never clears what a range marks affected (forks, develop-only fixes).
* Enrichment comes from CVE.org (ETag, CNA scores, CISA SSVC/KEV data); the
  global tier is CISA KEV + EPSS + CIRT.gy's ``page-data.json`` alerts; Alpine
  secdb is kept natively; service binaries are matched on their upstream
  versions (MariaDB/server and redis/redis repository advisories, nginx.org).
  dev.egov.gy and NVD are out of phase 1.

Every source is fetched with its own validators (ETag / If-Modified-Since,
body hash otherwise) and records its own freshness and error, so one dead feed
never hides the others and the page can say "as of" per source. Refresh: hourly,
on "check now", a catch-up when the daemon starts stale, and a network-free
re-match after any change to what is installed (plus one OSV querybatch).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ..utils.config_utils import cwcli_home
from . import docker as core_docker
from . import git_refs
from .app_catalog import credential
from .list import list_instances

UA = "cwcli-advisories/1.0 (+https://github.com/karotkriss/caffeinated-whale-cli)"
HTTP_TIMEOUT = 25
CVEORG_PER_PASS = 80
OSV_DETAILS_PER_PASS = 150
WATCH_SERVICES = ("nginx", "mariadb", "redis")
# Frappe-family apps and the repository whose advisories cover them (by NAME).
FRAPPE_FAMILY = {
    "frappe": "frappe/frappe",
    "erpnext": "frappe/erpnext",
    "hrms": "frappe/hrms",
    "lms": "frappe/lms",
    "crm": "frappe/crm",
    "press": "frappe/press",
    "insights": "frappe/insights",
    "builder": "frappe/builder",
    "helpdesk": "frappe/helpdesk",
    "drive": "frappe/drive",
    "payments": "frappe/payments",
    "wiki": "frappe/wiki",
    "gameplan": "frappe/gameplan",
    "education": "frappe/education",
    "healthcare": "frappe/health",
    "webshop": "frappe/webshop",
    "print_designer": "frappe/print_designer",
    "raven": "The-Commit-Company/raven",
}
SERVICE_REPOS = {"mariadb": "MariaDB/server", "redis": "redis/redis"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "moderate": 2, "low": 3, "unknown": 4}
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.I)
_DISTRO_PREFIX = re.compile(r"^(DEBIAN|UBUNTU|ALPINE)-(CVE-\d{4}-\d{4,})$", re.I)
_KEYWORDS = re.compile(
    r"\b(nginx|node\.?js|python|mariadb|mysql|redis|frappe|erpnext|debian|alpine|ubuntu|openssl|glibc|docker|kernel|git)\b",
    re.I,
)


def _dir():
    return cwcli_home() / "cache" / "advisories"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------- HTTP (conditional)


class Fetcher:
    """GETs that reuse ETag / Last-Modified and fall back to a body hash, recording
    per-URL state (``fetched_at``, ``changed_at``, ``status``, ``failures``)."""

    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta

    def get(
        self, url: str, *, headers: dict[str, str] | None = None, timeout: int = HTTP_TIMEOUT
    ) -> tuple[int, bytes | None, bool]:
        """``(status, body, changed)``: body None on 304/error; changed False when the
        body hash equals the last one (servers without validators)."""
        h = {"User-Agent": UA, **(headers or {})}
        m = dict(self.meta.get(url) or {})
        if m.get("etag"):
            h["If-None-Match"] = m["etag"]
        if m.get("last_modified"):
            h["If-Modified-Since"] = m["last_modified"]
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                body = r.read()
                digest = hashlib.sha256(body).hexdigest()
                changed = digest != m.get("sha256")
                m.update(
                    {
                        "etag": r.headers.get("ETag"),
                        "last_modified": r.headers.get("Last-Modified"),
                        "sha256": digest,
                        "fetched_at": time.time(),
                        "status": r.status,
                        "failures": 0,
                    }
                )
                if changed:
                    m["changed_at"] = time.time()
                self.meta[url] = m
                return r.status, body, changed
        except urllib.error.HTTPError as e:
            if e.code == 304:
                m.update({"fetched_at": time.time(), "status": 304, "failures": 0})
                self.meta[url] = m
                return 304, None, False
            m.update(
                {
                    "status": e.code,
                    "failures": int(m.get("failures") or 0) + 1,
                    "last_error_at": time.time(),
                }
            )
            self.meta[url] = m
            return e.code, None, False
        except (urllib.error.URLError, OSError, ValueError) as e:
            m.update(
                {
                    "status": 0,
                    "failures": int(m.get("failures") or 0) + 1,
                    "last_error_at": time.time(),
                    "error": str(e)[:120],
                }
            )
            self.meta[url] = m
            return 0, None, False


def _post_json(url: str, payload: dict, *, timeout: int = 60) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def _get_json(
    url: str, *, headers: dict[str, str] | None = None, timeout: int = HTTP_TIMEOUT
) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------------ versions


def ver(text: str | None) -> Version | None:
    if not text:
        return None
    t = str(text).strip().lstrip("vV")
    try:
        return Version(t)
    except InvalidVersion:
        m = re.match(r"^(\d+(?:\.\d+){0,3})", t)
        if not m:
            return None
        try:
            return Version(m.group(1))
        except InvalidVersion:
            return None


def parse_range(raw: str | None) -> dict[str, Any]:
    """A GitHub free-text range -> ``{spec, major, lower_bounded, status}``.

    ``<16.35.0`` -> spec ``<16.35.0``, major 16 (applies to that major only);
    ``>= 15.0.0, < 15.55.0`` -> as written; ``tbd``/bare/unparsed -> status.
    """
    if not raw:
        return {
            "spec": None,
            "major": None,
            "lower_bounded": False,
            "status": "missing",
            "raw": raw,
        }
    text = raw.strip().replace("= ", "=").replace("< =", "<=").replace("> =", ">=").replace("v", "")
    if text.lower() in ("tbd", "n/a", "unknown", "-"):
        return {"spec": None, "major": None, "lower_bounded": False, "status": "tbd", "raw": raw}
    parts = [p.strip() for p in text.split(",") if p.strip()]
    lower = any(p.startswith((">", ">=")) for p in parts)
    try:
        SpecifierSet(",".join(parts), prereleases=True)
    except InvalidSpecifier:
        v = ver(text)
        if v is not None and re.match(r"^\d", text):
            return {
                "spec": f"=={v}",
                "major": v.major,
                "lower_bounded": True,
                "status": "bare",
                "raw": raw,
            }
        return {
            "spec": None,
            "major": None,
            "lower_bounded": False,
            "status": "unparsed",
            "raw": raw,
        }
    major = None
    if not lower:
        for p in parts:
            m = re.search(r"(\d+)\.", p)
            if m:
                major = int(m.group(1))
                break
    return {
        "spec": ",".join(parts),
        "major": major,
        "lower_bounded": lower,
        "status": "ok",
        "raw": raw,
    }


def range_verdict(installed: str | None, rows: list[dict[str, Any]]) -> tuple[str, str]:
    """``(status, reason)`` for an installed version against a package's rows:
    affected (a row for this major contains it), fixed (a row for this major and
    the version is outside), possible (no row for this release line, or tbd/unparsed)."""
    v = ver(installed)
    if v is None:
        return "possible", "installed version unreadable"
    same_line: list[dict[str, Any]] = []
    soft = False
    for r in rows:
        pr = r["parsed"]
        if pr["status"] in ("tbd", "unparsed", "missing"):
            soft = True
            continue
        if pr["lower_bounded"] or pr["major"] == v.major:
            same_line.append(r)
    for r in same_line:
        try:
            if v in SpecifierSet(r["parsed"]["spec"], prereleases=True):
                return "affected", f"{installed} in {r['parsed']['raw']}" + (
                    f" (fixed in {r['fixed']})" if r.get("fixed") else ""
                )
        except InvalidSpecifier:
            soft = True
    if same_line:
        return "fixed", f"{installed} outside {', '.join(r['parsed']['raw'] for r in same_line)}"
    if soft:
        return "possible", "the advisory's range is not machine-readable (tbd)"
    return "possible", f"not stated for your release line (v{v.major})"


# ------------------------------------------------------------------------ inventory

_SOURCE_VERSIONS = r"""
for a in "$1"/apps/*/; do
  a=${a%/}; n=${a##*/}
  [ -d "$a" ] || continue
  g="git -c safe.directory=* -C $a"
  head=$($g rev-parse HEAD 2>/dev/null)
  br=$($g rev-parse --abbrev-ref HEAD 2>/dev/null)
  url=$($g remote get-url upstream 2>/dev/null || $g remote get-url origin 2>/dev/null)
  mod=$(ls -d "$a"/*/hooks.py 2>/dev/null | head -n 1); mod=${mod%/hooks.py}
  v=""
  [ -n "$mod" ] && v=$(grep -m1 -E '^__version__' "$mod/__init__.py" 2>/dev/null | sed -E 's/.*["'"'"']([^"'"'"']+)["'"'"'].*/\1/')
  printf '%s\t%s\t%s\t%s\t%s\n' "$n" "$v" "$head" "$br" "$url"
done
"""
_PY_LIST = r'cd "$1" && env/bin/pip list --format=json 2>/dev/null'
_NODE = r"""
for a in "$1"/apps/*/; do
  a=${a%/}; n=${a##*/}
  if [ -d "$a/node_modules" ]; then
    for p in "$a"/node_modules/*/package.json "$a"/node_modules/@*/*/package.json; do
      [ -f "$p" ] || continue
      python3 - "$n" "$p" <<'PY' 2>/dev/null
import json,sys
try:
    d=json.load(open(sys.argv[2]))
    if d.get("name") and d.get("version"): print(f"{sys.argv[1]}\t{d['name']}\t{d['version']}")
except Exception: pass
PY
    done
  elif [ -f "$a/yarn.lock" ]; then
    awk -v app="$n" '
      /^[^ #].*:$/ { key=$0; sub(/:$/, "", key); gsub(/"/, "", key); next }
      /^  version / { v=$2; gsub(/"/, "", v); m=key; sub(/,.*$/, "", m); sub(/@[^@]*$/, "", m); print app "\t" m "\t" v }
    ' "$a/yarn.lock"
  fi
done
"""
_IMAGE = r"""
. /etc/os-release 2>/dev/null; echo "os	${ID:-?}	${VERSION_ID:-?}	${VERSION_CODENAME:-?}	${PRETTY_NAME:-?}"
[ -f /etc/alpine-release ] && echo "alpine	$(cat /etc/alpine-release)"
if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f='deb\t${Package}\t${Version}\t${source:Package}\t${source:Version}\n' 2>/dev/null; fi
if command -v apk >/dev/null 2>&1; then apk info -v 2>/dev/null | sed -n 's/^\(.*\)-\([0-9][^-]*-r[0-9]*\)$/apk\t\1\t\2/p'; fi
env | sed -n 's/^\(MARIADB_VERSION\|REDIS_VERSION\|NGINX_VERSION\|NODE_VERSION\|PYTHON_VERSION\)=\(.*\)$/env\t\1\t\2/p'
command -v redis-server >/dev/null 2>&1 && echo "service	redis	$(redis-server --version 2>/dev/null | sed -n 's/.*v=\([0-9.]*\).*/\1/p')"
command -v mariadbd >/dev/null 2>&1 && echo "service	mariadb	$(mariadbd --version 2>/dev/null | sed -n 's/.*Ver \([0-9.]*\).*/\1/p')"
command -v nginx >/dev/null 2>&1 && echo "service	nginx	$(nginx -v 2>&1 | sed -n 's#.*nginx/\([0-9.]*\).*#\1#p')"
"""
# The OS source packages worth asking OSV about (what a bench or service actually loads).
OS_SOURCES = {
    "openssl",
    "python3.11",
    "python3.12",
    "python3.13",
    "python3-defaults",
    "nodejs",
    "mariadb",
    "mariadb-10.11",
    "redis",
    "curl",
    "zlib",
    "libxml2",
    "expat",
    "libxslt",
    "git",
    "nginx",
    "glibc",
    "wkhtmltopdf",
    "libjpeg-turbo",
    "libpng1.6",
    "freetype",
    "ca-certificates",
    "sudo",
    "bash",
    "musl",
    "busybox",
    "libgcrypt20",
    "gnutls28",
    "systemd",
    "util-linux",
    "apt",
    "dpkg",
    "perl",
    "sqlite3",
    "libtasn1-6",
    "nettle",
    "p11-kit",
    "krb5",
    "libssh2",
    "libpsl",
    "nghttp2",
}


def collect_inventory(catalog_apps: list) -> dict[str, Any]:
    """What is installed, per RUNNING instance, read from the containers (never the cache)."""
    inv: dict[str, Any] = {"collected_at": time.time(), "projects": {}}
    try:
        running = {d.project_name for d in (list_instances().data or []) if d.status == "running"}
    except Exception:
        running = set()
    benches: dict[str, dict[str, str]] = {}
    for app in catalog_apps:
        for inst in getattr(app, "installs", None) or []:
            d = (
                inst
                if isinstance(inst, dict)
                else {k: getattr(inst, k, None) for k in ("project", "bench_index", "bench_path")}
            )
            if d["project"] in running:
                benches.setdefault(d["project"], {})[str(d["bench_index"])] = d["bench_path"]
    for project in sorted(running):
        try:
            container = core_docker.get_frappe_container(project)
        except Exception:
            continue
        p: dict[str, Any] = {"benches": {}, "images": {}}
        for bi, path in sorted((benches.get(project) or {}).items()):
            b: dict[str, Any] = {"path": path, "apps": {}, "python": {}, "node": {}}
            try:
                _c, out = container.exec_run(
                    ["sh", "-c", _SOURCE_VERSIONS, "sh", path], user="frappe", demux=False
                )
                for line in (out or b"").decode("utf-8", "replace").splitlines():
                    cols = line.split("\t")
                    if len(cols) >= 5 and cols[0]:
                        b["apps"][cols[0]] = {
                            "version": cols[1] or None,
                            "head": cols[2] or None,
                            "branch": cols[3] or None,
                            "remote": cols[4] or None,
                            "version_source": "source __version__" if cols[1] else None,
                        }
            except Exception:
                pass
            try:
                _c, out = container.exec_run(
                    ["sh", "-c", _PY_LIST, "sh", path], user="frappe", demux=False
                )
                rows = json.loads((out or b"[]").decode("utf-8", "replace") or "[]")
                b["python"] = {
                    r["name"].lower(): r["version"]
                    for r in rows
                    if isinstance(r, dict) and r.get("name")
                }
            except Exception:
                b["python"] = {}
            try:
                _c, out = container.exec_run(["sh", "-c", _NODE, "sh", path], demux=False)
                node: dict[str, dict[str, str]] = {}
                for line in (out or b"").decode("utf-8", "replace").splitlines():
                    cols = line.split("\t")
                    if len(cols) == 3 and cols[1] and cols[2]:
                        node[cols[1]] = {"version": cols[2], "app": cols[0]}
                b["node"] = node
            except Exception:
                b["node"] = {}
            p["benches"][bi] = b
        try:
            containers = core_docker.get_project_containers(project) or []
        except Exception:
            containers = []
        for c in containers:
            if getattr(c, "status", "") != "running":
                continue
            role = (
                c.name[len(project) + 1 :].rsplit("-", 1)[0]
                if c.name.startswith(project + "-")
                else c.name
            )
            img: dict[str, Any] = {
                "image": (
                    c.image.tags[0]
                    if getattr(c.image, "tags", None)
                    else str(getattr(c.image, "id", ""))[:19]
                ),
                "os": None,
                "packages": {},
                "sources": {},
                "services": {},
                "env": {},
            }
            try:
                _c, out = c.exec_run(["sh", "-c", _IMAGE], demux=False)
                for line in (out or b"").decode("utf-8", "replace").splitlines():
                    cols = line.split("\t")
                    if cols[0] == "os" and len(cols) >= 5:
                        img["os"] = {
                            "id": cols[1],
                            "version": cols[2],
                            "codename": cols[3],
                            "name": cols[4],
                        }
                    elif cols[0] == "alpine" and len(cols) >= 2:
                        img["alpine"] = cols[1].strip()
                    elif cols[0] == "deb" and len(cols) >= 5:
                        img["packages"][cols[1]] = cols[2]
                        src = (cols[3] or cols[1]).split(" ")[0]
                        img["sources"][src] = cols[4] or cols[2]
                    elif cols[0] == "apk" and len(cols) >= 3:
                        img["packages"][cols[1]] = cols[2]
                        img["sources"][cols[1]] = cols[2]
                    elif cols[0] == "env" and len(cols) >= 3:
                        img["env"][cols[1]] = cols[2]
                    elif cols[0] == "service" and len(cols) >= 3 and cols[2]:
                        img["services"][cols[1]] = cols[2]
            except Exception:
                pass
            for k, name in (
                ("MARIADB_VERSION", "mariadb"),
                ("REDIS_VERSION", "redis"),
                ("NGINX_VERSION", "nginx"),
            ):
                if img["env"].get(k) and name not in img["services"]:
                    img["services"][name] = (
                        re.sub(r"^\d+:", "", img["env"][k]).split("+")[0].split("-")[0]
                    )
            p["images"][role] = img
        inv["projects"][project] = p
    return inv


def osv_ecosystem(img: dict[str, Any]) -> str | None:
    os_ = img.get("os") or {}
    if img.get("alpine"):
        return "Alpine:v" + ".".join(img["alpine"].split(".")[:2])
    if os_.get("id") == "debian" and os_.get("version"):
        return f"Debian:{os_['version'].split('.')[0]}"
    if os_.get("id") == "ubuntu" and os_.get("version"):
        return f"Ubuntu:{os_['version']}:LTS"
    return None


# ------------------------------------------------------------------- advisory set


def _adv(id_: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "aliases": [],
        "title": "",
        "summary": "",
        "severity": "unknown",
        "cvss": None,
        "severity_source": None,
        "published": None,
        "modified": None,
        "sources": [],
        "ranges": [],
        "signals": {},
        "matches": [],
    }
    base.update(kw)
    return base


class AdvisorySet:
    """Union-find over ids and aliases; one merged record per group."""

    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.alias: dict[str, str] = {}

    @staticmethod
    def _norm(i: str) -> str:
        i = i.strip().upper()
        m = _DISTRO_PREFIX.match(i)
        return m.group(2).upper() if m else i

    def add(self, adv: dict[str, Any]) -> dict[str, Any]:
        ids = [self._norm(i) for i in [adv["id"], *adv.get("aliases", [])] if i]
        canon = next((self.alias[i] for i in ids if i in self.alias), None)
        if canon is None:
            ghsa = next((i for i in ids if i.startswith("GHSA-")), None)
            cve = next((i for i in ids if i.startswith("CVE-")), None)
            canon = ghsa or cve or ids[0]
            self.by_id[canon] = _adv(canon)
        into = self.by_id[canon]
        for k in ("title", "summary", "published", "modified"):
            if not into.get(k) and adv.get(k):
                into[k] = adv[k]
        if SEVERITY_ORDER.get(adv.get("severity") or "unknown", 4) < SEVERITY_ORDER.get(
            into.get("severity") or "unknown", 4
        ):
            into["severity"] = adv["severity"]
            into["severity_source"] = adv.get("severity_source")
        if into.get("cvss") is None and adv.get("cvss") is not None:
            into["cvss"] = adv["cvss"]
        seen = {(s.get("name"), s.get("url")) for s in into["sources"]}
        for s in adv.get("sources", []):
            if (s.get("name"), s.get("url")) not in seen:
                into["sources"].append(s)
        into["ranges"].extend(adv.get("ranges", []))
        for k, v in (adv.get("signals") or {}).items():
            into["signals"][k] = v
        into["aliases"] = sorted((set(into["aliases"]) | set(ids)) - {canon})
        for i in ids + [canon]:
            self.alias[i] = canon
        return into


def _gh_headers() -> dict[str, str]:
    tok = credential("github.com")
    h = {"Accept": "application/vnd.github+json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


# ---------------------------------------------------------------------- sources


def source_github_repos(
    inv: dict[str, Any], fetcher: Fetcher, aset: AdvisorySet, prior: dict[str, Any]
) -> dict[str, Any]:
    """Repository advisories for every installed Frappe-family app (by name) and
    for the service binaries in use (MariaDB/server, redis/redis)."""
    repos: dict[str, str] = {}  # repo -> package name used in rows
    for p in inv["projects"].values():
        for b in p["benches"].values():
            for name in b["apps"]:
                if name in FRAPPE_FAMILY:
                    repos[FRAPPE_FAMILY[name]] = name
        for img in p["images"].values():
            for svc in img.get("services") or {}:
                if svc in SERVICE_REPOS:
                    repos[SERVICE_REPOS[svc]] = svc
    items: dict[str, Any] = dict(prior.get("items") or {})
    errors: list[str] = []
    headers = _gh_headers()
    for repo in sorted(repos):
        rows: list[dict[str, Any]] = []
        url: str | None = (
            f"https://api.github.com/repos/{repo}/security-advisories?per_page=100&state=published&sort=updated&direction=desc"
        )
        pages = 0
        while url and pages < 5:
            status, body, _ = fetcher.get(url, headers=headers)
            if status == 304 and pages == 0:
                rows = items.get(repo) or []
                break
            if status != 200 or not body:
                if status not in (304,):
                    errors.append(f"{repo}: HTTP {status or 'unreachable'}")
                break
            try:
                rows.extend(json.loads(body.decode("utf-8")))
            except ValueError:
                errors.append(f"{repo}: unreadable")
                break
            pages += 1
            url = None  # one page (100 newest) is enough for matching today's versions
        if rows:
            items[repo] = rows
    count = 0
    for repo, rows in items.items():
        pkg_name = repos.get(repo) or repo.split("/")[1]
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("ghsa_id") or r.get("withdrawn_at"):
                continue
            aliases = (
                [r["ghsa_id"]]
                + ([r["cve_id"]] if r.get("cve_id") else [])
                + [i.get("value") for i in r.get("identifiers", []) if i.get("value")]
            )
            sevs = r.get("cvss_severities") or {}
            cvss = (
                (r.get("cvss") or {}).get("score")
                or (sevs.get("cvss_v4") or {}).get("score")
                or (sevs.get("cvss_v3") or {}).get("score")
            )
            ranges = []
            for v in r.get("vulnerabilities") or []:
                pk = (v.get("package") or {}).get("name") or pkg_name
                ranges.append(
                    {
                        "source": "github",
                        "target_kind": "frappe-app" if pkg_name in FRAPPE_FAMILY else "service",
                        "target_name": (
                            pkg_name
                            if pkg_name in FRAPPE_FAMILY or pkg_name in SERVICE_REPOS
                            else pk
                        ),
                        "raw": v.get("vulnerable_version_range"),
                        "fixed": v.get("patched_versions") or v.get("first_patched_version"),
                        "parsed": parse_range(v.get("vulnerable_version_range")),
                    }
                )
            aset.add(
                _adv(
                    r["ghsa_id"],
                    aliases=sorted({a for a in aliases if a}),
                    title=r.get("summary") or r["ghsa_id"],
                    summary=(r.get("description") or "")[:1500],
                    severity=(r.get("severity") or "unknown").lower().replace("moderate", "medium"),
                    severity_source="GitHub",
                    cvss=cvss,
                    published=r.get("published_at"),
                    modified=r.get("updated_at"),
                    sources=[{"name": f"GitHub advisories ({repo})", "url": r.get("html_url")}],
                    ranges=ranges,
                )
            )
            count += 1
    return {
        "name": "GitHub repository advisories",
        "key": "github",
        "ok": not errors,
        "error": "; ".join(errors) or None,
        "count": count,
        "repos": sorted(repos),
        "items": items,
    }


def _osv_details(ids: list[str], details: dict[str, Any], budget: int) -> int:
    fetched = 0
    for vid in ids:
        if vid in details or fetched >= budget:
            continue
        try:
            details[vid] = _get_json(f"https://api.osv.dev/v1/vulns/{urllib.parse.quote(vid)}")
            fetched += 1
        except Exception:
            continue
    return fetched


def _osv_record(vid: str, d: dict[str, Any], src_url: str) -> dict[str, Any]:
    sev = str((d.get("database_specific") or {}).get("severity") or "unknown").lower()
    cvss = None
    for s in d.get("severity") or []:
        if s.get("type", "").startswith("CVSS") and s.get("score"):
            m = re.search(r"/AV:", s["score"])
            cvss = None if m else cvss
    ranges = []
    for a in d.get("affected") or []:
        pkg = a.get("package") or {}
        eco = pkg.get("ecosystem") or (
            "GIT" if (a.get("ranges") or [{}])[0].get("type") == "GIT" else ""
        )
        fixed = None
        introduced = None
        for rng in a.get("ranges") or []:
            for ev in rng.get("events") or []:
                if ev.get("fixed"):
                    fixed = ev["fixed"]
                if ev.get("introduced") and ev["introduced"] != "0":
                    introduced = ev["introduced"]
        ranges.append(
            {
                "source": "osv",
                "target_kind": "git" if eco == "GIT" else "package",
                "ecosystem": eco,
                "target_name": pkg.get("name") or (a.get("ranges") or [{}])[0].get("repo"),
                "introduced": introduced,
                "fixed": fixed,
                "raw": None,
                "parsed": {"status": "osv"},
                # GIT ranges name the fix by COMMIT; the tag list is what says which
                # releases still carry the bug (see fix_paths).
                "affected_versions": sorted(set(a.get("versions") or [])) if eco == "GIT" else [],
            }
        )
    return _adv(
        vid,
        aliases=sorted(set(d.get("aliases") or [])),
        title=d.get("summary") or vid,
        summary=(d.get("details") or "")[:1500],
        severity=sev if sev in SEVERITY_ORDER else "unknown",
        severity_source="OSV",
        cvss=cvss,
        published=d.get("published"),
        modified=d.get("modified"),
        sources=[{"name": "OSV.dev", "url": src_url}],
        ranges=ranges,
    )


def source_osv(inv: dict[str, Any], aset: AdvisorySet, prior: dict[str, Any]) -> dict[str, Any]:
    """OSV: GIT repo+tag for Frappe-family apps, PyPI/npm for installed packages,
    OS source packages per release ecosystem. Hits are recorded on the advisory
    (``_hits``) so matching needs no second query."""
    details: dict[str, Any] = dict(prior.get("details") or {})
    queries: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for project, p in inv["projects"].items():
        for bi, b in p["benches"].items():
            for name, a in b["apps"].items():
                if name in FRAPPE_FAMILY and a.get("version"):
                    queries.append(
                        {
                            "package": {
                                "ecosystem": "GIT",
                                "name": f"https://github.com/{FRAPPE_FAMILY[name]}",
                            },
                            "version": f"v{a['version']}",
                        }
                    )
                    labels.append(
                        {
                            "project": project,
                            "bench_index": bi,
                            "kind": "app",
                            "name": name,
                            "installed": a["version"],
                            "how": "OSV git tag",
                        }
                    )
            for name, v in b["python"].items():
                queries.append({"package": {"ecosystem": "PyPI", "name": name}, "version": v})
                labels.append(
                    {
                        "project": project,
                        "bench_index": bi,
                        "kind": "python-package",
                        "name": name,
                        "installed": v,
                        "how": "OSV PyPI",
                    }
                )
            for name, nv in b["node"].items():
                queries.append(
                    {"package": {"ecosystem": "npm", "name": name}, "version": nv["version"]}
                )
                labels.append(
                    {
                        "project": project,
                        "bench_index": bi,
                        "kind": "npm-package",
                        "name": name,
                        "installed": nv["version"],
                        "app": nv.get("app"),
                        "how": "OSV npm",
                    }
                )
        for role, img in p["images"].items():
            eco = osv_ecosystem(img)
            if not eco:
                continue
            services = {k.lower() for k in (img.get("services") or {})}
            for src, v in (img.get("sources") or {}).items():
                base = src.split("-")[0].lower()
                if base in services or src.lower() in services:
                    continue  # the image's own service (MariaDB/Redis/nginx packages): matched on its upstream version, never a distro feed
                if src in OS_SOURCES:
                    queries.append({"package": {"ecosystem": eco, "name": src}, "version": v})
                    labels.append(
                        {
                            "project": project,
                            "bench_index": None,
                            "kind": "os-package",
                            "name": f"{role}: {src}",
                            "installed": v,
                            "ecosystem": eco,
                            "how": f"OSV {eco}",
                        }
                    )
    hits: dict[str, list[dict[str, Any]]] = {}
    error = None
    try:
        for i in range(0, len(queries), 900):
            data = _post_json(
                "https://api.osv.dev/v1/querybatch", {"queries": queries[i : i + 900]}
            )
            for label, res in zip(labels[i : i + 900], data.get("results") or [], strict=False):
                for v in res.get("vulns") or []:
                    hits.setdefault(v["id"], []).append(label)
    except Exception as e:
        error = f"OSV: {e}"
    fetched = _osv_details(list(hits), details, OSV_DETAILS_PER_PASS)
    count = 0
    for vid, where in hits.items():
        d = details.get(vid) or {"id": vid}
        rec = _osv_record(vid, d, f"https://osv.dev/vulnerability/{vid}")
        adv = aset.add(rec)
        adv.setdefault("_hits", []).extend(where)
        count += 1
    return {
        "name": "OSV.dev",
        "key": "osv",
        "ok": error is None,
        "error": error,
        "count": count,
        "queried": len(queries),
        "hydrated": fetched,
        "pending_details": max(0, len(hits) - sum(1 for h in hits if h in details)),
        "details": details,
    }


def source_alpine(
    inv: dict[str, Any], fetcher: Fetcher, aset: AdvisorySet, prior: dict[str, Any]
) -> dict[str, Any]:
    releases: set[str] = set()
    for p in inv["projects"].values():
        for img in p["images"].values():
            if img.get("alpine"):
                releases.add(".".join(img["alpine"].split(".")[:2]))
    items: dict[str, Any] = dict(prior.get("items") or {})
    errors: list[str] = []
    for rel in sorted(releases):
        for repo in ("main", "community"):
            url = f"https://secdb.alpinelinux.org/v{rel}/{repo}.json"
            status, body, _ = fetcher.get(url)
            if status == 200 and body:
                try:
                    items[f"{rel}/{repo}"] = json.loads(body.decode("utf-8"))
                except ValueError:
                    errors.append(f"{rel}/{repo}: unreadable")
            elif status != 304:
                errors.append(f"{rel}/{repo}: HTTP {status or 'unreachable'}")
    watched = {
        "redis",
        "nginx",
        "openssl",
        "libssl3",
        "libcrypto3",
        "zlib",
        "musl",
        "busybox",
        "curl",
    }
    count = 0
    for key, data in items.items():
        rel = key.split("/")[0]
        for pk in (data or {}).get("packages") or []:
            pkg = pk.get("pkg") or {}
            if pkg.get("name") not in watched:
                continue
            for fixed_ver, cves in (pkg.get("secfixes") or {}).items():
                if fixed_ver == "0":
                    continue
                for cve in cves or []:
                    cid = cve.split()[0]
                    if not _CVE_RE.match(cid):
                        continue
                    aset.add(
                        _adv(
                            cid.upper(),
                            title=f"{pkg['name']}: {cid} (Alpine {rel})",
                            sources=[
                                {
                                    "name": f"Alpine secdb v{rel}",
                                    "url": f"https://security.alpinelinux.org/vuln/{cid}",
                                }
                            ],
                            ranges=[
                                {
                                    "source": "alpine",
                                    "target_kind": "os-package",
                                    "target_name": pkg["name"],
                                    "ecosystem": f"Alpine:v{rel}",
                                    "fixed": fixed_ver,
                                    "raw": f"< {fixed_ver}",
                                    "parsed": {
                                        "status": "ok",
                                        "spec": f"<{fixed_ver.split('-r')[0]}",
                                        "major": None,
                                        "lower_bounded": True,
                                        "raw": f"< {fixed_ver}",
                                    },
                                }
                            ],
                        )
                    )
                    count += 1
    return {
        "name": "Alpine secdb",
        "key": "alpine",
        "ok": not errors,
        "error": "; ".join(errors) or None,
        "count": count,
        "items": items,
    }


def source_nginx(fetcher: Fetcher, aset: AdvisorySet, prior: dict[str, Any]) -> dict[str, Any]:
    items: dict[str, Any] = dict(prior.get("items") or {})
    status, body, _ = fetcher.get("https://nginx.org/en/security_advisories.html")
    error = None
    if status == 200 and body:
        rows = []
        for block in re.split(r"<li>", body.decode("utf-8", "replace"))[1:]:
            plain = html.unescape(re.sub(r"<[^>]+>", " ", block))
            cves = sorted({c.upper() for c in _CVE_RE.findall(plain)})
            if not cves:
                continue
            title = re.sub(r"\s+", " ", plain.split("Severity")[0]).strip()[:160]
            m_sev = re.search(r"Severity:\s*(\w+)", plain)
            m_nv = re.search(r"Not vulnerable:\s*(.+?)(?:\s{2,}|Vulnerable:)", plain, re.S)
            m_v = re.search(r"Vulnerable:\s*(.+?)(?:\s{2,}|$)", plain, re.S)
            rows.append(
                {
                    "cves": cves,
                    "title": title,
                    "severity": (m_sev.group(1).lower() if m_sev else "unknown"),
                    "fixed": re.sub(r"\s+", " ", m_nv.group(1)).strip() if m_nv else None,
                    "range": re.sub(r"\s+", " ", m_v.group(1)).strip() if m_v else None,
                }
            )
        items["page"] = rows
    elif status != 304:
        error = f"HTTP {status or 'unreachable'}"
    count = 0
    for r in items.get("page") or []:
        aset.add(
            _adv(
                r["cves"][0],
                aliases=r["cves"][1:],
                title=f"nginx: {r['title']}",
                severity=r["severity"] if r["severity"] in SEVERITY_ORDER else "unknown",
                severity_source="nginx.org",
                sources=[
                    {
                        "name": "nginx.org security advisories",
                        "url": "https://nginx.org/en/security_advisories.html",
                    }
                ],
                ranges=[
                    {
                        "source": "nginx",
                        "target_kind": "service",
                        "target_name": "nginx",
                        "raw": r.get("range"),
                        "fixed": r.get("fixed"),
                        "parsed": {"status": "text", "raw": r.get("range")},
                    }
                ],
            )
        )
        count += 1
    return {
        "name": "nginx.org",
        "key": "nginx",
        "ok": error is None,
        "error": error,
        "count": count,
        "items": items,
    }


def source_kev(fetcher: Fetcher, aset: AdvisorySet, prior: dict[str, Any]) -> dict[str, Any]:
    items: dict[str, Any] = dict(prior.get("items") or {})
    status, body, _ = fetcher.get(
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    )
    error = None
    if status == 200 and body:
        try:
            data = json.loads(body.decode("utf-8"))
            items["kev"] = {
                v["cveID"].upper(): {
                    "date_added": v.get("dateAdded"),
                    "due_date": v.get("dueDate"),
                    "ransomware": v.get("knownRansomwareCampaignUse"),
                    "vendor": v.get("vendorProject"),
                    "product": v.get("product"),
                    "name": v.get("vulnerabilityName"),
                }
                for v in data.get("vulnerabilities") or []
            }
            items["catalog_version"] = data.get("catalogVersion")
        except ValueError:
            error = "unreadable"
    elif status != 304:
        error = f"HTTP {status or 'unreachable'}"
    kev = items.get("kev") or {}
    # KEV entries are signals on advisories we already know; recently added ones are
    # also admitted on their own (the global tier) when they name a watched product.
    cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
    admitted = 0
    for cve, k in kev.items():
        canon = aset.alias.get(cve)
        text = f"{k.get('vendor','')} {k.get('product','')} {k.get('name','')}".lower()
        recent = False
        try:
            recent = (
                datetime.fromisoformat(k["date_added"]).replace(tzinfo=timezone.utc).timestamp()
                >= cutoff
            )
        except Exception:
            recent = False
        if canon is None and recent and _KEYWORDS.search(text):
            aset.add(
                _adv(
                    cve,
                    title=f"{k.get('vendor')} {k.get('product')}: {k.get('name')}",
                    published=k.get("date_added"),
                    sources=[
                        {
                            "name": "CISA KEV",
                            "url": "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
                        }
                    ],
                    ranges=[
                        {
                            "source": "kev",
                            "target_kind": "keyword",
                            "target_name": w.lower(),
                            "raw": None,
                            "fixed": None,
                            "parsed": {"status": "keyword"},
                        }
                        for w in sorted({x.lower() for x in _KEYWORDS.findall(text)})
                    ],
                )
            )
            canon = aset.alias.get(cve)
            admitted += 1
        if canon is not None:
            aset.by_id[canon]["signals"]["kev"] = {
                "date_added": k.get("date_added"),
                "due_date": k.get("due_date"),
                "ransomware": k.get("ransomware"),
            }
    return {
        "name": "CISA KEV",
        "key": "kev",
        "ok": error is None,
        "error": error,
        "count": len(kev),
        "admitted": admitted,
        "items": items,
    }


def _walk_cirt(o: Any, rows: list[dict[str, Any]], kind: str) -> None:
    if isinstance(o, dict):
        if o.get("slug") and o.get("title"):
            ident = re.match(r"^([a-z]{2,3}\d{4}_\d+)", o["slug"])
            rows.append(
                {
                    "id": (ident.group(1) if ident else o["slug"]).upper(),
                    "slug": o["slug"],
                    "title": html.unescape(o["title"]),
                    "date": o.get("date"),
                    "excerpt": html.unescape(re.sub(r"<[^>]+>", " ", o.get("excerpt") or ""))[:400],
                    "kind": kind,
                }
            )
        for v in o.values():
            _walk_cirt(v, rows, kind)
    elif isinstance(o, list):
        for v in o:
            _walk_cirt(v, rows, kind)


def source_cirt(fetcher: Fetcher, aset: AdvisorySet, prior: dict[str, Any]) -> dict[str, Any]:
    items: dict[str, Any] = dict(prior.get("items") or {})
    errors: list[str] = []
    for kind, url in (
        ("alert", "https://cirt.gy/page-data/AlertsPost/1/page-data.json"),
        ("advisory", "https://cirt.gy/page-data/AdvisoriesPost/1/page-data.json"),
    ):
        status, body, _ = fetcher.get(url)
        if status == 200 and body:
            try:
                data = json.loads(body.decode("utf-8"))
            except ValueError:
                errors.append(f"{kind}: unreadable")
                continue
            rows: list[dict[str, Any]] = []
            _walk_cirt(data, rows, kind)
            items[kind] = rows
        elif status != 304:
            errors.append(f"{kind}: HTTP {status or 'unreachable'}")
    count = 0
    for kind, rows in items.items():
        for r in rows or []:
            text = f"{r['title']} {r.get('excerpt','')}"
            cves = sorted({c.upper() for c in _CVE_RE.findall(text)})
            words = sorted(
                {
                    w.lower().replace("node.js", "node").replace("nodejs", "node")
                    for w in _KEYWORDS.findall(r["title"])
                }
            )
            adv = aset.add(
                _adv(
                    r["id"],
                    aliases=cves,
                    title=f"CIRT.gy {kind}: {r['title']}",
                    summary=r.get("excerpt") or "",
                    published=r.get("date"),
                    sources=[{"name": "CIRT.gy", "url": f"https://cirt.gy/article/{r['slug']}/"}],
                    ranges=[
                        {
                            "source": "cirt",
                            "target_kind": "keyword",
                            "target_name": w,
                            "raw": None,
                            "fixed": None,
                            "parsed": {"status": "keyword"},
                        }
                        for w in words
                    ],
                )
            )
            adv["signals"]["national_alert"] = r["id"]
            count += 1
    return {
        "name": "CIRT.gy",
        "key": "cirt",
        "ok": not errors,
        "error": "; ".join(errors) or None,
        "count": count,
        "items": items,
    }


def source_epss(aset: AdvisorySet, prior: dict[str, Any]) -> dict[str, Any]:
    cache: dict[str, Any] = dict(prior.get("cache") or {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cves = sorted({i for i in aset.alias if i.startswith("CVE-")})
    todo = [c for c in cves if (cache.get(c) or {}).get("date") != today][:600]
    error = None
    for i in range(0, len(todo), 100):
        chunk = todo[i : i + 100]
        try:
            data = _get_json(f"https://api.first.org/data/v1/epss?cve={','.join(chunk)}")
            for row in data.get("data") or []:
                cache[row["cve"].upper()] = {
                    "score": float(row.get("epss") or 0),
                    "percentile": float(row.get("percentile") or 0),
                    "date": today,
                }
        except Exception as e:
            error = f"EPSS: {e}"
            break
    for cve in cves:
        canon = aset.alias.get(cve)
        if canon and cve in cache:
            aset.by_id[canon]["signals"]["epss"] = cache[cve]
    return {
        "name": "FIRST EPSS",
        "key": "epss",
        "ok": error is None,
        "error": error,
        "count": len(todo),
        "cache": cache,
    }


def source_cveorg(fetcher: Fetcher, aset: AdvisorySet, prior: dict[str, Any]) -> dict[str, Any]:
    """Enrichment for CVE ids still lacking a score or a summary: CNA metrics, CISA ADP (SSVC, KEV)."""
    cache: dict[str, Any] = dict(prior.get("cache") or {})
    done = 0
    error = None
    for canon, adv in list(aset.by_id.items()):
        cves = [i for i in [canon, *adv["aliases"]] if i.startswith("CVE-")]
        if not cves:
            continue
        cve = cves[0]
        needs = adv.get("cvss") is None or not adv.get("summary")
        if cve not in cache and (not needs or done >= CVEORG_PER_PASS):
            continue
        if cve not in cache:
            status, body, _ = fetcher.get(f"https://cveawg.mitre.org/api/cve/{cve}")
            done += 1
            if status != 200 or not body:
                if status not in (304, 404):
                    error = f"CVE.org: HTTP {status or 'unreachable'}"
                continue
            try:
                d = json.loads(body.decode("utf-8"))
            except ValueError:
                continue
            cna = (d.get("containers") or {}).get("cna") or {}
            score = None
            for m in cna.get("metrics") or []:
                for k in ("cvssV4_0", "cvssV3_1", "cvssV3_0"):
                    if m.get(k, {}).get("baseScore") is not None:
                        score = m[k]["baseScore"]
                        break
                if score is not None:
                    break
            desc = next(
                (
                    x.get("value")
                    for x in cna.get("descriptions") or []
                    if x.get("lang", "").startswith("en")
                ),
                "",
            )
            ssvc = None
            kev = None
            for adp in (d.get("containers") or {}).get("adp") or []:
                for m in adp.get("metrics") or []:
                    other = m.get("other") or {}
                    if other.get("type") == "ssvc":
                        opts = {
                            k: v
                            for o in (other.get("content") or {}).get("options") or []
                            for k, v in o.items()
                        }
                        ssvc = opts.get("Exploitation")
                    if other.get("type") == "kev":
                        kev = other.get("content")
            cache[cve] = {
                "cvss": score,
                "summary": (desc or "")[:1500],
                "published": (d.get("cveMetadata") or {}).get("datePublished"),
                "ssvc_exploitation": ssvc,
                "kev": kev,
                "state": (d.get("cveMetadata") or {}).get("state"),
            }
        e = cache[cve]
        if adv.get("cvss") is None and e.get("cvss") is not None:
            adv["cvss"] = e["cvss"]
            if adv.get("severity") == "unknown":
                adv["severity"] = (
                    "critical"
                    if e["cvss"] >= 9
                    else "high" if e["cvss"] >= 7 else "medium" if e["cvss"] >= 4 else "low"
                )
                adv["severity_source"] = "CVE.org CNA"
        if not adv.get("summary") and e.get("summary"):
            adv["summary"] = e["summary"]
        if not adv.get("published") and e.get("published"):
            adv["published"] = e["published"]
        if e.get("ssvc_exploitation"):
            adv["signals"]["ssvc_exploitation"] = e["ssvc_exploitation"]
        if not any(s.get("name") == "CVE.org" for s in adv["sources"]):
            adv["sources"].append(
                {"name": "CVE.org", "url": f"https://www.cve.org/CVERecord?id={cve}"}
            )
    return {
        "name": "CVE.org",
        "key": "cveorg",
        "ok": error is None,
        "error": error,
        "count": done,
        "cache": cache,
    }


# ---------------------------------------------------------------------- matching


def _installed_set(inv: dict[str, Any]) -> dict[str, Any]:
    svc: set[str] = set()
    oses: set[str] = set()
    apps: set[str] = set()
    for p in inv["projects"].values():
        for b in p["benches"].values():
            apps |= set(b["apps"])
        for img in p["images"].values():
            svc |= set(img.get("services") or {})
            oses.add((img.get("os") or {}).get("id") or "")
            if img.get("alpine"):
                oses.add("alpine")
    return {"services": svc, "oses": oses, "apps": apps}


def match(aset: AdvisorySet, inv: dict[str, Any]) -> None:
    """Fill ``matches`` on every advisory from the inventory. Nothing is fetched."""
    present = _installed_set(inv)
    for adv in aset.by_id.values():
        matches: list[dict[str, Any]] = []
        # OSV hits are already version-precise
        for h in adv.pop("_hits", []) or []:
            kind = h["kind"]
            target = h["name"].split(": ", 1)[-1].lower()
            family_repo = (FRAPPE_FAMILY.get(h["name"]) or "").lower()
            osv_rows = [
                r
                for r in adv["ranges"]
                if r.get("source") == "osv"
                and (r.get("target_name") or "").lower().endswith(target)
                or (family_repo and (r.get("target_name") or "").lower().endswith(family_repo))
            ]
            if kind == "os-package":
                osv_rows = [r for r in osv_rows if r.get("ecosystem") == h.get("ecosystem")]
            fixed = next((r.get("fixed") for r in osv_rows if r.get("fixed")), None)
            if kind == "os-package" and not fixed:
                matches.append(
                    {
                        **h,
                        "status": "possible",
                        "precision": "no-distro-fix",
                        "fixed": None,
                        "how": f"{h.get('ecosystem')} lists it as open with no fixed version yet",
                        "action": None,
                    }
                )
                continue
            action = None
            if kind == "app" and h["name"] in FRAPPE_FAMILY:
                action = {
                    "verb": "apps-update",
                    "apps": h["name"],
                    "project": h["project"],
                    "bench": h["bench_index"],
                }
            elif kind in ("python-package", "npm-package"):
                action = {
                    "verb": "apps-update",
                    "apps": "",
                    "project": h["project"],
                    "bench": h["bench_index"],
                    "reason": "reinstall requirements",
                }
            matches.append(
                {
                    **h,
                    "status": "affected",
                    "precision": "tag" if kind == "app" else "version",
                    "fixed": fixed,
                    "action": action,
                }
            )
        # GitHub per-repo rows, scoped per major, against the source-tree version
        gh_rows = [r for r in adv["ranges"] if r.get("source") == "github"]
        if gh_rows:
            for project, p in inv["projects"].items():
                for bi, b in p["benches"].items():
                    for name, a in b["apps"].items():
                        rows = [
                            r
                            for r in gh_rows
                            if (r.get("target_name") or "").lower() == name.lower()
                        ]
                        if not rows:
                            continue
                        status, reason = range_verdict(a.get("version"), rows)
                        if status in ("affected", "possible"):
                            fork = bool(a.get("remote")) and not (
                                a["remote"] or ""
                            ).lower().startswith(
                                f"https://github.com/{FRAPPE_FAMILY.get(name, '').lower()}"
                            )
                            iv = ver(a.get("version"))
                            fixed = next(
                                (
                                    r.get("fixed")
                                    for r in rows
                                    if iv is not None
                                    and r["parsed"].get("major") == iv.major
                                    and r.get("fixed")
                                ),
                                None,
                            ) or next((r.get("fixed") for r in rows if r.get("fixed")), None)
                            matches.append(
                                {
                                    "project": project,
                                    "bench_index": bi,
                                    "kind": "app",
                                    "name": name,
                                    "installed": a.get("version"),
                                    "fixed": fixed,
                                    "status": status,
                                    "how": reason + (" · fork, matched by version" if fork else ""),
                                    "precision": (
                                        "version-range (fork)" if fork else "version-range"
                                    ),
                                    "action": (
                                        None
                                        if (status != "affected" or fork)
                                        else {
                                            "verb": "apps-update",
                                            "apps": name,
                                            "project": project,
                                            "bench": bi,
                                        }
                                    ),
                                    "note": (
                                        "rebase the fork onto the fixed version"
                                        if fork and status == "affected"
                                        else None
                                    ),
                                }
                            )
                for role, img in p["images"].items():
                    for svc, installed in (img.get("services") or {}).items():
                        rows = [r for r in gh_rows if (r.get("target_name") or "").lower() == svc]
                        if not rows:
                            continue
                        status, reason = range_verdict(installed, rows)
                        if status in ("affected", "possible"):
                            matches.append(
                                {
                                    "project": project,
                                    "bench_index": None,
                                    "kind": "service",
                                    "name": f"{role}: {svc}",
                                    "installed": installed,
                                    "fixed": next(
                                        (r.get("fixed") for r in rows if r.get("fixed")), None
                                    ),
                                    "status": status,
                                    "how": reason,
                                    "precision": "version-range",
                                    "action": None,
                                    "note": "recreate the instance on a newer image",
                                }
                            )
        # Alpine secdb rows against apk packages / services
        for r in [x for x in adv["ranges"] if x.get("source") == "alpine"]:
            for project, p in inv["projects"].items():
                for role, img in p["images"].items():
                    if (
                        not img.get("alpine")
                        or r.get("ecosystem") != f"Alpine:v{'.'.join(img['alpine'].split('.')[:2])}"
                    ):
                        continue
                    installed = (img.get("packages") or {}).get(r["target_name"]) or (
                        img.get("services") or {}
                    ).get(r["target_name"])
                    vi, vf = ver(installed), ver((r.get("fixed") or "").split("-r")[0])
                    if installed and vi is not None and vf is not None and vi < vf:
                        matches.append(
                            {
                                "project": project,
                                "bench_index": None,
                                "kind": "os-package",
                                "name": f"{role}: {r['target_name']}",
                                "installed": installed,
                                "fixed": r.get("fixed"),
                                "status": "affected",
                                "how": f"Alpine fixes it in {r.get('fixed')}",
                                "precision": "version-range",
                                "action": None,
                                "note": "recreate the instance on a newer image",
                            }
                        )
        # nginx page rows against an nginx service
        for r in [x for x in adv["ranges"] if x.get("source") == "nginx"]:
            for project, p in inv["projects"].items():
                for role, img in p["images"].items():
                    installed = (img.get("services") or {}).get("nginx")
                    if not installed:
                        continue
                    matches.append(
                        {
                            "project": project,
                            "bench_index": None,
                            "kind": "service",
                            "name": f"{role}: nginx",
                            "installed": installed,
                            "fixed": r.get("fixed"),
                            "status": "possible",
                            "how": f"nginx range '{r.get('raw')}' is free text; check the advisory",
                            "precision": "text",
                            "action": None,
                        }
                    )
        # keyword rows (CIRT.gy, recent KEV) against names of things that run here
        kws = {r["target_name"] for r in adv["ranges"] if r.get("source") in ("cirt", "kev")}
        if kws:
            hit = sorted(
                w
                for w in kws
                if w in present["services"]
                or w in present["oses"]
                or w in present["apps"]
                or (w in ("python", "node", "git", "openssl", "glibc") and present["apps"])
            )
            if hit:
                matches.append(
                    {
                        "project": None,
                        "bench_index": None,
                        "kind": "keyword",
                        "name": ", ".join(hit),
                        "installed": None,
                        "fixed": None,
                        "status": "possible",
                        "how": "a national alert names something this machine runs; read it",
                        "precision": "keyword",
                        "action": None,
                    }
                )
        # dedupe
        seen: set[str] = set()
        uniq = []
        for m in matches:
            k = json.dumps(
                {x: m.get(x) for x in ("project", "bench_index", "kind", "name")}, sort_keys=True
            )
            if k not in seen:
                seen.add(k)
                uniq.append(m)
        adv["matches"] = uniq
        adv["affects"] = any(m["status"] == "affected" for m in uniq)
        adv["possible"] = bool(uniq) and not adv["affects"]


def global_tier(adv: dict[str, Any]) -> bool:
    sig = adv.get("signals") or {}
    if sig.get("kev") or sig.get("national_alert") or sig.get("ssvc_exploitation") == "active":
        return True
    epss = (sig.get("epss") or {}).get("score") or 0
    if epss >= 0.5:
        return True
    return bool(
        (adv.get("cvss") or 0) >= 9.0
        and any(
            r.get("target_kind") in ("frappe-app", "service", "os-package")
            for r in adv.get("ranges") or []
        )
    )


def fix_groups(affects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group affected matches by the fix (the research's UI rule): one row per
    (target name, fixed version) with the advisories and places it covers."""
    groups: dict[str, dict[str, Any]] = {}
    for a in affects:
        for m in a.get("matches") or []:
            if m.get("status") != "affected":
                continue
            key = f"{m['kind']}|{m['name']}|{m.get('fixed') or '?'}"
            g = groups.setdefault(
                key,
                {
                    "kind": m["kind"],
                    "name": m["name"],
                    "fixed": m.get("fixed"),
                    "installed": set(),
                    "places": set(),
                    "advisories": set(),
                    "worst": "unknown",
                    "action": m.get("action"),
                    "note": m.get("note"),
                    "fix": m.get("fix"),
                },
            )
            if m.get("installed"):
                g["installed"].add(m["installed"])
            g["places"].add(
                f"{m.get('project')}"
                + (f"/{m['bench_index']}" if m.get("bench_index") is not None else "")
            )
            g["advisories"].add(a["id"])
            if SEVERITY_ORDER.get(a.get("severity") or "unknown", 4) < SEVERITY_ORDER.get(
                g["worst"], 4
            ):
                g["worst"] = a.get("severity") or "unknown"
    out = []
    for g in groups.values():
        out.append(
            {
                **g,
                "installed": sorted(g["installed"]),
                "places": sorted(g["places"]),
                "advisories": sorted(g["advisories"]),
                "count": len(g["advisories"]),
            }
        )
    return sorted(out, key=lambda g: (SEVERITY_ORDER.get(g["worst"], 4), -g["count"], g["name"]))


# ---------------------------------------------------------------------- fix paths

_BRANCH_MAJOR = re.compile(r"^version-(\d+)$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_GH_REPO = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_DEP_BLOCK = re.compile(r"^dependencies\s*=\s*\[(.*?)^\]", re.M | re.S)
_DEP_ITEM = re.compile(r"""["']([^"']+)["']""")


def _newest_tag(remote: str | None, major: int | None) -> str | None:
    """The newest ``v<major>.x.y`` tag on the app's OWN remote (fork or upstream),
    read with one ``git ls-remote`` on the host; None when it cannot be read."""
    if not remote or major is None:
        return None
    refs = git_refs.list_remote_refs(remote)
    if not refs.ok:
        return None
    for t in refs.tags:  # newest-looking first
        if re.match(rf"^v?{major}\.\d+(\.\d+)?$", t):
            return t.lstrip("v")
    return None


def _pins(
    fetcher: Fetcher, cache: dict[str, Any], remote: str | None, branch: str | None
) -> dict[str, Any] | None:
    """Direct dependency pins at the TIP of ``branch`` on a GitHub-hosted app:
    ``{"python": {canonical: spec}, "node": {name: spec}}``, read from the raw
    ``pyproject.toml`` and ``package.json`` (ETag-cached); None when unreadable."""
    m = _GH_REPO.search(remote or "")
    if not m or not branch or branch == "HEAD":
        return None
    base = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{branch}"
    out: dict[str, Any] = {"python": {}, "node": {}}
    for fname, kind in (("pyproject.toml", "python"), ("package.json", "node")):
        url = f"{base}/{fname}"
        status, body, _ = fetcher.get(url)
        if status == 304 and url in cache:
            out[kind] = cache[url]
            continue
        if status == 404:
            cache[url] = {}
            continue
        if status != 200 or body is None:
            if url in cache:
                out[kind] = cache[url]
                continue
            return None
        text = body.decode("utf-8", "replace")
        pins: dict[str, str] = {}
        if kind == "python":
            blk = _DEP_BLOCK.search(text)
            for item in _DEP_ITEM.findall(blk.group(1)) if blk else []:
                try:
                    req = Requirement(item)
                except InvalidRequirement:
                    continue
                pins[canonicalize_name(req.name)] = item.strip()  # as written: Pillow~=12.3.0
        else:
            try:
                pj = json.loads(text)
            except ValueError:
                pj = {}
            for key in ("dependencies", "devDependencies"):
                for name, spec in (pj.get(key) or {}).items():
                    pins[str(name)] = str(spec)
        cache[url] = pins
        out[kind] = pins
    return out


def _spec_min(spec: str) -> Version | None:
    """The lowest version a pin admits (``~=12.3.0`` -> 12.3.0, ``>=1,<2`` -> 1)."""
    lows = []
    for part in spec.split(","):
        mm = re.match(r"^\s*(~=|>=|==|===)\s*v?([0-9][0-9A-Za-z.\-]*)", part)
        if mm:
            v = ver(mm.group(2).rstrip(".*"))
            if v is not None:
                lows.append(v)
    return max(lows) if lows else None


def _node_spec_contains(spec: str, v: Version) -> bool:
    """``^1.2.3`` admits 1.x >= 1.2.3, ``~1.2.3`` admits 1.2.x >= 1.2.3, a bare
    version only itself; ranges with ``||``/``>=`` fall back to the first bound."""
    s = spec.strip()
    first = re.search(r"(\d+)\.(\d+)\.(\d+)", s)
    if not first:
        return False
    base = ver(".".join(first.groups()))
    if base is None:
        return False
    if s.startswith("^"):
        return v.major == base.major and v >= base
    if s.startswith("~"):
        return v.major == base.major and v.minor == base.minor and v >= base
    if s.startswith(">="):
        return v >= base
    return v == base


def _fix(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "text": text, **extra}


def _app_fix(
    adv: dict[str, Any], m: dict[str, Any], app: dict[str, Any], newest: Callable[..., str | None]
) -> dict[str, Any]:
    name, installed, fixed = m["name"], m.get("installed") or "?", m.get("fixed")
    branch, remote = app.get("branch"), app.get("remote")
    bm = _BRANCH_MAJOR.match(branch or "")
    major = int(bm.group(1)) if bm else None
    upstream = FRAPPE_FAMILY.get(name)
    fork = bool(upstream and remote) and not re.match(
        rf"^https?://github\.com/{re.escape(upstream or '')}(\.git)?/?$", remote or "", re.I
    )
    who = f"the fork at {remote}" if fork else name
    if not fixed:
        return _fix("nofix", "no fix stated yet", short="no fix stated yet")
    if branch == "HEAD" or not branch:
        return _fix(
            "pinned_commit",
            f"pinned to a commit, not tracking a release branch; the fix ({fixed}) is not "
            f"reached by an update until a branch such as version-N is checked out",
            short="pinned to a commit, not on a release branch",
        )
    if major is None:
        return _fix(
            "not_release_branch",
            f"tracks {branch}, not a release branch; whether its tip carries the fix "
            f"({fixed}) is not stated",
            short=f"tracks {branch}; fix not stated for it",
        )
    latest = newest(remote, major)
    if latest is None:
        return _fix(
            "unchecked",
            f"could not read {remote} to find the newest {major}.x release; the fix is {fixed}",
            short=f"newest {major}.x release could not be read",
        )
    lv = ver(latest)
    if _SHA.match(fixed):
        versions: set[str] = set()
        for r in adv.get("ranges") or []:
            if r.get("source") == "osv" and r.get("target_kind") == "git":
                versions.update(r.get("affected_versions") or [])
        label = f"a commit after {installed}"
        if f"v{installed}" in versions and latest != installed and f"v{latest}" not in versions:
            return _fix(
                "ready",
                f"fixed in a commit after {installed}; update {name} to {latest}"
                + (" (its own remote carries that tag)" if fork else ""),
                short=f"fixed after {installed}; update to {latest} fixes this",
                latest=latest,
                fixed_label=label,
            )
        if f"v{latest}" in versions or latest == installed:
            return _fix(
                "notyet",
                f"fixed in a commit after {installed}; no {major}.x release carries it yet "
                f"({latest} is still listed as affected)",
                short=f"no {major}.x release carries the fix yet",
                latest=latest,
                fixed_label=label,
            )
        return _fix(
            "unknown_commit",
            f"fixed in commit {fixed[:10]}; the advisory does not say whether {latest} "
            f"carries it",
            short=f"fixed in a commit; not matched to a {major}.x release",
            latest=latest,
            fixed_label=f"commit {fixed[:10]}",
        )
    fv = ver(fixed)
    if fv is None:
        return _fix("unchecked", f"the fix is stated as '{fixed}'; check the advisory", short=fixed)
    if fv.major != major:
        return _fix(
            "other_major",
            f"the fix is {fixed} ({fv.major}.x); this bench tracks version-{major}, where no "
            f"fixed release is stated",
            short=f"fix is on {fv.major}.x, this bench tracks version-{major}",
            latest=latest,
        )
    if lv is not None and lv >= fv:
        return _fix(
            "ready",
            f"Update {name} to {latest} fixes this"
            + (f" (the tag is on {who}; upstream fixed it in {fixed})" if fork else ""),
            short=f"update to {latest} fixes this",
            latest=latest,
        )
    return _fix(
        "notyet",
        f"no {major}.x release carries the fix yet: newest on {who} is {latest}, the fix is "
        f"{fixed}" + (" (upstream has it; the fork has not pulled it)" if fork else ""),
        short=f"newest {major}.x is {latest}; fix is {fixed}",
        latest=latest,
    )


def _dep_fix(
    m: dict[str, Any], apps: dict[str, dict[str, Any]], pins_of: Callable[..., dict | None]
) -> dict[str, Any]:
    fixed = m.get("fixed")
    if not fixed:
        return _fix("nofix", "no fix stated yet", short="no fix stated yet")
    fv, iv = ver(fixed), ver(m.get("installed"))
    is_py = m["kind"] == "python-package"
    key = canonicalize_name(m["name"]) if is_py else m["name"]
    unread: list[str] = []
    order = sorted(apps, key=lambda n: (n != m.get("app"), n != "frappe", n))
    for app_name in order:
        a = apps[app_name]
        pins = pins_of(a.get("remote"), a.get("branch"))
        if pins is None:
            unread.append(app_name)
            continue
        spec = (pins["python"] if is_py else pins["node"]).get(key)
        if spec is None:
            continue
        branch = a.get("branch")
        src = f"{app_name}'s {'pyproject' if is_py else 'package.json'} on {branch}"
        if is_py:
            try:
                ss = Requirement(spec).specifier
                has_fix = fv is not None and ss.contains(fv, prereleases=True)
                keeps = iv is not None and ss.contains(iv, prereleases=True)
            except (InvalidRequirement, InvalidSpecifier):
                has_fix = keeps = False
                ss = SpecifierSet()
            low = _spec_min(str(ss))
            shown = spec
        else:
            has_fix = fv is not None and _node_spec_contains(spec, fv)
            keeps = iv is not None and _node_spec_contains(spec, iv)
            low = None
            shown = f"{m['name']} {spec}"
        if has_fix and not keeps:
            return _fix(
                "ready",
                f"pinned by {src} as {shown}; updating {app_name} reinstalls its "
                f"requirements and brings {fixed}",
                short=f"update {app_name} brings {fixed}",
                app=app_name,
                latest=fixed,
                action={
                    "verb": "apps-update",
                    "apps": app_name,
                    "project": m["project"],
                    "bench": m["bench_index"],
                },
            )
        if keeps or (low is not None and fv is not None and low < fv):
            return _fix(
                "pinned",
                f"pinned by {src} as {shown}; updating {app_name} does not bring {fixed}, "
                f"the pin has to move first",
                short=f"pinned by {app_name}; an update here does not bring {fixed}",
                app=app_name,
            )
        return _fix(
            "pinned",
            f"pinned by {src} as {shown}, which admits neither {m.get('installed')} nor "
            f"{fixed}; check the advisory",
            short=f"pinned by {app_name}; fix not reached by an update",
            app=app_name,
        )
    if unread:
        return _fix(
            "unchecked",
            f"the dependency pins of {', '.join(unread)} on their tracked branch could not be "
            f"read; whether an update brings {fixed} is unknown",
            short="pins could not be read",
        )
    return _fix(
        "transitive",
        f"not a direct dependency of any app here (pulled in by another package); no update "
        f"here brings {fixed}",
        short=f"not pinned by an app here; no update brings {fixed}",
    )


def fix_paths(
    aset: AdvisorySet, inv: dict[str, Any], fetcher: Fetcher, prior: dict[str, Any]
) -> dict[str, Any]:
    """Make every affected row's fix claim honest and specific (captain, 2026-10-28):
    an app row names the newest release on the branch the bench TRACKS, read from
    the app's own remote without starting the instance, and says when the fix is
    not on that branch (fork, pinned commit, other major, develop); a dependency
    row says who controls the fix (the pin on the tracked branch's tip) and
    offers an action only when an update actually brings the fixed version; a
    commit-only fix is named by version, never by hash. Sets ``m["fix"]`` and
    re-decides ``m["action"]``. Returns the pin cache to persist."""
    cache: dict[str, Any] = dict(prior.get("pins") or {})
    tag_memo: dict[tuple, str | None] = {}
    pin_memo: dict[tuple, dict | None] = {}

    def newest(remote: str | None, major: int | None) -> str | None:
        k = (remote, major)
        if k not in tag_memo:
            tag_memo[k] = _newest_tag(remote, major)
        return tag_memo[k]

    def pins_of(remote: str | None, branch: str | None) -> dict | None:
        k = (remote, branch)
        if k not in pin_memo:
            pin_memo[k] = _pins(fetcher, cache, remote, branch)
        return pin_memo[k]

    for adv in aset.by_id.values():
        for m in adv.get("matches") or []:
            if m.get("status") not in ("affected", "possible"):
                continue
            bench = ((inv["projects"].get(m.get("project")) or {}).get("benches") or {}).get(
                str(m.get("bench_index"))
            )
            apps = (bench or {}).get("apps") or {}
            if m["kind"] == "app":
                app = apps.get(m["name"])
                if not app:
                    continue
                m["fix"] = _app_fix(adv, m, app, newest)
            elif m["kind"] in ("python-package", "npm-package"):
                m["fix"] = _dep_fix(m, apps, pins_of)
            else:
                continue
            if m["fix"]["kind"] == "ready" and m["status"] == "affected":
                m["action"] = m["fix"].get("action") or {
                    "verb": "apps-update",
                    "apps": m["name"],
                    "project": m["project"],
                    "bench": m["bench_index"],
                }
            else:
                m["action"] = None
    return {"pins": cache}


# -------------------------------------------------------------------------- store


class AdvisoriesStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load("advisories.json")
        self._state: dict[str, Any] = self._load("state.json")
        self._checking = False
        self._progress: str | None = None

    def _load(self, name: str) -> dict[str, Any]:
        try:
            with open(_dir() / name, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, name: str, data: dict[str, Any]) -> None:
        try:
            os.makedirs(_dir(), exist_ok=True)
            tmp = _dir() / (name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, _dir() / name)
        except OSError:
            pass

    def stale(self, max_age_s: float = 3600) -> bool:
        return time.time() - float(self._data.get("checked_at") or 0) > max_age_s

    def set_state(
        self, adv_id: str, state: str, note: str | None = None, target: str | None = None
    ) -> dict[str, Any]:
        if state not in ("new", "read", "dismissed", "not_applicable"):
            raise ValueError("state must be new, read, dismissed or not_applicable")
        key = f"{adv_id}|{target}" if target else adv_id
        with self._lock:
            if state == "new":
                self._state.pop(key, None)
            else:
                self._state[key] = {"state": state, "note": (note or "")[:500], "at": _now_iso()}
            self._save("state.json", self._state)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._data)
            state = dict(self._state)
            checking, progress = self._checking, self._progress
        advs = [dict(a) for a in (data.get("advisories") or [])]
        for a in advs:
            st = state.get(a["id"]) or {}
            a["state"] = st.get("state") or "new"
            a["note"] = st.get("note") or ""
            a["state_at"] = st.get("at")
            a["global"] = global_tier(a)
            for m in a.get("matches") or []:
                ts = (
                    state.get(
                        f"{a['id']}|{m.get('project')}/{m.get('bench_index')}/{m.get('name')}"
                    )
                    or {}
                )
                m["state"] = ts.get("state") or "new"

        def key(a: dict[str, Any]) -> tuple:
            return (
                SEVERITY_ORDER.get(a.get("severity") or "unknown", 4),
                -(a.get("cvss") or 0),
                -((a.get("signals") or {}).get("epss", {}).get("score") or 0),
                a.get("published") or "",
            )

        affects = sorted([a for a in advs if a.get("affects")], key=key)
        possible = sorted([a for a in advs if a.get("possible")], key=key)
        others = sorted([a for a in advs if not a.get("matches")], key=key)
        global_ = sorted([a for a in others if a["global"]], key=key)
        return {
            "checked_at": data.get("checked_at"),
            "rematched_at": data.get("rematched_at"),
            "checking": checking,
            "progress": progress,
            "inventory_at": (data.get("inventory") or {}).get("collected_at"),
            "inventory_summary": data.get("inventory_summary"),
            "sources": data.get("sources") or [],
            "affects": affects,
            "possible": possible,
            "global": global_,
            "others": [a for a in others if not a["global"]],
            "fix_groups": fix_groups(affects),
            "counts": {
                "affects": len(affects),
                "affects_new": sum(
                    1
                    for a in affects
                    if a["state"] == "new"
                    and (a.get("first_seen") or 0) > (data.get("baseline_at") or 0) + 60
                ),
                "possible": len(possible),
                "global": len(global_),
                "others": len(others) - len(global_),
                "by_project": _count_by(affects, "project"),
            },
            "slowest_source": data.get("slowest_source"),
            "new_affecting": data.get("new_affecting") or [],
        }

    def refresh(self, catalog_apps: list, publish=None, *, rematch_only: bool = False) -> bool:
        with self._lock:
            if self._checking:
                return False
            self._checking = True
        threading.Thread(
            target=self._run,
            args=(catalog_apps, publish, rematch_only),
            daemon=True,
            name="advisories-check",
        ).start()
        return True

    def _fix_paths(self, aset: AdvisorySet, inv: dict, fetcher: Fetcher, prior: dict) -> dict:
        self._progress = "fix paths"
        try:
            return fix_paths(aset, inv, fetcher, prior.get("fixpaths") or {})
        except Exception:  # a dead remote never hides the matches themselves
            return prior.get("fixpaths") or {}

    def _run(self, catalog_apps: list, publish, rematch_only: bool) -> None:
        try:
            self._progress = "inventory"
            inv = collect_inventory(catalog_apps)
            summary = {
                "projects": len(inv["projects"]),
                "benches": sum(len(p["benches"]) for p in inv["projects"].values()),
                "apps": sum(
                    len(b["apps"]) for p in inv["projects"].values() for b in p["benches"].values()
                ),
                "python": sum(
                    len(b["python"])
                    for p in inv["projects"].values()
                    for b in p["benches"].values()
                ),
                "node": sum(
                    len(b["node"]) for p in inv["projects"].values() for b in p["benches"].values()
                ),
                "images": sum(len(p["images"]) for p in inv["projects"].values()),
            }
            prior = self._data.get("source_data") or {}
            meta = self._data.get("http_meta") or {}
            fetcher = Fetcher(meta)
            aset = AdvisorySet()
            if rematch_only and self._data.get("advisories"):
                # Cached advisories, fresh inventory, one OSV querybatch (seconds).
                for a in self._data["advisories"]:
                    rec = {**a, "matches": []}
                    rec.pop("_hits", None)
                    aset.by_id[a["id"]] = rec
                    for i in [a["id"], *a.get("aliases", [])]:
                        aset.alias[AdvisorySet._norm(i)] = a["id"]
                self._progress = "osv"
                osv = source_osv(inv, aset, prior.get("osv", {}))
                self._progress = "matching"
                match(aset, inv)
                fp = self._fix_paths(aset, inv, fetcher, prior)
                with self._lock:
                    sd = dict(self._data.get("source_data") or {})
                    sd["osv"] = {"details": osv.get("details")}
                    sd["fixpaths"] = fp
                    self._data = {
                        **self._data,
                        "advisories": list(aset.by_id.values()),
                        "inventory": inv,
                        "inventory_summary": summary,
                        "rematched_at": time.time(),
                        "source_data": sd,
                    }
                    self._save("advisories.json", self._data)
                return
            steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
                (
                    "github",
                    lambda: source_github_repos(inv, fetcher, aset, prior.get("github", {})),
                ),
                ("osv", lambda: source_osv(inv, aset, prior.get("osv", {}))),
                ("alpine", lambda: source_alpine(inv, fetcher, aset, prior.get("alpine", {}))),
                ("nginx", lambda: source_nginx(fetcher, aset, prior.get("nginx", {}))),
                ("cirt", lambda: source_cirt(fetcher, aset, prior.get("cirt", {}))),
                ("kev", lambda: source_kev(fetcher, aset, prior.get("kev", {}))),
                ("cveorg", lambda: source_cveorg(fetcher, aset, prior.get("cveorg", {}))),
                ("epss", lambda: source_epss(aset, prior.get("epss", {}))),
            ]
            sources: list[dict[str, Any]] = []
            source_data: dict[str, Any] = {}
            for key, fn in steps:
                self._progress = key
                t0 = time.monotonic()
                try:
                    res = fn()
                except Exception as e:  # one dead source never hides the others
                    res = {"name": key, "key": key, "ok": False, "error": str(e)[:200], "count": 0}
                secs = round(time.monotonic() - t0, 1)
                source_data[key] = {
                    k: v for k, v in res.items() if k in ("items", "details", "cache")
                }
                prev: dict[str, Any] = next(
                    (s for s in (self._data.get("sources") or []) if s.get("key") == key), {}
                )
                sources.append(
                    {
                        "name": res.get("name", key),
                        "key": key,
                        "ok": bool(res.get("ok")),
                        "error": res.get("error"),
                        "count": res.get("count", 0),
                        "seconds": secs,
                        "checked_at": time.time(),
                        "failures": 0 if res.get("ok") else int(prev.get("failures") or 0) + 1,
                        "extra": {
                            k: v
                            for k, v in res.items()
                            if k in ("repos", "queried", "hydrated", "pending_details", "admitted")
                        },
                    }
                )
            self._progress = "matching"
            match(aset, inv)
            source_data["fixpaths"] = self._fix_paths(aset, inv, fetcher, prior)
            prev = {a["id"]: a for a in (self._data.get("advisories") or [])}
            baseline = self._data.get("baseline_at") or time.time()
            now = time.time()
            advisories = []
            for a in aset.by_id.values():
                a.pop("_hits", None)
                a["first_seen"] = (prev.get(a["id"]) or {}).get("first_seen") or now
                advisories.append(a)
            new_affecting = [
                a["id"]
                for a in advisories
                if a.get("affects") and a["id"] not in prev and self._data.get("baseline_at")
            ]
            slowest = max(sources, key=lambda s: s["seconds"])["name"] if sources else None
            with self._lock:
                self._data = {
                    "checked_at": now,
                    "rematched_at": now,
                    "baseline_at": baseline,
                    "advisories": advisories,
                    "sources": sources,
                    "inventory": inv,
                    "inventory_summary": summary,
                    "source_data": source_data,
                    "http_meta": meta,
                    "slowest_source": slowest,
                    "new_affecting": new_affecting,
                }
                self._save("advisories.json", self._data)
        finally:
            with self._lock:
                self._checking = False
                self._progress = None
            if publish is not None:
                try:
                    publish(self.snapshot())
                except Exception:
                    pass


def _count_by(advs: list[dict[str, Any]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in advs:
        for m in a.get("matches") or []:
            if m.get("status") != "affected" or m.get(field) is None:
                continue
            k = str(m.get(field))
            out[k] = out.get(k, 0) + 1
    return out


def rss(snapshot: dict[str, Any], base_url: str) -> str:
    """An RSS 2.0 feed of the advisories that affect this machine (for a team reader)."""
    items = []
    for a in snapshot.get("affects") or []:
        matches: list[dict[str, Any]] = list(a.get("matches") or [])
        srcs: list[dict[str, Any]] = list(a.get("sources") or [])
        hits = "; ".join(
            f"{m.get('project')}{' bench ' + str(m['bench_index']) if m.get('bench_index') is not None else ''}: {m['name']} {m.get('installed') or ''}{' -> ' + m['fixed'] if m.get('fixed') else ''}"
            for m in matches
            if m.get("status") == "affected"
        )
        link = next((s.get("url") for s in srcs if s.get("url")), base_url)
        title = f"[{a.get('severity') or '?'}] {a.get('title') or a['id']}"
        pub = f"<pubDate>{html.escape(a['published'])}</pubDate>" if a.get("published") else ""
        items.append(
            f"<item><title>{html.escape(title)}</title><link>{html.escape(link or '')}</link><guid>{html.escape(a['id'])}</guid><description>{html.escape((a.get('summary') or '')[:400] + ' Hits: ' + hits)}</description>{pub}</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>cwcli advisories affecting this machine</title><link>{html.escape(base_url)}</link>"
        "<description>Security advisories matched against the instances cwcli manages here.</description>"
        f"{''.join(items)}</channel></rss>"
    )
