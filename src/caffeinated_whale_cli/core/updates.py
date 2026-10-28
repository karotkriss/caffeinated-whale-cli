"""Updates available for installed apps: which checked-out apps are behind the
branch they track, by how much where that can be counted, and for the core apps
(frappe, erpnext) which release is newest on the same major and whether a newer
major exists. Detection only - nothing here updates anything.

Per RUNNING instance, ONE exec per container runs, for every app dir on every
bench: ``git rev-parse HEAD``, the tracked branch, the remote URL, and
``git ls-remote <url> refs/heads/<branch>`` (the bench's own git and credentials,
so a private remote the bench could clone is readable here too). An app is
``behind`` when its HEAD is not the remote head. The COUNT of commits behind comes
from the forge's compare API (GitHub ``compare/{head}...{remote}``, GitLab
``repository/compare``) with the same signed-in ``gh``/``glab`` credentials the
catalog uses; where that cannot be asked, the count is honestly None.

Core releases come from GitHub's releases list for ``frappe/frappe`` and
``frappe/erpnext`` (public): the newest ``vX.Y.Z`` on the installed major is the
routine update; a newer major is flagged ``major_available`` separately and never
counted as a routine update; ``security`` is set when a release note between the
installed version and the newest same-major release mentions security, a CVE or a
vulnerability (a keyword flag, stated as such).

Results persist in ``cache/app-catalog/updates.json`` and refresh with the catalog
scan (hourly, Rescan) and on ``POST /api/v1/updates/check``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import docker as core_docker
from .app_catalog import _fetch_public, _Http, catalog_dir, credential
from .list import list_instances

CORE_APPS = ("frappe", "erpnext")
_RELEASE_REPOS = {"frappe": "frappe/frappe", "erpnext": "frappe/erpnext"}
_SECURITY_RE = re.compile(r"\b(security|cve-\d{4}-\d+|vulnerab\w*|xss|sql injection|rce)\b", re.I)
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_LOCAL_SCRIPT = r"""
for b in "$@"; do
  for a in "$b"/apps/*/; do
    a=${a%/}; n=${a##*/}
    [ -d "$a/.git" ] || [ -f "$a/.git" ] || continue
    g="git -c safe.directory=* -C $a"
    head=$($g rev-parse HEAD 2>/dev/null)
    br=$($g rev-parse --abbrev-ref HEAD 2>/dev/null)
    url=$($g remote get-url upstream 2>/dev/null || $g remote get-url origin 2>/dev/null)
    ver=$(sed -nE "s/^__version__ *= *['\"]([^'\"]*)['\"].*/\1/p" "$a/$n/__init__.py" 2>/dev/null | head -n 1)
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$b" "$n" "$head" "$br" "$url" "$ver"
  done
done
"""
# stdin: one "url<TAB>branch" per line; every unique pair is asked ONCE, in parallel,
# each bounded by its own timeout, so a container with many benches sharing the
# same frappe/erpnext remotes costs one round trip, not one per app.
_REMOTE_SCRIPT = r"""
while IFS="$(printf '\t')" read -r url br; do
  [ -n "$url" ] && [ -n "$br" ] || continue
  (
    sha=$(timeout 25 git -c safe.directory=* ls-remote "$url" "refs/heads/$br" 2>/dev/null | head -n 1 | cut -f1)
    printf '%s\t%s\t%s\n' "$url" "$br" "$sha"
  ) &
done
wait
"""


_sh_quote = shlex.quote


def _file():
    return catalog_dir() / "updates.json"


def _parse_version(text: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match((text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _host_path(url: str) -> tuple[str, str] | None:
    """``https://github.com/frappe/frappe.git`` / ``git@host:grp/proj.git`` -> (host, path)."""
    url = url.strip()
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1).lower(), m.group(2)
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if not p.hostname or not p.path:
        return None
    return p.hostname.lower(), p.path.strip("/").removesuffix(".git")


def _count_behind(
    host: str, path: str, head: str, remote_head: str, memo: dict[str, Any]
) -> int | None:
    key = f"{host}/{path}@{head}..{remote_head}"
    if key in memo:
        cached = memo[key]
        return int(cached) if cached is not None else None
    count: int | None = None
    token = credential(host)
    try:
        if host == "github.com":
            http = _Http(token, "bearer")
            status, raw, _ = http.get(
                f"https://api.github.com/repos/{path}/compare/{head}...{remote_head}",
                accept="application/vnd.github+json",
            )
            if status == 200:
                count = int(json.loads(raw.decode("utf-8")).get("ahead_by") or 0)
        elif token:
            http = _Http(token, "bearer")
            pid = urllib.parse.quote(path, safe="")
            status, raw, _ = http.get(
                f"https://{host}/api/v4/projects/{pid}/repository/compare?from={head}&to={remote_head}",
                accept="application/json",
            )
            if status == 401:
                http = _Http(token, "private-token")
                status, raw, _ = http.get(
                    f"https://{host}/api/v4/projects/{pid}/repository/compare?from={head}&to={remote_head}",
                    accept="application/json",
                )
            if status == 200:
                count = len(json.loads(raw.decode("utf-8")).get("commits") or [])
    except Exception:
        count = None
    memo[key] = count
    return count


