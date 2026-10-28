"""Site favicons for the web UI: each site's own icon, fetched through its bench
(a request carrying the site as ``Host``, the way Frappe routes), cached
content-addressed next to the app logos and served same-origin.

Sources, in order:

1. RUNNING instance: ``GET /`` on the bench's container port with ``Host: <site>``
   (in-container ``curl``, the health probe's own transport), the page's
   ``<link rel="icon">`` (Website Settings' favicon when one is set, else Frappe's
   default), then ``/favicon.ico``.
2. STOPPED instance: a favicon file under ``sites/<site>/public/files`` on the
   bench's host bind mount (the file Website Settings uploads), else the icon
   remembered from the last time the instance ran.
3. Nothing: the page draws a plain placeholder. Never a guessed icon.

Entries are refreshed at most once an hour per site, in the background, and the
``site_icons`` SSE frame announces a finished pass.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
import urllib.parse
from typing import Any

from . import docker as core_docker
from . import resolvers
from .app_catalog import _HREF_RE, _ICON_LINK_RE, _host_bench_dir, catalog_dir, store_logo

REFRESH_AFTER_S = 3600
_IMAGE_EXT = (".ico", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp")


def _file() -> os.PathLike:
    return catalog_dir() / "site-icons.json"


def _curl(container, port: int, site: str, path: str) -> bytes | None:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        code, out = container.exec_run(["curl", "-s", "-m", "6", "-L", "-H", f"Host: {site}", url])
    except Exception:
        return None
    return out if code == 0 and out else None


def fetch_live(container, bench_path: str, site: str) -> dict[str, Any] | None:
    ports = resolvers.resolve_assigned_ports(container, [bench_path], fill_defaults=False)
    assigned = ports.get(bench_path)
    if assigned is None:
        return None
    port = assigned[0]
    candidates: list[str] = []
    html = _curl(container, port, site, "/")
    if html:
        text = html[:300_000].decode("utf-8", "replace")
        for tag in _ICON_LINK_RE.findall(text):
            m = _HREF_RE.search(tag)
            if m:
                href = urllib.parse.urljoin("/", m.group(1))
                if href.startswith("/"):
                    candidates.append(href.split("?", 1)[0])
    candidates.append("/favicon.ico")
    for path in candidates:
        stored = store_logo(_curl(container, port, site, path), source="site_favicon")
        if stored:
            return stored
    return None


def fetch_from_disk(project: str, bench_path: str, site: str) -> dict[str, Any] | None:
    bench_dir = _host_bench_dir(project, bench_path)
    if bench_dir is None:
        return None
    files_dir = os.path.join(bench_dir, "sites", site, "public", "files")
    for path in sorted(glob.glob(os.path.join(glob.escape(files_dir), "*favicon*"))):
        if path.lower().endswith(_IMAGE_EXT):
            try:
                with open(path, "rb") as f:
                    stored = store_logo(f.read(), source="site_favicon_file")
            except OSError:
                continue
            if stored:
                return stored
    return None


class SiteIconStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = self._load()
        self._refreshing = False

    def _load(self) -> dict[str, dict[str, Any]]:
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
                json.dump(self._entries, f)
            os.replace(tmp, _file())
        except OSError:
            pass

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._entries)

    def refresh(self, sites: list[dict[str, Any]], publish, *, force: bool = False) -> bool:
        """Refresh the icons of ``sites`` (``{project, bench_path, site, running}``) in
        the background; False when a pass is already running."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        threading.Thread(target=self._run, args=(sites, publish, force), daemon=True).start()
        return True

    def _run(self, sites: list[dict[str, Any]], publish, force: bool) -> None:
        now = time.time()
        changed = False
        try:
            for s in sites:
                key = f"{s['project']}/{s['site']}"
                with self._lock:
                    cur = self._entries.get(key) or {}
                fresh = cur.get("fetched_at", 0) > now - REFRESH_AFTER_S
                if cur.get("logo") and cur.get("source") == "live" and fresh and not force:
                    continue
                logo = None
                source = None
                if s.get("running"):
                    try:
                        container = core_docker.get_frappe_container(s["project"])
                    except Exception:
                        container = None
                    if container is not None:
                        logo = fetch_live(container, s["bench_path"], s["site"])
                        source = "live" if logo else None
                if logo is None and (not cur.get("logo") or force):
                    logo = fetch_from_disk(s["project"], s["bench_path"], s["site"])
                    source = "disk" if logo else None
                if logo is None:
                    if cur.get("logo"):
                        continue  # keep what the last run found
                    entry: dict[str, Any] = {"logo": None, "source": None, "fetched_at": now}
                else:
                    entry = {"logo": logo, "source": source, "fetched_at": now}
                with self._lock:
                    self._entries[key] = entry
                changed = True
            if changed:
                with self._lock:
                    self._save()
        finally:
            with self._lock:
                self._refreshing = False
            if publish is not None:
                try:
                    publish({"icons": self.all(), "changed": changed})
                except Exception:
                    pass