def _releases(app: str, memo: dict[str, Any]) -> list[dict[str, Any]]:
    repo = _RELEASE_REPOS.get(app)
    if not repo:
        return []
    if repo in memo:
        return list(memo[repo])
    out: list[dict[str, Any]] = []
    for page in (1, 2):
        raw = _fetch_public(
            f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        )
        if not raw:
            break
        try:
            rows = json.loads(raw.decode("utf-8"))
        except ValueError:
            break
        if not isinstance(rows, list):
            break
        for r in rows:
            v = _parse_version(r.get("tag_name"))
            if v and not r.get("prerelease") and not r.get("draft"):
                out.append(
                    {
                        "version": v,
                        "tag": r.get("tag_name"),
                        "published_at": r.get("published_at"),
                        "security": bool(_SECURITY_RE.search(r.get("body") or "")),
                        "url": r.get("html_url"),
                    }
                )
        if len(rows) < 100:
            break
    memo[repo] = out
    return out


def _release_facts(app: str, installed: str | None, memo: dict[str, Any]) -> dict[str, Any] | None:
    cur = _parse_version(installed)
    if app not in _RELEASE_REPOS:
        return None
    rels = _releases(app, memo)
    if not rels:
        return {"installed": installed, "checked": False}
    same = [r for r in rels if cur and r["version"][0] == cur[0]]
    latest_same = max(same, key=lambda r: r["version"]) if same else None
    latest_any = max(rels, key=lambda r: r["version"])
    between = [r for r in same if cur and r["version"] > cur]
    return {
        "installed": installed,
        "checked": True,
        "latest_same_major": latest_same["tag"] if latest_same else None,
        "latest_same_major_url": latest_same["url"] if latest_same else None,
        "release_available": bool(cur and latest_same and latest_same["version"] > cur),
        "releases_behind": len(between),
        "security": any(r["security"] for r in between),
        "latest_major": latest_any["tag"],
        "major_available": bool(cur and latest_any["version"][0] > cur[0]),
    }


class UpdatesStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()
        self._checking = False

    def _load(self) -> dict[str, Any]:
        try:
            with open(_file(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(catalog_dir(), exist_ok=True)
            tmp = str(_file()) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, _file())
        except OSError:
            pass

    def record(self, items: list[dict[str, Any]]) -> None:
        """Store a finished pass (the ``check-updates`` job's path, in its own process)."""
        with self._lock:
            self._data = {"checked_at": time.time(), "items": items}
            self._save()

    def reload(self) -> None:
        """Re-read the file another process wrote (the daemon, after a check-updates job)."""
        with self._lock:
            self._data = self._load()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._data)
            checking = self._checking
        items = data.get("items") or []
        by_project: dict[str, int] = {}
        by_bench: dict[str, int] = {}
        for it in items:
            if it.get("update_available"):
                by_project[it["project"]] = by_project.get(it["project"], 0) + 1
                k = f"{it['project']}/{it['bench_index']}"
                by_bench[k] = by_bench.get(k, 0) + 1
        return {
            "checked_at": data.get("checked_at"),
            "checking": checking,
            "items": items,
            "summary": {
                "total": sum(1 for it in items if it.get("update_available")),
                "core": sum(1 for it in items if it.get("update_available") and it.get("core")),
                "security": sum(1 for it in items if (it.get("release") or {}).get("security")),
                "major": sum(1 for it in items if (it.get("release") or {}).get("major_available")),
                "by_project": by_project,
                "by_bench": by_bench,
            },
        }

    def stale(self, max_age_s: float = 3600) -> bool:
        return time.time() - float(self._data.get("checked_at") or 0) > max_age_s

    def refresh(self, catalog_apps: list, publish=None) -> bool:
        with self._lock:
            if self._checking:
                return False
            self._checking = True
        threading.Thread(
            target=self._run, args=(catalog_apps, publish), daemon=True, name="updates-check"
        ).start()
        return True

    def _run(self, catalog_apps: list, publish) -> None:
        try:
            try:
                items = check_updates(catalog_apps)
            except Exception as exc:  # a failed pass is reported, never a dead thread
                with self._lock:
                    self._data = {**self._data, "error": f"{type(exc).__name__}: {exc}"[:300]}
                    self._save()
                return
            with self._lock:
                self._data = {"checked_at": time.time(), "items": items}
                self._save()
        finally:
            with self._lock:
                self._checking = False
            if publish is not None:
                try:
                    publish(self.snapshot())
                except Exception:
                    pass


def check_updates(catalog_apps: list) -> list[dict[str, Any]]:
    """One pass over every running instance's benches; returns the item list."""
    try:
        running = {d.project_name for d in (list_instances().data or []) if d.status == "running"}
    except Exception:
        running = set()
    # installed version per (project, bench_index, app) from the catalog's installs
    versions: dict[tuple[str, int, str], str | None] = {}
    titles: dict[str, str] = {}
    bench_paths: dict[str, dict[int, str]] = {}
    for app in catalog_apps:
        name = getattr(app, "name", None) or (app.get("name") if isinstance(app, dict) else None)
        installs = (
            getattr(app, "installs", None)
            or (app.get("installs") if isinstance(app, dict) else [])
            or []
        )
        title = getattr(app, "title", None) or (app.get("title") if isinstance(app, dict) else None)
        if name and title:
            titles[name] = title
        if not isinstance(name, str):
            continue
        for inst in installs:
            d = inst if isinstance(inst, dict) else dataclasses.asdict(inst)
            vs = [s.get("version") for s in (d.get("sites") or []) if s.get("version")]
            project_name = str(d["project"])
            versions[(project_name, int(d["bench_index"]), name)] = vs[0] if vs else None
            bench_paths.setdefault(project_name, {})[int(d["bench_index"])] = str(d["bench_path"])
    memo: dict[str, Any] = {}
    memo_lock = threading.Lock()

    def one_project(project: str, benches: dict[int, str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if project not in running:
            return rows
        try:
            container = core_docker.get_frappe_container(project)
            _c, raw = container.exec_run(
                ["sh", "-c", _LOCAL_SCRIPT, "sh", *sorted(set(benches.values()))],
                user="frappe",
                demux=False,
            )
        except Exception:
            return rows
        if raw is None:
            return rows
        path_to_index = {p: i for i, p in benches.items()}
        local: list[tuple[str, str, str, str, str]] = []
        for line in raw.decode("utf-8", "replace").splitlines():
            cols = line.split("\t")
            if len(cols) >= 5 and cols[0] in path_to_index:
                local.append((cols[0], cols[1], cols[2], cols[3], cols[4]))
                # The source tree's __version__ is what runs; the cache's is install-time.
                if len(cols) >= 6 and cols[5]:
                    versions[(project, path_to_index[cols[0]], cols[1])] = cols[5]
        pairs = sorted({(url, br) for _b, _a, _h, br, url in local if url and br and br != "HEAD"})
        remote_heads: dict[tuple[str, str], str] = {}
        if pairs:
            stdin = "".join(f"{url}\t{br}\n" for url, br in pairs)
            try:
                _c, out = container.exec_run(
                    [
                        "sh",
                        "-c",
                        f"printf '%s' {_sh_quote(stdin)} | sh -c {_sh_quote(_REMOTE_SCRIPT)}",
                    ],
                    user="frappe",
                    demux=False,
                )
            except Exception:
                out = None
            for line in (out or b"").decode("utf-8", "replace").splitlines():
                cols = line.split("\t")
                if len(cols) >= 3 and cols[2]:
                    remote_heads[(cols[0], cols[1])] = cols[2]
        for bench_path, app, head, branch, url in local:
            bench_index = path_to_index[bench_path]
            remote_head = remote_heads.get((url, branch), "")
            hp = _host_path(url) if url else None
            differs = bool(head and remote_head and head != remote_head)
            with memo_lock:
                count = (
                    _count_behind(hp[0], hp[1], head, remote_head, memo)
                    if (differs and hp)
                    else (0 if head and remote_head else None)
                )
                release = _release_facts(app, versions.get((project, bench_index, app)), memo)
            behind = differs and count != 0
            rows.append(
                {
                    "project": project,
                    "bench_index": bench_index,
                    "bench_path": bench_path,
                    "app": app,
                    "title": titles.get(app, app),
                    "core": app in CORE_APPS,
                    "installed_version": versions.get((project, bench_index, app)),
                    "branch": branch or None,
                    "head": head or None,
                    "remote": f"{hp[0]}/{hp[1]}" if hp else (url or None),
                    "remote_head": remote_head or None,
                    "remote_checked": bool(remote_head),
                    "behind": behind,
                    "commits_behind": count,
                    "release": release,
                    "update_available": behind
                    or bool(release and release.get("release_available")),
                }
            )
        return rows

    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rows in pool.map(lambda kv: one_project(kv[0], kv[1]), sorted(bench_paths.items())):
            items.extend(rows)
    core_first = sorted(
        items, key=lambda it: (not it["core"], it["project"], it["bench_index"], it["app"])
    )
    return core_first
