"""The cwcli web UI daemon - the HTTP/SSE frontend over the core, behind ``cwcli ui``.

``cwcli ui`` (``commands/ui.py``) starts this daemon detached, reuses a live one,
and opens the browser; ``cwcli ui --foreground`` runs it in the terminal. The
``python -m caffeinated_whale_cli.commands.serve`` module entry stays as the
developer entry with a ``--host`` override. There is no auto-start, no boot
unit: a dashboard nobody is looking at should not be probing anyone's Docker
daemon, and the FAST probe loop only speaks when state changes or for an
instance a client has open.

Stdlib only (``http.server.ThreadingHTTPServer`` + ``queue`` + ``threading``) over
``docker``, which is already a runtime dependency - no web framework, no asyncio,
no new dependency. Browser-native SSE over plain HTTP is what keeps this
cross-platform down to "any browser", including a **Windows** browser reaching
a daemon bound inside **WSL**.

The page is the built frappe-ui single-page app shipped as package data under
``caffeinated_whale_cli/webui/dist/`` (source in the repo's ``webui/``). Every
non-``/api/`` path serves it: a real file by name, otherwise ``index.html`` so
the router owns deep links. An install without a build gets a one-screen notice
naming the build command.

Endpoints. Every success body is ``{"data": ...}`` and every failure is
``{"errors": [{"type", "title", "message", "hint", "indicator", ...}]}``, the
envelope frappe-ui's fetch layer reads natively (``type`` is the core's
``ErrorKind`` and ``title`` its error code).

* ``GET /``                                 - the web UI (open, static)
* ``GET /api/v1/meta``                      - ``version`` and the integer
  ``protocol`` a page checks before trusting the API shape it was built against
  (open, like ``/``: it carries no fleet data; deliberately NOT enveloped, since
  it is the one read a page makes before it knows the envelope)
* ``GET /api/v1/instances``                 - the whole fleet model
* ``GET /api/v1/instances/<p>``             - the LAZY tier: cache-backed
  ``core.inspect`` carrying its ``served_from`` and ``installed_apps_verified``
  freshness labels through unchanged, plus ``cached_at``
* ``GET /api/v1/instances/<p>/logs``        - resumable ``core.read_logs_since``
  tail (``?bench=&process=&lines=&after=<proc>:<offset>,...``); same-origin
  only - log lines are more sensitive than fleet health
* ``GET /api/v1/instances/<p>/url``         - ``core.url.probe_url`` host URL plus
  a fresh reachability probe (same-origin, serialized with the project's
  actions because it execs ``curl`` in the container)
* ``GET /api/v1/doctor``                    - system-wide ``core.doctor.run_all``
* ``GET /api/v1/config``                    - ``core.config.show_config`` (read)
* ``GET /api/v1/cache``                     - the cache inventory (remembered projects)
* ``GET /api/v1/search?q=<term>``           - ``core.where``, its per-row
  ``project_state`` verified/remembered token passed through unchanged
* ``GET /api/v1/jobs[/<id>]``               - durable detached job summaries
  and one job's resumable event log
* ``GET /api/v1/stream[?focus=<project>]``  - SSE: one ``snapshot`` event, then
  ``delta`` events tagged ``tier: instant|fast``; one stream per tab, every
  frame numbered with an ``id`` so ``Last-Event-ID`` reconnects are honest
  (a reconnect always gets a fresh snapshot, which IS the resume)
* ``POST /api/v1/session``                  - exchange the one-time login code
  (JSON ``{"code": ...}``) or the bearer token for the browser session cookie
* ``POST /api/v1/session/code``             - mint a fresh one-time login code
  (bearer token ONLY; how ``cwcli ui`` signs a second browser in)
* ``POST /api/v1/jobs``                     - create one detached long-running
  job; passwords travel only to the child environment and never enter its spec
* ``POST /api/v1/jobs/<id>/interrupt``      - request a cleanup-aware interrupt
* ``POST /api/v1/jobs/<id>/detach``         - leave the durable job running
* ``POST /api/v1/jobs/<id>/retry``          - start a new job from a failed one's
  verb, target, args and in-memory secrets (never its consent)
* ``POST /api/v1/jobs/<id>/continue``       - answer a ``needs_choice`` job with a
  new job from the posted target, args and consent plus its in-memory secrets
* ``POST /api/v1/actions``                  - the Phase 1 compatibility endpoint;
  the Phase 2 page sends lifecycle work to jobs, while older built pages keep
  their original guarded behavior until they reload
* ``POST /api/v1/settings``                 - one settings write (``{"setting":
  <name>, ...}``) over the SAME core functions ``cwcli config`` and ``cwcli axi
  setup`` call: cache clear/enable/disable, search paths add/remove/prune,
  auto-inspect and credential-bridge enable/disable, tips, agent hooks. The
  whole-cache clear keeps the core's own consent (``NEEDS_CHOICE``
  ``confirm_clear`` -> 409 until ``consent: true``), and nothing here is a
  project-scoped mutation, so it is serialized under one shared lock

The guarded destructive verbs (``rm``, ``rm-site``, ``rm-bench``, ``apps
checkout --reset``) are JOBS, not actions: ``jobs.py`` runs them through the
same core paths as the CLI, consent is a spec field the page sets only after a
typed confirmation of the exact instance, bench, site or app name, and no
request can express ``--no-backup``. The one ``force`` a request can carry is
``apps-update``'s, which on the frappe path accepts discarding uncommitted and
unpushed app work; on that path the page sends it only after a typed
confirmation of the bench name. A forced update of other apps needs no typed
confirmation, and there ``force`` still discards local changes on a git
conflict.

``?focus=<project>`` is how the browser says which instance it currently has
open, and it is the whole mechanism behind the web-probe cadence: the SSE
connection itself carries the answer, so closing the tab retracts focus when a
later delta or keepalive discovers the closed response stream. See
``core.fleet.Fleet.set_focus``.

**Binding.** The default is ``127.0.0.1``, and ``cwcli ui`` binds nothing else.
Every endpoint names local projects, ports and sites, and the action endpoint
drives lifecycle operations, so the listener is this machine's by default and
reaching it from elsewhere is a deliberate act through the module entry.

A Windows browser CAN reach a WSL-only ``127.0.0.1`` listener: WSL's default NAT
mode forwards ``localhost`` into the distro, which Microsoft documents
(https://learn.microsoft.com/en-us/windows/wsl/networking) and which this repo
measured on its own primary machine (``docs/e2e/serve-daemon.md`` section 1,
whose table records ``127.0.0.1`` as reachable from Windows). The reason
``0.0.0.0`` remains available is the narrower one that table also records: the
WSL-IP route survives ``localhostForwarding`` being turned off, and it is what a
genuinely remote browser needs.

**Host allowlist (the DNS-rebinding guard).** Every request, the static page
included, must carry a ``Host`` that names this machine. The only NAME admitted
is ``localhost``; the admitted IP LITERALS are any loopback address (all of
``127.0.0.0/8`` and ``[::1]``), plus any IP on a non-loopback bind (which already
needs a token). Literals are admitted that widely on purpose: rebinding needs an
attacker-controlled hostname. Anything else is ``403 host.forbidden`` before any
other guard runs. A rebinding page reaches the loopback listener under the ATTACKER's
hostname, so its ``Origin`` and ``Host`` match and the browser even labels it
``Sec-Fetch-Site: same-origin``: a same-origin check alone passed it straight to
action dispatch. ``Host`` is the one header that still tells the two apart, and
an IP literal cannot be rebound.

**Authentication is ALWAYS on, loopback included.** Every non-``OPTIONS``
``/api/*`` request needs a credential. The bearer token is ``CWCLI_SERVE_TOKEN``
when set, otherwise a random token made fresh for each launch. It is read from
the ENVIRONMENT only and never from a flag, so it stays out of ``ps`` output
(the ``core/init.py`` secret-transport rule), and it is never printed. Comparison
is ``hmac.compare_digest``. A browser signs in through the one-time login code
in ``http://<bound address>:<port>/#login=<code>``: the fragment is never sent
to a server or in a ``Referer``, the page posts it once to ``/api/v1/session``
and strips it from history, and a redeemed code is dead, so a link copied out of
history or scrollback cannot be replayed. ``cwcli ui`` mints a fresh code for
every browser it opens through ``POST /api/v1/session/code`` with the bearer
token it holds in a 0600 file. ``GET /`` and ``GET /api/v1/meta`` stay open
because they carry no fleet data, and a browser that could not load the page
could never sign in. ``OPTIONS`` returns no data and dispatches no action.

**A non-loopback bind REFUSES TO START without a token.** An explicit
``--host 0.0.0.0`` used to be silently unauthenticated: the same-origin guard
below returns True when a request carries neither ``Origin`` nor
``Sec-Fetch-Site``, which is exactly what a non-browser client sends, so any host
on the LAN could stop or restart an instance with one ``curl``. That guard is a
CSRF guard and is correct as one; it was never authentication, and it is kept
here as defense in depth rather than relied on.

Browsers authenticate once via ``POST /api/v1/session``, which exchanges the
login code (or the bearer token) for a ``HttpOnly``/``SameSite=Strict`` session
cookie. That exists because ``EventSource`` cannot send request headers, so a
header-only scheme would leave the SSE stream unauthenticatable from a page. The
cookie carries one random daemon-wide session id, never the token itself. It is
replayable if captured until that daemon restarts. ``HttpOnly`` and ``SameSite``
constrain browser behavior; they do not encrypt plain HTTP. A non-loopback
listener belongs only on a trusted network.

**No CORS, anywhere.** No response carries an ``Access-Control-*`` header, so no
other origin's page can read any of it. Non-browser clients send the bearer
token; browsers use the same-origin page and its session cookie. Cross-origin
browser actions and the sensitive logs/url/doctor/config/cache/search reads are
also refused by the same-origin check.

**Content-Security-Policy.** The SPA runs under ``script-src 'self'`` with no
inline script (Vite's ``assetsInlineLimit: 0`` keeps fonts and icons out of
``data:`` script/style URIs). The one exception is the ``<style>`` block reka-ui's
ScrollArea injects at runtime, allowed by hash: the webui build computes it from
the installed reka-ui and writes ``dist/csp.json``, which this daemon merges into
``style-src``, so the hash follows the dependency version rather than a constant
someone has to remember to update.
"""

from __future__ import annotations

import dataclasses
import hmac
import ipaddress
import json
import mimetypes
import os
import platform
import queue
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import typer

from .. import __version__
from .. import jobs as core_jobs
from ..core import accounts as core_accounts
from ..core import advisories as core_advisories
from ..core import app_catalog as core_app_catalog
from ..core import apps as core_apps
from ..core import auto_inspect as core_auto_inspect
from ..core import backup_store as core_backup_store
from ..core import bench_ops as core_bench_ops
from ..core import config as core_config
from ..core import cred_bridge as core_cred_bridge
from ..core import daemon_recovery
from ..core import dev_mode as core_dev_mode
from ..core import directory as core_directory
from ..core import doctor as core_doctor
from ..core import editors as core_editors
from ..core import fleet as core_fleet
from ..core import git_refs as core_git_refs
from ..core import inspect as core_inspect
from ..core import integrations as core_integrations
from ..core import label as core_label
from ..core import list as core_list
from ..core import logs as core_logs
from ..core import metrics as core_metrics
from ..core import open as core_open
from ..core import resolvers as core_resolvers
from ..core import restart as core_restart
from ..core import scale as core_scale
from ..core import schedules as core_schedules
from ..core import site_icons as core_site_icons
from ..core import site_logs as core_site_logs
from ..core import start as core_start
from ..core import stop as core_stop
from ..core import transfer as core_transfer
from ..core import unlock as core_unlock
from ..core import updates as core_updates
from ..core import url as core_url
from ..core import version as core_version
from ..core import where as core_where
from ..core.envelope import Message, Result, Status
from ..core.errors import CwcliError, ErrorKind
from ..core.rm import is_valid_project_name
from ..utils import agent_hooks, cache, daemon_identity
from ..utils.console import console, stderr_console
from . import schedule as schedule_cmd

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DEFAULT_INTERVAL = 2.5
KEEPALIVE_S = 15.0
TOKEN_ENV = "CWCLI_SERVE_TOKEN"
SESSION_COOKIE = "cwcli_serve_session"
# Bumped on any change a page built against an older daemon could misread, so a
# page compares it with the number it was built for instead of parsing versions.
# 2: the /api/v1 routes and the {data}/{errors} envelope (Phase 1).
# 3: durable jobs, job SSE frames, and detached lifecycle actions (Phase 2).
# 4: the settings endpoint and the guarded destructive job verbs (Phase 3).
# 5: the apps catalog (/api/v1/apps, its logo route, the `catalog` SSE event).
# 7: roles, directory sign-in, and /api/v1/integrations (the S3 target moved there).
PROTOCOL_VERSION = 7

# The running process's install fingerprint (core.version.install_fingerprint), set
# by run_daemon, so a page can tell a daemon running different code.
_running_build: str | None = None

# Map the core's closed error kinds onto equivalent HTTP statuses.
_HTTP_FOR_KIND = {
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.NOT_RUNNING: 409,
    ErrorKind.CONFLICT: 409,
    ErrorKind.PRECONDITION: 412,
    ErrorKind.USAGE: 400,
    ErrorKind.DOCKER: 503,
}
_MAX_ACTION_BODY = 64 * 1024
_MAX_UPLOAD_BODY = 50 * 1024 * 1024 * 1024  # a set's tar, streamed to disk
_ACTION_LOCK_STRIPES = 64

# The synchronous safe action set. See the module docstring for what may NOT be
# added here without its own decision.
ACTIONS = frozenset(
    {
        "start_instance",
        "stop_instance",
        "restart_instance",
        "stop_bench",
        "restart_process",
        "refresh_status",
        "set_label",
        "unlock_site",
        "clear_maintenance",
        "scale_instance",
        "checkout_app",
        "open_editor",
    }
)

# Every settings write the page can ask for. Each maps onto ONE core function the
# CLI renderer already calls; none is project-scoped lifecycle work.
SETTINGS = frozenset(
    {
        "cache.clear",
        "cache.enable",
        "cache.disable",
        "paths.add",
        "paths.remove",
        "paths.prune",
        "auto_inspect.enable",
        "auto_inspect.disable",
        "auto_inspect.start",
        "auto_inspect.restart",
        "cred_bridge.enable",
        "cred_bridge.disable",
        "cred_bridge.start",
        "cred_bridge.restart",
        "tips.enable",
        "tips.disable",
        "agent_hooks.install",
        "catalog.source_add",
        "catalog.source_remove",
    }
)
# Job verbs only an Admin may start: removals, and replacing cwcli itself.
ADMIN_VERBS = frozenset({"rm", "rm-site", "rm-bench", "delete-backup", "self-update"})
# The least role a GET needs; anything not named here is a read every role may make.
_GET_ROLES = {"/api/v1/integrations": "admin", "/api/v1/backups/download": "operator"}

# The lock stripe every settings write shares: the config is one TOML file read,
# modified and written whole, so two concurrent writes would lose one.
_SETTINGS_LOCK_KEY = "@settings"

# ------------------------------------------------------------------ the web UI

# Package data written by `cd webui && npm run build`; git-ignored in the tree.
WEBUI_DIST = files("caffeinated_whale_cli.webui").joinpath("dist")
# CWCLI_WEBUI_DIST points a daemon at a different BUILT page (a prototype dist
# built from another branch) while the Python code stays the installed one.
WEBUI_DIST_ENV = "CWCLI_WEBUI_DIST"


def _webui_dist():
    override = (os.environ.get(WEBUI_DIST_ENV) or "").strip()
    return Path(override) if override else WEBUI_DIST


_SPA_CSP_BASE: dict[str, list[str]] = {
    "default-src": ["'none'"],
    "script-src": ["'self'"],
    "style-src": ["'self'"],
    # lucide icons are CSS masks on data: SVG URIs (frappe-ui's tailwind plugin).
    "img-src": ["'self'", "data:"],
    "font-src": ["'self'"],
    "connect-src": ["'self'"],
    "base-uri": ["'none'"],
    "form-action": ["'none'"],
    "frame-ancestors": ["'none'"],
}
_MIME = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".html": "text/html; charset=utf-8",
}
# The notice an unbuilt install serves at `/`: no script, no style, so its CSP
# is the strictest possible and nothing on it needs a hash.
UNBUILT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>cwcli web UI not built</title></head>
<body><h1>The cwcli web UI is not built in this install.</h1>
<p>This is a source checkout without a frontend build. From the repository root run:</p>
<pre>cd webui &amp;&amp; npm ci &amp;&amp; npm run build</pre>
<p>then reload. Released packages ship the UI prebuilt.</p>
</body></html>
"""


def webui_available() -> bool:
    """Whether a built UI is installed (the ``index.html`` package-data file exists)."""
    try:
        return bool(_webui_dist().joinpath("index.html").is_file())
    except (OSError, ValueError):
        return False


def spa_csp() -> str:
    """The SPA's policy, with the build's extra hashes (``dist/csp.json``) merged in."""
    policy = {k: list(v) for k, v in _SPA_CSP_BASE.items()}
    try:
        extra = json.loads(_webui_dist().joinpath("csp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        extra = {}
    for directive, sources in extra.items():
        if directive in policy and isinstance(sources, list):
            policy[directive].extend(str(s) for s in sources if s)
    return "; ".join(f"{k} {' '.join(v)}" for k, v in policy.items())


def _spa_file(path: str):
    """The built file a request path names, or None (``..`` and the like refuse).

    Walks the package-data tree one validated segment at a time rather than
    resolving a filesystem path: a segment that is empty, ``.``, ``..`` or carries
    a separator can never leave ``dist/``.
    """
    segments = [unquote(s) for s in path.strip("/").split("/") if s]
    if not segments:
        return None
    node = _webui_dist()
    for segment in segments:
        if segment in {".", ".."} or "/" in segment or "\\" in segment or "\0" in segment:
            return None
        node = node.joinpath(segment)
    try:
        return node if node.is_file() else None
    except (OSError, ValueError):
        return None


def auth_token() -> str | None:
    """The operator's ``CWCLI_SERVE_TOKEN``, or None when unset.

    None never means "no authentication": ``serve`` then makes a random token
    for this launch, and browsers sign in with the one-time login code.

    Read from the ENVIRONMENT only. There is deliberately no ``--token`` flag:
    an argv secret is readable by every other process on the machine, which is
    the same rule ``core/init.py`` follows for the admin and db-root passwords.
    """
    return (os.environ.get(TOKEN_ENV) or "").strip() or None


def is_loopback(host: str) -> bool:
    """True only when this bind address is provably this machine's loopback.

    Fails CLOSED. An empty host binds every interface, and a name that is not an
    IP literal could resolve anywhere, so both answer False: "this bind needs a
    token" is the safe answer to a question that cannot be settled here.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def host_allowed(host_header: str | None, *, remote_bind: bool) -> bool:
    """Whether a request's ``Host`` names this machine rather than someone's domain.

    ``localhost`` and loopback IP literals always pass. On a non-loopback bind
    (which cannot start without a token) any IP literal passes too, because that
    is how a browser on another machine addresses it, and an IP literal cannot be
    rebound. Every other name, a missing header and anything malformed fail
    closed. The port is not compared: rebinding needs an attacker-controlled
    NAME, and the listener already owns the port it was reached on.
    """
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        name, closed, rest = host[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")):
            return False
    else:
        name = host.partition(":")[0]
    # Names: only localhost. Literals: any loopback, plus any IP on a remote bind.
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or remote_bind


def _secret_matches(presented: str, expected: str) -> bool:
    """Timing-safe secret comparison, over bytes so a non-ASCII token cannot raise."""
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceRestartOutcome:
    """The narrow action result for stop-then-start instance restart."""

    project: str
    stopped: core_stop.StopOutcome
    started: core_start.StartOutcome | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceOutcome:
    """``clear_maintenance``: whether the site ended OUT of maintenance mode."""

    project: str
    bench_path: str
    site: str
    maintenance_off: bool


class _Hub:
    """Fan-out to every connected SSE client, one unbounded queue each.

    Unbounded is deliberate: a bounded queue would have to choose between
    blocking the probe thread on a stalled browser and silently dropping a delta,
    and a dropped delta is a UI that is quietly wrong. The FAST tier only speaks
    when state actually changes, or once per probe for an instance a client has
    open, so a queue that grows without bound means the client is stalled rather
    than ordinary steady-state traffic.

    Every published frame carries a monotonic ``id`` so a reconnecting
    ``EventSource`` sends ``Last-Event-ID``; the daemon answers any reconnect with
    a fresh snapshot (the model is state, not a journal, so the snapshot IS the
    resume), and the id lets a client tell a replayed stream from a new one.
    """

    def __init__(self, on_focus) -> None:
        self._lock = threading.Lock()
        self._focus_lock = threading.Lock()
        self._clients: dict[int, tuple[queue.Queue, str | None]] = {}
        self._next = 0
        self._event_id = 0
        self._on_focus = on_focus

    def subscribe(self, focus: str | None) -> tuple[int, queue.Queue]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._next += 1
            client_id = self._next
            self._clients[client_id] = (q, focus)
        self._sync_focus()
        return client_id, q

    def unsubscribe(self, client_id: int) -> None:
        with self._lock:
            removed = self._clients.pop(client_id, None) is not None
        if removed:
            self._sync_focus()

    def _sync_focus(self) -> None:
        with self._focus_lock:
            with self._lock:
                focused = {f for _q, f in self._clients.values() if f}
            self._on_focus(focused)

    def next_id(self) -> int:
        with self._lock:
            self._event_id += 1
            return self._event_id

    def publish(self, tier, project, state, cause) -> None:
        payload = {
            "tier": tier,
            "project": project,
            "instance": core_fleet.as_json(state) if state is not None else None,
            "removed": state is None,
        }
        if cause:
            payload["cause"] = cause
        frame = _sse_frame("delta", payload, self.next_id())
        with self._lock:
            queues = [q for q, _f in self._clients.values()]
        for q in queues:
            q.put(frame)

    def publish_job(self, record: dict) -> None:
        self.publish_event("job", record)

    def publish_event(self, event: str, data: dict) -> None:
        frame = _sse_frame(event, data, self.next_id())
        with self._lock:
            queues = [q for q, _f in self._clients.values()]
        for q in queues:
            q.put(frame)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


def _sse_frame(event: str, data: dict, event_id: int | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _directory_on() -> bool:
    """Whether directory sign-in is on. An unreadable integrations store reads as off,
    so it can never stand between the local admin and the sign-in."""
    try:
        return core_integrations.active("ldap") is not None
    except CwcliError:
        return False


def _offsite_summary() -> dict | None:
    """Where off-site copies go (the S3 backup target integration), without its keys."""
    try:
        found = core_integrations.active("s3")
    except CwcliError:
        return None
    if found is None:
        return None
    return {k: found[0].get(k) for k in ("endpoint", "bucket", "prefix")}


def _account_info(acct: core_accounts.Account) -> dict:
    return {
        "username": acct.username,
        "must_change_password": acct.must_change,
        "role": acct.role,
        "source": acct.source,
        "name": acct.name,
    }


def _error_body(kind: str, code: str, message: str, hint: str | None = None, **extra) -> dict:
    """The frappe-ui error envelope: ``errors[0]`` is what its fetch layer reads."""
    error: dict = {"type": kind, "title": code, "message": message, "hint": hint}
    error["indicator"] = "red"
    error.update(extra)
    return {"errors": [error]}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cwcli-serve"

    fleet: core_fleet.Fleet
    hub: _Hub
    catalog: core_app_catalog.CatalogStore
    metrics: core_metrics.MetricsStore
    site_icons: core_site_icons.SiteIconStore
    updates: core_updates.UpdatesStore
    advisories: core_advisories.AdvisoriesStore
    action_locks: tuple[threading.Lock, ...]
    token: str
    lockout: core_accounts.Lockout
    remote_bind: bool

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook name
        """Silence per-request logging; a dashboard polls, and the noise buries the banner."""

    # ------------------------------------------------------------- responses

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, kind: str, code: str, message: str, **extra) -> None:
        self._send_json(_error_body(kind, code, message, **extra), status=status)

    def _send_bytes(self, raw: bytes, content_type: str, *, csp: str | None, cache: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        if csp is not None:
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _send_spa(self, path: str) -> None:
        """A built file by name, else ``index.html`` (the router owns deep links)."""
        if not webui_available():
            if path == "/":
                self._send_bytes(
                    UNBUILT_PAGE.encode(),
                    _MIME[".html"],
                    csp="default-src 'none'; frame-ancestors 'none'",
                    cache="no-cache",
                )
            else:
                self._send_error(404, ErrorKind.NOT_FOUND.value, "webui.not_built", "not found")
            return
        node = _spa_file(path)
        if node is None:
            raw = _webui_dist().joinpath("index.html").read_bytes()
            self._send_bytes(raw, _MIME[".html"], csp=spa_csp(), cache="no-cache")
            return
        suffix = os.path.splitext(node.name)[1].lower()
        content_type = (
            _MIME.get(suffix) or mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        )
        immutable = path.startswith("/assets/")  # Vite's content-hashed bundle dir
        self._send_bytes(
            node.read_bytes(),
            content_type,
            csp=spa_csp() if suffix == ".html" else None,
            cache="public, max-age=31536000, immutable" if immutable else "no-cache",
        )

    # ------------------------------------------------------------------- auth

    def _refuse_foreign_host(self) -> bool:
        """Send the 403 and return True when ``Host`` does not name this machine.

        The DNS-rebinding guard, run before every other guard on every method.
        The connection is closed rather than drained: nothing more from a client
        that addressed some other name is worth parsing.
        """
        if host_allowed(self.headers.get("Host"), remote_bind=self.remote_bind):
            return False
        self.close_connection = True
        self._send_error(
            403,
            ErrorKind.PRECONDITION.value,
            "host.forbidden",
            "This cwcli serve daemon only answers requests addressed "
            "to localhost or its IP address.",
            hint=(
                "Open it at http://<this machine's IP address>:<port>/."
                if self.remote_bind
                else "Open it at http://127.0.0.1:<port>/ or http://localhost:<port>/."
            ),
        )
        return True

    def _bearer_authenticated(self) -> bool:
        scheme, _, presented = (self.headers.get("Authorization") or "").partition(" ")
        return scheme.lower() == "bearer" and _secret_matches(presented.strip(), self.token)

    def _authenticated(self) -> bool:
        """Whether this request carries the credential. There is no unauthenticated mode.

        Two accepted forms. ``Authorization: Bearer`` is what a programmatic
        client (curl, a script, ``cwcli ui`` itself) sends. The session cookie is
        what a BROWSER falls back to, and it exists for one concrete reason:
        ``EventSource`` cannot set request headers, so a header-only scheme would
        leave the stream unreachable from the page.
        """
        return self._bearer_authenticated() or self._session_account() is not None

    def _session_account(self) -> core_accounts.Account | None:
        """The account behind this request's session cookie, or None."""
        return core_accounts.session_account(self._session_cookie())

    def _account_name(self) -> str:
        """Who is acting: the signed-in account, else ``api`` for a bearer client."""
        acct = self._session_account()
        return acct.username if acct is not None else "api"

    _CHANGE_PENDING_PATHS = frozenset(
        {
            "/api/v1/session",
            "/api/v1/session/password",
            "/api/v1/session/signout",
            "/api/v1/session/sessions",
        }
    )

    def _refuse_change_pending(self, path: str) -> bool:
        """The forced first change: an account still on the default password reaches
        nothing but its own session until it has picked a real one."""
        acct = self._session_account()
        if acct is None or not acct.must_change or path in self._CHANGE_PENDING_PATHS:
            return False
        self._send_error(
            403,
            ErrorKind.PRECONDITION.value,
            "auth.password_change_required",
            "Choose a new password before using the web UI.",
        )
        return True

    @property
    def _cookie_name(self) -> str:
        """Named per port: browsers do not scope cookies by port, so two daemons on
        one machine sharing a name would sign each other's tabs out."""
        return f"{SESSION_COOKIE}_{self.connection.getsockname()[1]}"

    def _session_cookie(self) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            return SimpleCookie(raw)[self._cookie_name].value
        except (CookieError, KeyError):
            return ""

    def _refuse_unauthenticated(self) -> bool:
        """Send the 401 and return True when the request lacks the credential."""
        if self._authenticated():
            return False
        self._send_unauthenticated()
        return True

    def _send_unauthenticated(self) -> None:
        """The 401. It names the environment variable and NEVER a value, nor what
        was presented: an error that echoes a credential puts it into the browser
        console, the terminal and any bug report copied out of them.
        """
        body = json.dumps(
            _error_body(
                ErrorKind.PRECONDITION.value,
                "auth.required",
                "This cwcli serve daemon requires you to sign in.",
                hint="Sign in on the page (the first sign-in is admin / admin), or send "
                f"'Authorization: Bearer <token>' matching {TOKEN_ENV}.",
            )
        ).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", 'Bearer realm="cwcli serve"')
        self.end_headers()
        self.wfile.write(body)

    def _cookie_attrs(self) -> str:
        return "SameSite=Strict"

    def _sign_in(self, raw: bytes) -> None:
        """``POST /api/v1/session``: username + password for the session cookie.

        Failures are counted per username and per address (``core.accounts.Lockout``);
        a locked address answers 429 with ``retry_after`` and never runs the hash, and
        a locked username answers 429 to anything but its correct password. On a
        remote bind the default password signs in only alongside the bearer token.
        The cookie carries a random session id whose hash the store keeps;
        ``remember`` makes it a 30-day persistent cookie, otherwise it dies with the
        browser.
        """
        try:
            body = _parse_action_body(raw)
        except CwcliError as e:
            self._send_core_error(e)
            return
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        remember = bool(body.get("remember"))
        address, user = f"addr:{self.client_address[0]}", f"user:{username.lower()}"
        user_wait = self.lockout.retry_after(user)
        wait = self.lockout.attempt(address, user)
        acct: core_accounts.Account | None = None
        refusal: CwcliError | None = None
        if not wait and username and password:
            # A local account's name always signs in locally - the break-glass way in
            # while directory sign-in is on; every other name asks the directory.
            if core_accounts.is_local(username) or not _directory_on():
                acct = core_accounts.verify(username, password)
            else:
                try:
                    found = core_directory.authenticate(username, password)
                    acct = core_accounts.Account(
                        found.username, False, role=found.role, source="ldap", name=found.name
                    )
                except CwcliError as e:
                    refusal = e
        if not wait and acct is None and (refusal is None or refusal.code == "auth.invalid"):
            wait = user_wait
        if wait:
            self._send_error(
                429,
                ErrorKind.PRECONDITION.value,
                "auth.locked",
                f"Too many failed sign-ins; try again in {wait} seconds.",
                retry_after=wait,
            )
            return
        if refusal is not None and refusal.code != "auth.invalid":
            self._send_error(
                503 if refusal.code == "auth.directory_unreachable" else 403,
                ErrorKind.PRECONDITION.value,
                refusal.code,
                refusal.message,
                hint=refusal.hint,
            )
            return
        if acct is None:
            self._send_error(
                401,
                ErrorKind.PRECONDITION.value,
                "auth.invalid",
                "That username and password do not match.",
            )
            return
        self.lockout.succeeded(address, user)
        if acct.must_change and self.remote_bind and not self._bearer_authenticated():
            self._send_error(
                403,
                ErrorKind.PRECONDITION.value,
                "auth.default_password_remote",
                "The account still has the default password, so it cannot sign in "
                "from the network.",
                hint="Start the web UI on loopback with 'cwcli ui', sign in there and change "
                f"it first, or send 'Authorization: Bearer <token>' matching {TOKEN_ENV}.",
            )
            return
        session_id = core_accounts.open_session(
            acct.username,
            remember=remember,
            agent=(self.headers.get("User-Agent") or ""),
            source=acct.source,
            role=acct.role,
            name=acct.name,
        )
        body_out = json.dumps({"data": {"signed_in": True, **_account_info(acct)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        max_age = f"; Max-Age={core_accounts.REMEMBER_S}" if remember else ""
        self.send_header(
            "Set-Cookie",
            f"{self._cookie_name}={session_id}; Path=/; HttpOnly; {self._cookie_attrs()}{max_age}",
        )
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _session_info(self) -> dict:
        acct = self._session_account()
        if acct is None:
            return {
                "username": "api",
                "must_change_password": False,
                "account": False,
                "role": "admin",
                "source": "api",
                "name": "",
            }
        return {**_account_info(acct), "account": True}

    def _role(self) -> str:
        """The caller's role: the bearer token is the machine owner's, so Admin."""
        acct = self._session_account()
        return acct.role if acct is not None else "admin"

    def _refuse_role(self, needed: str) -> bool:
        """Send the 403 and return True when the caller's role is below ``needed``."""
        role = self._role()
        if core_accounts.ROLE_RANK[role] >= core_accounts.ROLE_RANK[needed]:
            return False
        self._send_error(
            403,
            ErrorKind.PRECONDITION.value,
            "auth.role_forbidden",
            f"Your role ({role.title()}) cannot do this; it needs {needed.title()}.",
            role=role,
            needs=needed,
        )
        return True

    def _post_role(self, path: str, payload: dict) -> str:
        """The least role a POST needs. Viewers only read; Operators run the fleet;
        Admins also change settings and integrations, remove instances, sites, benches
        and backups, and update cwcli itself."""
        if path in ("/api/v1/settings", "/api/v1/integrations"):
            return "admin"
        if path == "/api/v1/jobs":
            return "admin" if payload.get("verb") in ADMIN_VERBS else "operator"
        if path.startswith("/api/v1/jobs/") and path.rsplit("/", 1)[-1] in ("retry", "continue"):
            job_id = unquote(path[len("/api/v1/jobs/") :]).rsplit("/", 1)[0]
            try:
                verb = core_jobs.read_job(job_id).get("verb")
            except CwcliError:
                verb = None
            return "admin" if verb in ADMIN_VERBS else "operator"
        # Schedules need no check of their own: no Admin-only verb is schedulable.
        return "operator"

    def _change_password(self, raw: bytes) -> None:
        acct = self._session_account()
        if acct is not None and acct.source != "local":
            self._send_error(
                403,
                ErrorKind.PRECONDITION.value,
                "auth.directory_password",
                "Your password belongs to the directory; change it there.",
            )
            return
        if acct is None:
            self._send_error(
                403,
                ErrorKind.PRECONDITION.value,
                "auth.account_required",
                "Only a signed-in account can change its password.",
            )
            return
        try:
            body = _parse_action_body(raw)
            new = str(body.get("new") or "")
            current = body.get("current")
            core_accounts.change_password(
                acct.username,
                new,
                current=None if acct.must_change else str(current or ""),
                keep=self._session_cookie(),
            )
        except CwcliError as e:
            self._send_core_error(e)
            return
        self._send_json({"data": {"changed": True}})

    def _sign_out(self, raw: bytes) -> None:
        acct = self._session_account()
        try:
            others = bool(_parse_action_body(raw).get("others"))
        except CwcliError:
            others = False
        sid = self._session_cookie()
        if acct is not None and others:
            closed = core_accounts.close_other_sessions(acct.username, keep=sid)
            self._send_json({"data": {"signed_out": False, "closed": closed}})
            return
        if sid:
            core_accounts.close_session(sid)
        body_out = json.dumps({"data": {"signed_out": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Set-Cookie",
            f"{self._cookie_name}=; Path=/; HttpOnly; Max-Age=0; {self._cookie_attrs()}",
        )
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def do_OPTIONS(self):  # noqa: N802 - stdlib hook name
        """No data, no action, and no ``Access-Control-*`` header, so no preflight passes."""
        if self._refuse_foreign_host():
            return
        self.send_response(204)
        post = _is_post_path(urlparse(self.path).path)
        self.send_header("Allow", "POST, OPTIONS" if post else "GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------------- routes

    def do_GET(self):  # noqa: N802 - stdlib hook name
        if self._refuse_foreign_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not path.startswith("/api/"):
            # The page itself is static and holds no fleet data, and gating it
            # would leave a browser with no way to reach the sign-in it needs.
            self._send_spa(path)
            return
        if path == "/api/v1/meta":
            # directory_sign_in only tells the sign-in page which hint to show.
            meta: dict = {
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "directory_sign_in": _directory_on(),
            }
            if _running_build is not None:
                meta["build"] = _running_build
            self._send_json(meta)
            return
        if self._refuse_unauthenticated():
            return
        if path == "/api/v1/session":
            self._send_json({"data": self._session_info()})
            return
        if self._refuse_change_pending(path):
            return
        if self._refuse_role(_GET_ROLES.get(path, "viewer")):
            return
        if path == "/api/v1/session/sessions":
            acct = self._session_account()
            sid = self._session_cookie()
            rows = (
                [asdict(x) for x in core_accounts.list_sessions(acct.username, current=sid)]
                if acct is not None
                else []
            )
            self._send_json({"data": rows})
            return
        if path == "/api/v1/instances":
            self._send_json({"data": self.fleet.snapshot()})
        elif path == "/api/v1/stream":
            self._stream_events((query.get("focus") or [None])[0])
        elif path.startswith("/api/v1/instances/"):
            rest = path[len("/api/v1/instances/") :]
            project, _sep, verb = rest.partition("/")
            project = unquote(project)
            if not verb:
                self._send_detail(project)
            elif verb == "logs":
                self._send_logs(project, query)
            elif verb == "url":
                self._send_url(project, query)
            elif verb == "site-logs":
                self._send_site_logs(project, query)
            elif verb == "dev-mode":
                self._send_dev_mode(project, query)
            elif verb == "error-log":
                self._send_error_log(project, query)
            else:
                self._send_error(404, ErrorKind.NOT_FOUND.value, "route.not_found", "not found")
        elif path == "/api/v1/apps":
            self._send_catalog(query)
        elif path == "/api/v1/apps/refs":
            url = _optional_str((query.get("url") or [None])[0]) or ""
            refs = core_git_refs.list_remote_refs(url) if url else None
            self._send_json(
                {
                    "data": (
                        dataclasses.asdict(refs)
                        if refs is not None
                        else {
                            "url": "",
                            "branches": [],
                            "tags": [],
                            "ok": False,
                            "error": "url required",
                        }
                    )
                }
            )
        elif path == "/api/v1/metrics":
            self._send_metrics(query)
        elif path == "/api/v1/sites/icons":
            self._send_json({"data": {"icons": self.site_icons.all()}})
        elif path == "/api/v1/updates":
            self._send_updates()
        elif path == "/api/v1/schedules":
            self._send_json(
                {
                    "data": {
                        **core_schedules.list_schedules(),
                        "templates": core_schedules.templates(),
                    }
                }
            )
        elif path == "/api/v1/backups":
            self._send_backups(query)
        elif path == "/api/v1/backups/download":
            key = _optional_str((query.get("key") or [None])[0])
            if not key:
                self._send_error(400, ErrorKind.USAGE.value, "backups.key", "key is required")
                return
            outbox = core_backup_store.store_root() / ".outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(dir=outbox))
            try:
                try:
                    tar_path = core_transfer.export_tar(key, work)
                except CwcliError as e:
                    self._send_core_error(e)
                    return
                size = tar_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/x-tar")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{tar_path.name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with open(tar_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        elif path == "/api/v1/backups/offsite":
            project = _optional_str((query.get("project") or [None])[0])
            if not project:
                self._send_error(
                    400, ErrorKind.USAGE.value, "backups.project", "project is required"
                )
                return
            try:
                rows = core_backup_store.offsite_list(project)
            except CwcliError as e:
                self._send_core_error(e)
                return
            self._send_json({"data": {"project": project, "sets": rows}})
        elif path == "/api/v1/backups/retention":
            project = _optional_str((query.get("project") or [None])[0])
            if not project:
                self._send_error(
                    400, ErrorKind.USAGE.value, "backups.project", "project is required"
                )
                return
            plan = core_backup_store.retention_plan(project)
            self._send_json(
                {
                    "data": {
                        "project": project,
                        "decisions": [asdict(d) for d in plan],
                        "remove": sum(1 for d in plan if not d.keep),
                        "keep": sum(1 for d in plan if d.keep),
                        "settings": core_backup_store.settings(project),
                        "offsite_target": _offsite_summary(),
                    }
                }
            )
        elif path == "/api/v1/advisories":
            self._send_advisories()
        elif path == "/api/v1/advisories.rss":
            snap = self.advisories.snapshot()
            self._send_bytes(
                core_advisories.rss(
                    snap, f"http://{self.headers.get('Host', '127.0.0.1')}/advisories"
                ).encode("utf-8"),
                "application/rss+xml; charset=utf-8",
                csp=None,
                cache="no-cache",
            )
        elif path.startswith("/api/v1/apps/logo/"):
            self._send_logo(unquote(path[len("/api/v1/apps/logo/") :]))
        elif path == "/api/v1/integrations":
            self._send_json({"data": {"integrations": core_integrations.view()}})
        elif path == "/api/v1/doctor":
            self._send_doctor()
        elif path == "/api/v1/config":
            self._send_config()
        elif path == "/api/v1/cache":
            self._send_cache()
        elif path == "/api/v1/search":
            self._send_where(query)
        elif path == "/api/v1/jobs":
            self._send_json({"data": core_jobs.list_jobs()})
        elif path.startswith("/api/v1/jobs/"):
            job_id = unquote(path[len("/api/v1/jobs/") :])
            try:
                after = int((query.get("after") or ["0"])[0])
                if after < 0:
                    raise ValueError
                raw_limit = (query.get("limit") or [None])[0]
                limit = int(raw_limit) if raw_limit is not None else None
                if limit is not None and not 0 <= limit <= 20000:
                    raise ValueError
                record = core_jobs.read_job(job_id, include_events=True, after=after, limit=limit)
            except ValueError:
                self._send_error(
                    400,
                    ErrorKind.USAGE.value,
                    "job.after_invalid",
                    "after must be a non-negative event sequence number.",
                )
                return
            except CwcliError as e:
                self._send_core_error(e)
                return
            self._send_json({"data": record})
        else:
            self._send_error(404, ErrorKind.NOT_FOUND.value, "route.not_found", "not found")

    def do_POST(self):  # noqa: N802 - stdlib hook name
        if self._refuse_foreign_host():
            return
        parsed = urlparse(self.path)
        if not _is_post_path(parsed.path):
            self._send_error(404, ErrorKind.NOT_FOUND.value, "route.not_found", "not found")
            self.close_connection = True
            return
        if parsed.path == "/api/v1/backups/upload":
            # A set's tar, streamed to disk: too big for the bounded action body. An
            # unauthenticated upload is refused without reading it (the connection
            # closes instead of draining gigabytes).
            if not self._authenticated() or self._refuse_cross_origin():
                self.close_connection = True
                if not self._authenticated():
                    self._send_unauthenticated()
                return
            if self._refuse_change_pending(parsed.path) or self._refuse_role("operator"):
                self.close_connection = True
                return
            self._receive_upload(parsed)
            return
        # Drain the body BEFORE any guard. This is HTTP/1.1 with keep-alive, so a
        # guard that answers 401/403/415 without consuming the request body leaves
        # those bytes in the socket, and the NEXT request on that connection is
        # parsed starting mid-body - which the browser sees as a bogus 501.
        try:
            raw = self._read_request_body()
        except CwcliError as e:
            self.close_connection = True
            self._send_core_error(e)
            return
        if parsed.path == "/api/v1/session":
            if self._refuse_cross_origin():
                return
            self._sign_in(raw)
            return
        if self._refuse_unauthenticated() or self._refuse_cross_origin():
            return
        if self._refuse_change_pending(parsed.path):
            return
        if parsed.path == "/api/v1/session/password":
            self._change_password(raw)
            return
        if parsed.path == "/api/v1/session/signout":
            self._sign_out(raw)
            return
        if not self._action_json_content_type():
            self._send_error(
                415,
                ErrorKind.USAGE.value,
                "request.content_type_invalid",
                "Action request body must be application/json.",
            )
            return
        try:
            payload = _parse_action_body(raw)
        except CwcliError as e:
            self._send_core_error(e)
            return
        if self._refuse_role(self._post_role(parsed.path, payload)):
            return
        try:
            if parsed.path == "/api/v1/actions":
                status, body = self._dispatch_action(payload)
            elif parsed.path == "/api/v1/jobs":
                status, body = self._create_job(payload)
            elif parsed.path == "/api/v1/settings":
                status, body = self._apply_setting(payload)
            elif parsed.path == "/api/v1/apps/scan":
                status, body = self._scan_catalog()
            elif parsed.path == "/api/v1/sites/icons":
                status, body = self._refresh_site_icons(payload)
            elif parsed.path == "/api/v1/updates/check":
                status, body = self._check_updates()
            elif parsed.path == "/api/v1/backups/settings":
                status, body = self._save_backup_settings(payload)
            elif parsed.path == "/api/v1/schedules":
                status, body = self._schedules_post(payload)
            elif parsed.path == "/api/v1/integrations":
                status, body = self._integrations_post(payload)
            elif parsed.path == "/api/v1/advisories/check":
                status, body = self._check_advisories(
                    rematch_only=bool(payload.get("rematch_only"))
                )
            elif parsed.path == "/api/v1/advisories/state":
                status, body = self._set_advisory_state(payload)
            else:
                status, body = self._control_job(parsed.path, payload)
        except CwcliError as e:
            self._send_core_error(e)
            return
        except Exception as e:  # pragma: no cover - defensive HTTP boundary
            self._send_error(500, ErrorKind.INTERNAL.value, "action.internal_error", str(e))
            return
        self._send_json(body, status=status)

    def _job_origin(self) -> dict:
        """Which UI made this job: the daemon's port, the page it was on and the client,
        written into the job spec so a start can be attributed after the fact."""
        referer = self.headers.get("Referer") or ""
        try:
            page = urlparse(referer).path if referer else None
        except ValueError:
            page = None
        addr = getattr(self.server, "server_address", None)
        ui_port = addr[1] if isinstance(addr, tuple) and len(addr) > 1 else None
        acct = self._session_account()
        return {
            "via": "web",
            "account": self._account_name(),
            "account_source": acct.source if acct is not None else "api",
            "ui_port": ui_port,
            "page": page,
            "agent": (self.headers.get("User-Agent") or "")[:80],
        }

    def _create_job(self, payload: dict) -> tuple[int, dict]:
        verb = _required_str(payload, "verb")
        target = payload.get("target") or {}
        args = payload.get("args") or {}
        secret_values = payload.get("secrets") or {}
        if not isinstance(target, dict) or not isinstance(args, dict):
            raise CwcliError(
                ErrorKind.USAGE,
                "job.payload_invalid",
                "Job target and args must be JSON objects.",
            )
        if not isinstance(secret_values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in secret_values.items()
        ):
            raise CwcliError(
                ErrorKind.USAGE,
                "job.secrets_invalid",
                "Job secrets must be a JSON object containing string values.",
            )
        unknown_secrets = set(secret_values) - {"admin_password", "db_root_password"}
        if unknown_secrets:
            raise CwcliError(
                ErrorKind.USAGE,
                "job.secret_unsupported",
                "Unsupported job secret fields: " + ", ".join(sorted(unknown_secrets)) + ".",
            )
        record = core_jobs.create_job(
            verb,
            target,
            args,
            consent=payload.get("consent") is True,
            secret_values=secret_values,
            ui_context=self._ui_context(),
            origin=self._job_origin(),
        )
        return 202, {"data": record}

    def _schedules_post(self, payload: dict) -> tuple[int, dict]:
        """``{op: save|delete|enable|run|settings, ...}`` over ``core.schedules``; every
        change records the signed-in account."""
        op = str(payload.get("op") or "")
        by = self._account_name()
        if op == "save":
            sched = payload.get("schedule")
            if not isinstance(sched, dict):
                raise CwcliError(
                    ErrorKind.USAGE, "schedule.payload_invalid", "schedule must be an object."
                )
            return 200, {"data": core_schedules.save(sched, by=by)}
        sid = str(payload.get("id") or "")
        if op == "delete":
            core_schedules.delete(sid)
            return 200, {"data": {"deleted": sid}}
        if op == "enable":
            core_schedules.set_enabled(sid, bool(payload.get("enabled", True)), by=by)
            return 200, {"data": {"id": sid, "enabled": bool(payload.get("enabled", True))}}
        if op == "run":

            def _create(verb, target, args, **kw):
                origin = {
                    **(kw.get("origin") or {}),
                    **self._job_origin(),
                    "account": by,
                    "schedule": sid,
                }
                return core_jobs.create_job(
                    verb, target, args, consent=bool(kw.get("consent")), origin=origin
                )

            return 202, {"data": core_schedules.run_now(sid, _create)}
        if op == "settings":
            core_schedules.save_settings(
                windows=(
                    payload.get("windows") if isinstance(payload.get("windows"), dict) else None
                ),
                quiet_hours=(payload.get("quiet_hours") if "quiet_hours" in payload else False),
                guards=payload.get("guards") if isinstance(payload.get("guards"), dict) else None,
                digest=payload.get("digest") if isinstance(payload.get("digest"), str) else None,
            )
            return 200, {"data": core_schedules.list_schedules()}
        raise CwcliError(
            ErrorKind.USAGE,
            "schedule.op_invalid",
            "op must be save, delete, enable, run or settings.",
        )

    def _ui_context(self) -> dict[str, str | int]:
        """What a ``self-update`` job needs to restart this daemon afterwards."""
        pid = os.getpid()
        if not isinstance(self.server, ThreadingHTTPServer):  # defensive type boundary
            raise CwcliError(ErrorKind.INTERNAL, "job.server_invalid", "Invalid HTTP server.")
        return {
            "pid": pid,
            "start": daemon_identity.process_start_time(pid) or "",
            "port": int(self.server.server_port),
            "token": self.token,
        }

    def _apply_setting(self, payload: dict) -> tuple[int, dict]:
        """One settings write, over the core function the CLI renderer calls.

        Consent stays in the core: ``cache.clear`` with ``all`` returns the
        core's ``confirm_clear`` choice (a 409) until the request carries
        ``consent: true``, which the page sends only after its own confirmation.
        ``agent_hooks.install`` is the one write outside ``core/``: it edits the
        user's OWN agent-harness config files (the ``cwcli axi setup`` opt-in),
        reports per-agent outcomes, and never bootstraps a harness it does not
        detect.
        """
        setting = _required_str(payload, "setting")
        if setting not in SETTINGS:
            raise CwcliError(
                ErrorKind.USAGE,
                "setting.unsupported",
                f"Unsupported setting '{setting}'.",
                hint="Allowed settings are " + ", ".join(sorted(SETTINGS)) + ".",
            )
        with self._action_lock(_SETTINGS_LOCK_KEY):
            if setting == "cache.clear":
                result: Result = core_config.clear_cache(
                    _optional_str(payload.get("project")),
                    all_projects=payload.get("all") is True,
                    consent=payload.get("consent") is True,
                )
            elif setting in {"cache.enable", "cache.disable"}:
                result = core_config.set_cache(setting == "cache.enable")
            elif setting == "paths.add":
                result = core_config.add_search_path(_required_str(payload, "path"))
            elif setting == "paths.remove":
                result = core_config.remove_search_path(_required_str(payload, "path"))
            elif setting == "paths.prune":
                result = core_config.prune_search_paths(apply=payload.get("apply") is True)
            elif setting == "auto_inspect.enable":
                result = core_auto_inspect.enable(
                    interval=_optional_int(payload.get("interval"), "interval"),
                    at_boot=_optional_bool(payload.get("at_boot"), "at_boot"),
                )
            elif setting == "auto_inspect.disable":
                result = core_auto_inspect.disable()
            elif setting == "auto_inspect.start":
                result = core_auto_inspect.start()
            elif setting == "auto_inspect.restart":
                result = core_auto_inspect.restart()
            elif setting == "cred_bridge.enable":
                result = core_cred_bridge.enable(
                    at_boot=_optional_bool(payload.get("at_boot"), "at_boot")
                )
            elif setting == "cred_bridge.disable":
                result = core_cred_bridge.disable()
            elif setting == "cred_bridge.start":
                result = core_cred_bridge.start()
            elif setting == "cred_bridge.restart":
                result = core_cred_bridge.restart()
            elif setting in {"tips.enable", "tips.disable"}:
                result = core_config.set_tips(setting == "tips.enable")
            elif setting == "catalog.source_add":
                result = Result(
                    status=Status.OK,
                    data={
                        "extra_sources": core_app_catalog.add_extra_source(
                            _required_str(payload, "source")
                        )
                    },
                )
            elif setting == "catalog.source_remove":
                result = Result(
                    status=Status.OK,
                    data={
                        "extra_sources": core_app_catalog.remove_extra_source(
                            _required_str(payload, "source")
                        )
                    },
                )
            else:
                assert setting == "agent_hooks.install"
                result = Result(status=Status.OK, data=agent_hooks.install())
        return _setting_response(setting, result)

    def _control_job(self, path: str, payload: dict) -> tuple[int, dict]:
        rest = path[len("/api/v1/jobs/") :]
        job_id, separator, operation = rest.partition("/")
        if not separator or operation not in _JOB_OPERATIONS:
            raise CwcliError(ErrorKind.NOT_FOUND, "route.not_found", "not found")
        job_id = unquote(job_id)
        if operation == "retry":
            return 202, {"data": core_jobs.retry_job(job_id, ui_context=self._ui_context())}
        if operation == "continue":
            target = payload.get("target") or {}
            args = payload.get("args") or {}
            if not isinstance(target, dict) or not isinstance(args, dict):
                raise CwcliError(
                    ErrorKind.USAGE,
                    "job.payload_invalid",
                    "Job target and args must be JSON objects.",
                )
            record = core_jobs.continue_job(
                job_id,
                target,
                args,
                consent=payload.get("consent") is True,
                ui_context=self._ui_context(),
            )
            return 202, {"data": record}
        record = (
            core_jobs.interrupt_job(job_id)
            if operation == "interrupt"
            else core_jobs.detach_job(job_id)
        )
        return 200, {"data": record}

    def _refuse_cross_origin(self) -> bool:
        """Send the 403 and return True when the request fails the same-origin check.

        Shared by the action endpoint and sensitive read endpoints. Those reads
        expose local environment or instance details, so on top of the no-CORS rule
        every read has, they also get the action guard's same-origin check.
        """
        if self._action_same_origin():
            return False
        self._send_error(
            403,
            ErrorKind.PRECONDITION.value,
            "action.origin_forbidden",
            "Actions must be sent from this daemon's own page.",
        )
        return True

    def _action_same_origin(self) -> bool:
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if sec_fetch_site and sec_fetch_site not in {"same-origin", "none"}:
            return False

        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == (self.headers.get("Host") or "").lower()
        )

    def _action_json_content_type(self) -> bool:
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return media_type == "application/json"

    def _read_request_body(self) -> bytes:
        """Consume the whole request body, bounded. See ``do_POST`` for why always."""
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError as e:
            raise CwcliError(
                ErrorKind.USAGE,
                "request.length_invalid",
                "Invalid Content-Length for action request.",
            ) from e
        if length < 0:
            raise CwcliError(
                ErrorKind.USAGE,
                "request.length_invalid",
                "Invalid Content-Length for action request.",
            )
        if length > _MAX_ACTION_BODY:
            raise CwcliError(
                ErrorKind.USAGE,
                "request.too_large",
                "Action request body is too large.",
            )
        return self.rfile.read(length) if length else b"{}"

    def _dispatch_action(self, payload: dict) -> tuple[int, dict]:
        action = _required_str(payload, "action")
        project = _required_str(payload, "project")
        bench = _optional_str(payload.get("bench"))
        if action not in ACTIONS:
            raise CwcliError(
                ErrorKind.USAGE,
                "action.unsupported",
                f"Unsupported action '{action}'.",
                hint="Allowed actions are " + ", ".join(sorted(ACTIONS)) + ".",
            )
        process = _required_str(payload, "process") if action == "restart_process" else None

        with self._action_lock(project):
            if action == "start_instance":
                core_jobs.check_start_port_conflicts(project)
                start_result = core_start.start(project, bench=bench)
                self._refresh_lifecycle(project, start_result.warnings)
                return _result_response(action, project, start_result)
            if action == "stop_instance":
                stop_result = core_stop.stop(project)
                self._refresh_lifecycle(project, stop_result.warnings)
                return _stop_response(action, project, stop_result)
            if action == "stop_bench":
                bench_stop = core_stop.stop_bench(project, bench=bench)
                self._refresh_process(project, bench_stop.warnings)
                return _result_response(action, project, bench_stop)
            if action == "restart_instance":
                return self._restart_instance(project, bench)
            if action == "refresh_status":
                return self._refresh_status(project)
            if action == "set_label":
                label = _required_str(payload, "label")
                label_result = core_label.set_label(project, bench=bench, label=label)
                self._refresh_process(project, label_result.warnings)
                return _result_response(action, project, label_result)
            if action == "unlock_site":
                site = _optional_str(payload.get("site"))
                unlock_result = core_unlock.unlock(project, site=site, bench=bench)
                return _result_response(action, project, unlock_result)
            if action == "clear_maintenance":
                return self._clear_maintenance(project, bench, _required_str(payload, "site"))
            if action == "scale_instance":
                # Consent is strictly the JSON boolean true, and it means consent
                # ONLY: nothing here starts a stopped instance (core.scale refuses
                # one with NOT_RUNNING before consent is even considered).
                consent = payload.get("consent") is True
                scale_result = core_scale.scale(
                    project, to=_optional_to(payload.get("to")), consent=consent
                )
                if scale_result.status is not Status.NEEDS_CHOICE:
                    self._refresh_lifecycle(project, scale_result.warnings)
                return _result_response(action, project, scale_result)
            if action == "checkout_app":
                app = _required_str(payload, "app")
                ref = _required_str(payload, "ref")
                core_apps.validate_checkout_app_name(app)
                return self._checkout_app(project, bench, app, ref)
            if action == "open_editor":
                return self._open_editor(
                    project,
                    bench,
                    _optional_str(payload.get("app")),
                    _required_str(payload, "editor"),
                )

            assert process is not None
            process_result = core_restart.restart_process(project, process, bench=bench)
            self._refresh_process(project, process_result.warnings)
            return _result_response(action, project, process_result)

    def _action_lock(self, project: str) -> threading.Lock:
        return self.action_locks[hash(project) % len(self.action_locks)]

    def _restart_instance(self, project: str, bench: str | None) -> tuple[int, dict]:
        core_jobs.check_start_port_conflicts(project)
        bench_result = core_resolvers.resolve_bench(project, bench, None)
        if bench_result is None:
            bench_path = core_resolvers.DEFAULT_BENCH_PATH
            bench_warnings: list[Message | dict[str, str]] = [
                {"code": "bench.default_used", "text": f"Using default: {bench_path}"}
            ]
        elif bench_result.status is Status.NEEDS_CHOICE:
            return _choice_response("restart_instance", project, bench_result)
        else:
            assert bench_result.data is not None
            bench_path = bench_result.data
            bench_warnings = list(bench_result.warnings)

        stop_result = core_stop.stop(project)
        start_result = core_start.start(project, bench_path=bench_path)
        assert stop_result.data is not None
        assert start_result.data is not None
        warnings = [*bench_warnings, *stop_result.warnings, *start_result.warnings]
        self._refresh_lifecycle(project, warnings)
        outcome = InstanceRestartOutcome(
            project=project,
            stopped=stop_result.data,
            started=start_result.data,
        )
        return _ok_response("restart_instance", project, Status.OK, outcome, warnings)

    def _refresh_status(self, project: str) -> tuple[int, dict]:
        """The manual status refresh: re-list the fleet, re-probe this instance.

        A read dressed as an action so it inherits the action guards; the fresh
        state also rides the SSE stream to every client as ordinary deltas.
        """
        warnings: list = []
        self._refresh_lifecycle(project, warnings)
        self._refresh_process(project, warnings)
        state = self.fleet.get(project)
        if state is None:
            raise CwcliError(
                ErrorKind.NOT_FOUND,
                "instance.not_found",
                f"No instance named '{project}' exists.",
            )
        return _ok_response(
            "refresh_status", project, Status.OK, core_fleet.as_json(state), warnings
        )

    def _clear_maintenance(self, project: str, bench: str | None, site: str) -> tuple[int, dict]:
        """Take ONE site out of maintenance mode (``core.bench_ops.clear_maintenance``).

        The Phase 1 addition to the safe set: the state ``core.status`` reports as
        ``maintenance_mode`` (a killed migrate's leftover 503) and the fix it names
        (``set-maintenance-mode off``), reachable without a terminal. Safe by
        construction - it only ever turns maintenance OFF, writes no schema, and
        a site that stays on is reported as a failure, never a success.
        """
        core_resolvers.validate_site_name(site)
        resolved = core_resolvers.resolve_container_and_bench(project, bench, None)
        if isinstance(resolved, Result):
            return _choice_response("clear_maintenance", project, resolved)
        container, bench_path, warnings = resolved
        off = core_bench_ops.clear_maintenance(container, bench_path, site, recheck=False)
        self._refresh_process(project, warnings)
        outcome = MaintenanceOutcome(
            project=project, bench_path=bench_path, site=site, maintenance_off=off
        )
        if not off:
            return (
                500,
                _error_body(
                    "step_failed",
                    "maintenance.still_on",
                    f"bench refused to take '{site}' out of maintenance mode.",
                    hint=f"Try: cwcli run {project} --site {site} set-maintenance-mode off",
                    outcome=_plain(outcome),
                    warnings=[_plain(w) for w in warnings],
                ),
            )
        return _ok_response("clear_maintenance", project, Status.OK, outcome, warnings)

    def _checkout_app(
        self, project: str, bench: str | None, app: str, ref: str
    ) -> tuple[int, dict]:
        """``apps checkout`` in its no-reset form ONLY.

        ``reset`` - checkout's one destructive element (it discards tracked local
        work) - is deliberately never read from the request payload, so no request
        can express it (the Tier A ruling; the CLI verbs are the way through a
        dirty tree). ``auto_start`` stays at its False default for the same reason
        every axi bench verb refuses it. A failed git step's reason exists only in
        git's own bytes (a checkout logs nowhere ``cwcli logs`` can serve), so a
        bounded tail of them is folded into the failure message.
        """
        tail: list[str] = []

        def _collect(event) -> None:
            if isinstance(event, core_apps.AppsOutput):
                tail.append(event.text)
                del tail[:-80]

        result = core_apps.checkout_app(project, app, ref, bench=bench, on_event=_collect)
        if result.status is Status.NEEDS_CHOICE:
            return _choice_response("checkout_app", project, result)
        report = result.data
        assert report is not None  # OK/WARNING always carries an AppsReport
        warnings = list(result.warnings)
        # The post-mutation recache epilogue, matching the human and axi verbs: a
        # failed recache is a warning, never a failure - the checkout already landed,
        # and failing here would invite retrying a mutation that succeeded.
        if any(r.ok for r in report.results) and not cache.recache_project(
            project, bench_path=report.bench_path, warnings=warnings
        ):
            warnings.append(
                Message(
                    "cache.recache_failed",
                    f"Checkout completed, but re-caching '{project}' failed; "
                    "run 'cwcli inspect --update' to refresh.",
                )
            )
        # ok reads report.ok, NOT result.status: a failed git step is a
        # WARNING-shaped envelope (the exit-code precedent, in HTTP clothes).
        if report.ok:
            return _ok_response("checkout_app", project, result.status, report, warnings)
        failed = next((r for r in report.results if not r.ok), None)
        step = f" at step '{failed.action}'" if failed else ""
        output = "".join(tail)[-600:].strip()
        return (
            422,
            _error_body(
                "step_failed",
                "app.step_failed",
                f"Checkout of '{ref}' into '{app}' failed{step}."
                + (f" Git said: {output}" if output else ""),
                outcome=_plain(report),
                warnings=[_plain(w) for w in warnings],
            ),
        )

    def _open_editor(
        self, project: str, bench: str | None, app: str | None, editor: str
    ) -> tuple[int, dict]:
        """``cwcli open --app <app> --<editor>`` from the page: the same ``core.open_plan``
        (container, bench, app dir, editor detection) and the same host handover the
        CLI performs (``vscode_utils.open_in_vscode``). A stopped instance comes back as
        the core's ``confirm_start`` choice, never auto-started; an editor that is not
        installed is the core's ``editor.not_installed`` error."""
        if editor == "docker":
            raise CwcliError(
                ErrorKind.USAGE,
                "editor.unsupported",
                "The page opens editors only; a shell is `cwcli open`.",
            )
        result = core_open.open_plan(project, bench=bench, app=app, editor=editor)
        if result.status is Status.NEEDS_CHOICE:
            return _choice_response("open_editor", project, result)
        target = result.data
        assert target is not None
        notes: list[str] = []
        core_editors.launch(
            target.editor, target.container_name, target.working_dir, log=notes.append
        )
        warnings = [*result.warnings, *(Message(code="editor.note", text=n) for n in notes)]
        return _ok_response("open_editor", project, result.status, target, warnings)

    def _refresh_lifecycle(self, project: str, warnings: list) -> None:
        try:
            self.fleet.bootstrap()
        except CwcliError as e:
            warnings.append({"code": "fleet.refresh_failed", "text": e.message})

    def _refresh_process(self, project: str, warnings: list) -> None:
        try:
            self.fleet.probe(project)
        except (CwcliError, OSError) as e:
            warnings.append({"code": "fleet.refresh_failed", "text": str(e)})

    def _send_core_error(self, e: CwcliError) -> None:
        self._send_error(
            _HTTP_FOR_KIND.get(e.kind, 500),
            e.kind.value,
            e.code,
            e.message,
            hint=e.hint,
            detail=e.detail,
        )

    def _send_detail(self, project: str) -> None:
        """The LAZY tier. Cache-backed, never auto-starts, freshness labels intact,
        plus ``cached_at`` so a remembered answer can say how old it is."""
        if not project:
            self._send_error(400, ErrorKind.USAGE.value, "project.required", "no project")
            return
        try:
            report = core_inspect.inspect(project, refresh="auto", offer_choice=False).data
        except CwcliError as e:
            self._send_core_error(e)
            return
        assert report is not None
        rows = core_config.cached_projects().data or []
        cached_at = next((r.last_updated for r in rows if r.name == project), None)
        try:
            dev_modes = core_dev_mode.read_dev_modes(project)
        except Exception:  # a cache read must never break the detail page
            dev_modes = {}
        self._send_json(
            {"data": {**asdict(report), "cached_at": cached_at, "dev_modes": dev_modes}}
        )

    def _send_logs(self, project: str, query: dict) -> None:
        """Resumable ``core.read_logs_since`` tail. A PURE READ - launches nothing.

        Same-origin only and serialized under the project's action lock, matching
        the action endpoint's guards (raw log lines are more sensitive than fleet
        health, and a tail exec'ing into a container mid-restart helps nobody).
        ``after=<process>:<offset>,...`` resumes each process from the byte offset
        the previous read returned.
        """
        if self._refuse_cross_origin():
            return
        if not project:
            self._send_error(400, ErrorKind.USAGE.value, "project.required", "no project")
            return
        bench = _optional_str((query.get("bench") or [None])[0])
        process = _optional_str((query.get("process") or [None])[0])
        raw_lines = (query.get("lines") or ["200"])[0]
        try:
            lines = int(raw_lines)
        except ValueError:
            lines = -1
        if not 1 <= lines <= 1000:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "logs.lines_invalid",
                "lines must be a whole number between 1 and 1000.",
            )
            return
        after = _parse_after((query.get("after") or [""])[0])
        try:
            with self._action_lock(project):
                result = core_logs.read_logs_since(
                    project, bench=bench, process=process, lines=lines, after=after
                )
        except CwcliError as e:
            self._send_core_error(e)
            return
        status, body = _result_response("read_logs", project, result)
        self._send_json(body, status=status)

    def _send_dev_mode(self, project: str, query: dict) -> None:
        """One site's developer-mode state, read live (``core.dev_mode.read_dev_mode``)."""
        bench_path = _optional_str((query.get("bench_path") or [None])[0])
        site = _optional_str((query.get("site") or [None])[0])
        if not project or not bench_path or not site:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "site.required",
                "project, bench_path and site are required",
            )
            return
        try:
            state = core_dev_mode.read_dev_mode(project, bench_path, site)
        except CwcliError as e:
            self._send_core_error(e)
            return
        self._send_json({"data": asdict(state)})

    def _send_site_logs(self, project: str, query: dict) -> None:
        """A site's own log files (``core.site_logs``): the file list, plus a resumable
        tail of ``file`` when one is named. Read-only; same-origin and under the
        project's action lock like ``read_logs``."""
        if self._refuse_cross_origin():
            return
        bench_path = _optional_str((query.get("bench_path") or [None])[0])
        site = _optional_str((query.get("site") or [None])[0])
        file = _optional_str((query.get("file") or [None])[0])
        if not project or not bench_path or not site:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "site.required",
                "project, bench_path and site are required",
            )
            return
        try:
            lines = int((query.get("lines") or ["200"])[0])
        except ValueError:
            lines = -1
        if not 1 <= lines <= 1000:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "logs.lines_invalid",
                "lines must be a whole number between 1 and 1000.",
            )
            return
        raw_after = (query.get("after") or [""])[0]
        try:
            with self._action_lock(project):
                if file == "*":
                    # Every live file at once, each with its own cursor (JSON name -> offset).
                    cursors: dict[str, int] = {}
                    if raw_after.startswith("{"):
                        try:
                            parsed = json.loads(raw_after)
                        except ValueError:
                            parsed = {}
                        if isinstance(parsed, dict):
                            cursors = {
                                str(k): int(v) for k, v in parsed.items() if isinstance(v, int)
                            }
                    payload = core_site_logs.read_site_logs(
                        project, bench_path, site, lines=min(lines, 200), after=cursors
                    )
                    self._send_json({"data": {**payload, "tail": None}})
                    return
                after = int(raw_after) if raw_after.isdigit() else None
                listing = core_site_logs.list_site_logs(project, bench_path, site)
                tail = (
                    core_site_logs.read_site_log(
                        project, bench_path, site, file, lines=lines, after=after
                    )
                    if file
                    else None
                )
        except CwcliError as e:
            self._send_core_error(e)
            return
        self._send_json({"data": {**listing, "tail": tail}})

    def _send_error_log(self, project: str, query: dict) -> None:
        """The site's Error Log entries, newest first (``core.site_logs.read_error_log``)."""
        if self._refuse_cross_origin():
            return
        bench_path = _optional_str((query.get("bench_path") or [None])[0])
        site = _optional_str((query.get("site") or [None])[0])
        if not project or not bench_path or not site:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "site.required",
                "project, bench_path and site are required",
            )
            return
        raw_limit = (query.get("limit") or ["50"])[0]
        limit = int(raw_limit) if raw_limit.isdigit() else 50
        try:
            with self._action_lock(project):
                data = core_site_logs.read_error_log(project, bench_path, site, limit=limit)
        except CwcliError as e:
            self._send_core_error(e)
            return
        self._send_json({"data": data})

    def _send_url(self, project: str, query: dict) -> None:
        """Fresh host-URL + reachability probe for one bench (``core.url.probe_url``).

        A PURE READ, but it execs ``curl`` inside the container, so it is
        same-origin only and serialized under the project's action lock - the
        ``read_logs`` precedent. A stopped or ambiguous-multi-bench project comes
        back as the core's ``NEEDS_CHOICE`` (rendered as a 409), never
        auto-started.
        """
        if self._refuse_cross_origin():
            return
        if not project:
            self._send_error(400, ErrorKind.USAGE.value, "project.required", "no project")
            return
        bench = _optional_str((query.get("bench") or [None])[0])
        site = _optional_str((query.get("site") or [None])[0])
        try:
            with self._action_lock(project):
                result = core_url.probe_url(project, bench=bench, site=site)
        except CwcliError as e:
            self._send_core_error(e)
            return
        status, body = _result_response("probe_url", project, result)
        self._send_json(body, status=status)

    # ------------------------------------------------------------ apps catalog

    def _send_metrics(self, query: dict) -> None:
        """The rolling performance window (``core.metrics``): one instance's points
        when ``?project=`` names one (empty for a stopped or unknown instance -
        never a flat fake line), plus the host series either way."""
        project = (query.get("project") or [None])[0]
        try:
            window = float((query.get("window") or ["3600"])[0])
        except ValueError:
            window = -1.0
        if not 0 < window <= 366 * 24 * 3600:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "metrics.window_invalid",
                "window must be a number of seconds greater than 0.",
            )
            return
        since = time.time() - window
        body: dict = {"host": self.metrics.host_series(since), "window": {"since": since}}
        if project:
            body["project"] = project
            body["points"] = self.metrics.series(project, since)
            state = self.fleet.get(project)
            body["running"] = bool(state and state.container_running)
        else:
            body["projects"] = {
                p: self.metrics.series(p, since) for p in self.fleet.running_projects()
            }
        self._send_json({"data": body})

    def _send_catalog(self, query: dict) -> None:
        """The apps catalog (``core.app_catalog``): installed + remote, logos same-origin.

        Same-origin only (it names every repo the host's forge sign-ins reach).
        ``?installed=fresh`` forces the installed half to be re-read now instead
        of within its 30s memo; the remote half never blocks on the network -
        ``POST /api/v1/apps/scan`` refreshes it in the background and a
        ``catalog`` SSE frame announces when it landed.
        """
        if self._refuse_cross_origin():
            return
        force = (query.get("installed") or [""])[0] == "fresh"
        try:
            catalog = self.catalog.get(force_installed=force)
        except CwcliError as e:
            self._send_core_error(e)
            return
        self._send_json({"data": core_app_catalog.as_json(catalog)})

    def _send_advisories(self) -> None:
        """Advisories matched against what runs here (``core.advisories``); a stale
        (older than an hour) result starts a background check first."""
        snap = self.advisories.snapshot()
        if self.advisories.stale() and not snap["checking"]:
            self._check_advisories()
            snap = self.advisories.snapshot()
        self._send_json({"data": snap})

    def _check_advisories(self, *, rematch_only: bool = False) -> tuple[int, dict]:
        catalog = self.catalog.get()
        started = self.advisories.refresh(
            catalog.apps,
            lambda data: self.hub.publish_event("advisories", data),
            rematch_only=rematch_only,
        )
        return 202, {"data": {"started": started, "checking": True, "rematch_only": rematch_only}}

    def _set_advisory_state(self, payload: dict) -> tuple[int, dict]:
        adv_id = payload.get("id")
        state = payload.get("state")
        if not isinstance(adv_id, str) or not isinstance(state, str):
            raise CwcliError(
                ErrorKind.USAGE, "advisory.state_invalid", "Body needs an advisory id and a state."
            )
        try:
            snap = self.advisories.set_state(
                adv_id, state, payload.get("note") if isinstance(payload.get("note"), str) else None
            )
        except ValueError as e:
            raise CwcliError(ErrorKind.USAGE, "advisory.state_invalid", str(e)) from e
        self.hub.publish_event("advisories", snap)
        return 200, {"data": snap}

    def _send_updates(self) -> None:
        """Updates available for installed apps (``core.updates``): the last check,
        refreshed in the background when older than an hour."""
        snap = self.updates.snapshot()
        if self.updates.stale() and not snap["checking"]:
            self._check_updates()
            snap = self.updates.snapshot()
        self._send_json({"data": snap})

    def _send_backups(self, query: dict) -> None:
        """The backup store (``core.backup_store``): every set (or one project's), the
        last good set per site with its age, the disk budget and the settings."""
        if self._refuse_cross_origin():
            return
        project = _optional_str((query.get("project") or [None])[0])
        self._send_json({"data": backups_snapshot(project)})

    def _save_backup_settings(self, payload: dict) -> tuple[int, dict]:
        project = payload.get("project")
        values = payload.get("values")
        if not isinstance(project, str) or not project or not isinstance(values, dict):
            return 400, _error_body(
                ErrorKind.USAGE.value, "backups.settings", "project and values are required"
            )
        try:
            saved = core_backup_store.save_settings(project, values)
        except CwcliError as e:
            return 400, _error_body(e.kind.value, e.code, e.message, hint=e.hint)
        return 200, {"data": {"project": project, "settings": saved}}

    def _receive_upload(self, parsed) -> None:
        query = parse_qs(parsed.query)
        project = _optional_str((query.get("project") or [None])[0])
        site = _optional_str((query.get("site") or [None])[0])
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_UPLOAD_BODY:
            self.close_connection = True
            self._send_error(
                413,
                ErrorKind.USAGE.value,
                "upload.size",
                "Upload needs a Content-Length under 50 GB.",
            )
            return
        inbox = core_backup_store.store_root() / ".inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="upload-", suffix=".tar", dir=inbox)
        tmp = Path(tmp_name)
        try:
            remaining = length
            with os.fdopen(fd, "wb") as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                self.close_connection = True
                self._send_error(
                    400, ErrorKind.USAGE.value, "upload.short", "The upload ended early."
                )
                return
            result = core_transfer.import_tar(tmp, project=project, site=site)
        except CwcliError as e:
            self._send_core_error(e)
            return
        finally:
            tmp.unlink(missing_ok=True)
        try:
            self.hub.publish_event("backups", backups_snapshot())
        except Exception:  # pragma: no cover - a refresh must never break the upload
            pass
        self._send_json({"data": result.data})

    def _integrations_post(self, payload: dict) -> tuple[int, dict]:
        """``{op: test|configure|disable|action, id, ...}`` over ``core.integrations``.

        ``settings`` and ``secrets`` are laid over the stored values (an empty secret
        keeps the stored one); no answer ever carries a secret. ``configure`` with
        ``enable: true`` turns the integration on only when its test passes, and
        answers the test either way.
        """
        op = str(payload.get("op") or "")
        integration_id = _required_str(payload, "id")
        settings = payload.get("settings")
        secret_values = payload.get("secrets")
        by = self._account_name()
        if op == "test":
            tested = core_integrations.test(integration_id, settings, secret_values)
            return 200, {"data": {"check": asdict(tested)}}
        if op == "configure":
            enable = payload.get("enable")
            with self._action_lock(_SETTINGS_LOCK_KEY):
                check, view = core_integrations.configure(
                    integration_id,
                    settings,
                    secret_values,
                    enable=enable if isinstance(enable, bool) else None,
                    by=by,
                )
            return 200, {"data": {"check": asdict(check) if check else None, "integration": view}}
        if op == "disable":
            with self._action_lock(_SETTINGS_LOCK_KEY):
                view = core_integrations.disable(integration_id, by=by)
            return 200, {"data": {"integration": view}}
        if op == "action":
            args = payload.get("args") or {}
            if not isinstance(args, dict):
                raise CwcliError(ErrorKind.USAGE, "integration.args", "args must be an object.")
            result = core_integrations.run_action(
                integration_id, _required_str(payload, "action"), args, settings, secret_values
            )
            return 200, {"data": {"result": result}}
        raise CwcliError(
            ErrorKind.USAGE,
            "integration.op_invalid",
            "op must be test, configure, disable or action.",
        )

    def _check_updates(self) -> tuple[int, dict]:
        catalog = self.catalog.get()
        started = self.updates.refresh(
            catalog.apps, lambda data: self.hub.publish_event("updates", data)
        )
        return 202, {"data": {"started": started, "checking": True}}

    def _refresh_site_icons(self, payload: dict) -> tuple[int, dict]:
        """Read the favicons of the listed sites in the background (``core.site_icons``):
        live through the bench for a running instance, from the bench files on disk
        for a stopped one. The ``site_icons`` SSE frame carries the result."""
        sites = payload.get("sites") if isinstance(payload, dict) else None
        if not isinstance(sites, list):
            raise CwcliError(ErrorKind.USAGE, "sites.required", "Body must carry a 'sites' list.")
        clean = []
        for s in sites[:500]:
            if not isinstance(s, dict):
                continue
            project, site, bench_path = s.get("project"), s.get("site"), s.get("bench_path")
            if not (
                isinstance(project, str) and isinstance(site, str) and isinstance(bench_path, str)
            ):
                continue
            if not is_valid_project_name(project):
                continue
            core_resolvers.validate_site_name(site)
            clean.append(
                {
                    "project": project,
                    "site": site,
                    "bench_path": bench_path,
                    "running": bool(s.get("running")),
                }
            )
        started = self.site_icons.refresh(
            clean,
            lambda data: self.hub.publish_event("site_icons", data),
            force=bool(payload.get("force")),
        )
        return 202, {"data": {"started": started, "icons": self.site_icons.all()}}

    def _scan_catalog(self) -> tuple[int, dict]:
        def _landed(data: dict) -> None:
            self.hub.publish_event("catalog", data)
            if "scanned_at" in data:  # the scan finished: re-check updates on the fresh catalog
                try:
                    self._check_updates()
                except Exception:
                    pass

        started = self.catalog.refresh(_landed)
        return 202, {"data": {"scanning": True, "started": started}}

    def _send_logo(self, name: str) -> None:
        """One cached logo by its content-addressed name; anything else is 404.

        Served with ``nosniff`` and the type sniffed from the bytes at store time;
        the page renders SVG only through ``<img>``, where it cannot run script.
        """
        path = core_app_catalog.logo_path(name)
        if path is None:
            self._send_error(404, ErrorKind.NOT_FOUND.value, "logo.not_found", "not found")
            return
        self._send_bytes(
            path.read_bytes(),
            core_app_catalog.content_type_for(name),
            csp="default-src 'none'; style-src 'unsafe-inline'; sandbox",
            cache="public, max-age=31536000, immutable",
        )

    def _send_doctor(self) -> None:
        """System-wide, read-only environment preflight (``core.doctor.run_all``).

        Launches, pulls and mutates nothing (every check is a pure read); each
        check's ``pass``/``warn``/``fail`` and the ``version_verified``
        qualifier ride through unchanged so the page renders staleness honestly.
        """
        if self._refuse_cross_origin():
            return
        report = core_doctor.run_all().data
        assert report is not None
        self._send_json({"data": _doctor_json(report)})

    def _send_config(self) -> None:
        """``core.config.show_config``, the read half of Settings (writes are
        ``POST /api/v1/settings``)."""
        if self._refuse_cross_origin():
            return
        report = core_config.show_config().data
        assert report is not None
        self._send_json({"data": asdict(report)})

    def _send_cache(self) -> None:
        """The cache inventory: every remembered project, live or not."""
        if self._refuse_cross_origin():
            return
        rows = core_config.cached_projects().data or []
        self._send_json({"data": [asdict(r) for r in rows]})

    def _send_where(self, query: dict) -> None:
        """``core.where``: the cache search, verified/remembered tokens intact.

        Same-origin only, like ``/logs``. The per-row ``project_state`` and the
        result-level ``verified`` flag pass through unchanged so the page can
        render remembered rows as remembered instead of presenting a cached
        answer as a live one.
        """
        if self._refuse_cross_origin():
            return
        term = ((query.get("q") or [""])[0] or "").strip()
        if not term:
            self._send_error(
                400,
                ErrorKind.USAGE.value,
                "where.term_required",
                "A non-empty search term 'q' is required.",
            )
            return
        try:
            result = core_where.where(term)
        except CwcliError as e:
            self._send_core_error(e)
            return
        assert result.data is not None  # where never returns NEEDS_CHOICE
        self._send_json(
            {
                "data": {
                    **asdict(result.data),
                    "warnings": [_plain(w) for w in result.warnings],
                }
            }
        )

    def _stream_events(self, focus: str | None) -> None:
        client_id, q = self.hub.subscribe(focus)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # No Content-Length and no chunking: the body ends when the connection
            # does, which is what an infinite stream means over HTTP/1.1.
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(
                _sse_frame(
                    "snapshot",
                    {"instances": self.fleet.snapshot(), "jobs": core_jobs.list_jobs()},
                    self.hub.next_id(),
                )
            )
            self.wfile.flush()
            while True:
                try:
                    frame = q.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    frame = b": keepalive\n\n"
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the client went away; the finally below is the whole cleanup
        finally:
            self.hub.unsubscribe(client_id)


_JOB_OPERATIONS = {"interrupt", "detach", "retry", "continue"}
_POST_PATHS = {
    "/api/v1/actions",
    "/api/v1/session",
    "/api/v1/session/password",
    "/api/v1/session/signout",
    "/api/v1/jobs",
    "/api/v1/settings",
    "/api/v1/apps/scan",
    "/api/v1/sites/icons",
    "/api/v1/updates/check",
    "/api/v1/backups/settings",
    "/api/v1/schedules",
    "/api/v1/integrations",
    "/api/v1/backups/upload",
    "/api/v1/advisories/check",
    "/api/v1/advisories/state",
}


def _is_post_path(path: str) -> bool:
    if path in _POST_PATHS:
        return True
    if not path.startswith("/api/v1/jobs/"):
        return False
    _job_id, separator, operation = path[len("/api/v1/jobs/") :].partition("/")
    return bool(separator and operation in _JOB_OPERATIONS)


def _parse_after(raw: str) -> dict[str, int] | None:
    """``web:1234,worker_default:99`` -> ``{"web": 1234, ...}``; malformed entries drop."""
    after: dict[str, int] = {}
    for item in raw.split(","):
        process, sep, offset = item.strip().partition(":")
        if sep and process and offset.isdigit():
            after[process] = int(offset)
    return after or None


def _parse_action_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CwcliError(
            ErrorKind.USAGE,
            "request.json_invalid",
            "Action request body must be JSON.",
        ) from e
    if not isinstance(body, dict):
        raise CwcliError(
            ErrorKind.USAGE,
            "request.json_object_required",
            "Action request body must be a JSON object.",
        )
    return body


def _required_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CwcliError(
            ErrorKind.USAGE,
            f"action.{key}_required",
            f"Action requires a non-empty '{key}' string.",
        )
    return value.strip()


def _optional_to(value) -> int | None:
    """Scale's optional bench-count floor: a JSON integer or absent, nothing else.

    ``bool`` is excluded explicitly because it is an ``int`` subclass and a JSON
    ``true`` must not read as "cover 1 bench". Range validation stays in
    ``core.scale`` (the single owner of that rule).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CwcliError(
            ErrorKind.USAGE,
            "action.to_invalid",
            "Action 'to' must be an integer when present.",
        )
    return int(value)


def _optional_int(value, key: str) -> int | None:
    """A JSON integer or absent; ``bool`` excluded (it is an ``int`` subclass)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CwcliError(
            ErrorKind.USAGE, f"setting.{key}_invalid", f"'{key}' must be an integer when present."
        )
    return int(value)


def _optional_bool(value, key: str) -> bool | None:
    """A JSON boolean or absent (``None`` leaves the setting untouched in the core)."""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise CwcliError(
            ErrorKind.USAGE,
            f"setting.{key}_invalid",
            f"'{key}' must be true or false when present.",
        )
    return value


def _optional_str(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise CwcliError(
            ErrorKind.USAGE,
            "action.bench_invalid",
            "Action 'bench' must be a string when present.",
        )
    return value.strip() or None


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _doctor_json(report) -> dict:
    """Serialize a ``DoctorReport``, resolving each check's ``CheckStatus`` enum.

    ``_plain``/``asdict`` would leave the nested enum unserializable, so this is
    the one place the ``pass``/``warn``/``fail`` token is flattened to its value.
    """
    return {
        "checks": [
            {
                "id": c.id,
                "title": c.title,
                "group": c.group,
                "status": c.status.value,
                "detail": c.detail,
                "fix": c.fix,
                "version_verified": c.version_verified,
                "action": c.action,
            }
            for c in report.checks
        ],
        "passed": report.passed,
        "warned": report.warned,
        "failed": report.failed,
        "ok": report.ok,
    }


def _ok_response(action: str, project: str, status: Status, outcome, warnings) -> tuple[int, dict]:
    return (
        200,
        {
            "data": {
                "action": action,
                "project": project,
                "status": status.value,
                "outcome": _plain(outcome),
                "warnings": [_plain(w) for w in warnings],
            }
        },
    )


def _setting_response(setting: str, result: Result) -> tuple[int, dict]:
    if result.status is Status.NEEDS_CHOICE:
        return _choice_response(setting, "", result)
    return (
        200,
        {
            "data": {
                "setting": setting,
                "status": result.status.value,
                "outcome": _plain(result.data),
                "warnings": [_plain(w) for w in result.warnings],
            }
        },
    )


def _result_response(action: str, project: str, result: Result) -> tuple[int, dict]:
    if result.status is Status.NEEDS_CHOICE:
        return _choice_response(action, project, result)
    return _ok_response(action, project, result.status, result.data, result.warnings)


def _stop_response(action: str, project: str, result: Result) -> tuple[int, dict]:
    outcome = result.data
    if outcome is None or outcome.db_clean_shutdown is not False:
        return _result_response(action, project, result)
    warnings = [_plain(w) for w in result.warnings]
    warning = next(
        (w for w in warnings if w.get("code") == "stop.db_unclean"),
        {"text": "The database did not shut down cleanly."},
    )
    return (
        500,
        _error_body(
            "unclean_shutdown",
            "stop.db_unclean",
            warning["text"],
            action=action,
            outcome=_plain(outcome),
            warnings=warnings,
        ),
    )


def _choice_response(action: str, project: str, result: Result) -> tuple[int, dict]:
    choice = result.choice
    message = choice.prompt if choice is not None else "This action requires a choice."
    code = choice.kind if choice is not None else "choice_required"
    return (
        409,
        _error_body(
            "needs_choice",
            code,
            message,
            hint=(
                f"Start the instance first (cwcli start {project}), then retry."
                if code == "confirm_start"
                else (
                    "Confirm, then retry with consent: true."
                    if choice is not None and choice.param == "consent"
                    else "Select a single bench or process, then retry."
                )
            ),
            action=action,
            choice=_plain(choice) if choice is not None else None,
            warnings=[_plain(w) for w in result.warnings],
        ),
    )


def backups_snapshot(project: str | None = None) -> dict:
    """What the Backups page and a site's Backups tab read: sets newest first, the last
    good set per (project, site) with its age in days, the store's disk budget, and each
    instance's settings (per-instance values over the captain's defaults)."""
    sets = core_backup_store.list_sets(project)
    rows = [b.to_dict() for b in sets]
    last_good: dict[str, dict] = {}
    for b in sets:
        key = f"{b.project}/{b.site}"
        if b.verified and key not in last_good:
            try:
                age_days = (
                    time.time()
                    - time.mktime(time.strptime(b.created_at, "%Y-%m-%dT%H:%M:%SZ"))
                    + time.timezone
                ) / 86400
            except (ValueError, OverflowError):
                age_days = None
            last_good[key] = {"key": b.key, "created_at": b.created_at, "age_days": age_days}
    projects = sorted({b.project for b in sets} | ({project} if project else set()))
    settings = {p: core_backup_store.settings(p) for p in projects}
    drills: dict[str, dict] = {}
    for b in sets:
        if b.drill and f"{b.project}/{b.site}" not in drills:
            drills[f"{b.project}/{b.site}"] = {**b.drill, "set_key": b.key}
    budget = core_backup_store.disk_budget()
    return {
        "sets": rows,
        "last_good": last_good,
        "budget": budget,
        "settings": settings,
        "drills": drills,
        "reasons": list(core_backup_store.REASONS),
    }


def make_server(
    host: str,
    port: int,
    fleet: core_fleet.Fleet,
    hub: _Hub,
    token: str,
    catalog: core_app_catalog.CatalogStore | None = None,
    metrics: core_metrics.MetricsStore | None = None,
    site_icons: core_site_icons.SiteIconStore | None = None,
    updates: core_updates.UpdatesStore | None = None,
    advisories: core_advisories.AdvisoriesStore | None = None,
) -> ThreadingHTTPServer:
    """Bind the HTTP server, handing the handler class its fleet, hub and credentials.

    There is no unauthenticated mode: a programmatic client sends the bearer
    ``token``; a browser signs in with the administrator account
    (``core.accounts``), whose sessions live in the account store, so they
    survive a daemon restart.
    """
    core_accounts.ensure_default()
    handler = type(
        "Handler",
        (_Handler,),
        {
            "fleet": fleet,
            "hub": hub,
            "catalog": catalog or core_app_catalog.CatalogStore(),
            "metrics": metrics or core_metrics.MetricsStore(),
            "site_icons": site_icons or core_site_icons.SiteIconStore(),
            "updates": updates or core_updates.UpdatesStore(),
            "advisories": advisories or core_advisories.AdvisoriesStore(),
            "action_locks": tuple(threading.Lock() for _ in range(_ACTION_LOCK_STRIPES)),
            "token": token,
            "lockout": core_accounts.Lockout(),
            "remote_bind": not is_loopback(host),
        },
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def _url_host(host: str) -> str:
    """``host`` as it must appear in a URL: an IPv6 literal is bracketed."""
    bare = host.strip("[]")
    return f"[{bare}]" if ":" in bare else bare


def _binds_everywhere(host: str) -> bool:
    """True when ``host`` is the wildcard bind, which names no address to browse to."""
    try:
        return not host or ipaddress.ip_address(host.strip("[]")).is_unspecified
    except ValueError:
        return False


def _outbound_address() -> str | None:
    """This host's own address on the interface that reaches the outside, or None.

    On WSL this is the address a browser on the Windows side connects to when
    localhost forwarding is off or the daemon is reached from another machine, so
    printing it is the difference between the boundary working and guessing.

    A UDP socket ``connect`` to a documentation address (TEST-NET-1, never
    routed) sends NOTHING - it only makes the kernel pick a source interface -
    and is the portable way to ask that question. Resolving the hostname is the
    obvious alternative and is wrong here: on this WSL distro it answers
    ``127.0.1.1`` and nothing else.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        addr = str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()
    return None if addr.startswith("127.") else addr


def page_url(host: str, port: int) -> str:
    """The plain address of the page for a daemon bound at ``host:port``."""
    return f"http://{_url_host(host)}:{port}/"


def _recover_daemons() -> None:
    for note in daemon_recovery.recover():
        console.print(note.note)


def run_daemon(
    host: str,
    port: int,
    interval: float,
    *,
    env_token: str | None,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Bootstrap the fleet, bind, print the banner, serve until interrupted.

    The body shared by the ``serve`` module entry and ``cwcli ui --foreground``.
    ``on_ready`` is called once with the page address after the bind succeeds,
    which is how ``ui --foreground`` opens the browser. Raises ``typer.Exit`` on
    a failed bootstrap or bind (the callers are both Typer commands).
    """
    global _running_build
    # The two reference each other (the fleet publishes through the hub; the hub
    # reports focus back to the fleet), so the delta sink is bound after both exist.
    fleet = core_fleet.Fleet()
    hub = _Hub(fleet.set_focus)
    fleet.set_publish(hub.publish)
    stop = threading.Event()

    def _warn(exc: Exception) -> None:
        stderr_console.print(f"[yellow]serve:[/yellow] {exc}")

    try:
        fleet.bootstrap()
    except CwcliError as e:
        stderr_console.print(f"[red]{e.message}[/red]")
        raise typer.Exit(code=1) from e

    metrics_store = core_metrics.MetricsStore()
    advisories_store = core_advisories.AdvisoriesStore()
    updates_store = core_updates.UpdatesStore()
    catalog = core_app_catalog.CatalogStore()
    # Anything that changes what is installed re-matches the advisories (no fetch):
    # a cheap pass on the fresh inventory, as the locked spec asks.
    recheck_verbs = {
        "apps-install",
        "apps-update",
        "apps-checkout",
        "new-site",
        "new-bench",
        "init",
        "rm",
        "rm-bench",
        "rm-site",
        "clone-sites",
        "move-sites",
    }

    backup_verbs = {
        "clone-sites",
        "move-sites",
        "receive-backup",
        "prune-backups",
        "backup-sites",
        "s3-copy",
        "s3-fetch",
        "s3-prune",
        "restore-drill",
        "backup",
        "restore-set",
        "verify-backup",
        "delete-backup",
        "rm",
        "rm-site",
        "rm-bench",
        "apps-update",
        "migrate",
        "apps-install",
    }

    def _on_job(record: dict) -> None:
        hub.publish_job(record)
        if record.get("state") in ("succeeded", "failed") and record.get("verb") in backup_verbs:
            try:
                hub.publish_event("backups", backups_snapshot())
            except Exception as e:  # pragma: no cover - a refresh must never break job delivery
                _warn(e)
        if record.get("state") == "succeeded" and record.get("verb") == "check-updates":
            # The job wrote the snapshot in its own process; serve it and tell every tab.
            updates_store.reload()
            hub.publish_event("updates", updates_store.snapshot())
        if record.get("state") == "succeeded" and record.get("verb") in recheck_verbs:
            try:
                advisories_store.refresh(
                    catalog.get().apps,
                    lambda d: hub.publish_event("advisories", d),
                    rematch_only=True,
                )
            except Exception as e:  # pragma: no cover - a re-match must never break job delivery
                _warn(e)

    def _schedule_loop() -> None:
        """Once a minute: the backup settings' daily run (phase 3), then every schedule
        (``core.schedules.tick``: due slots, guards, recipes and event triggers). A busy
        instance is retried on the next tick; the tick never takes the daemon down."""
        tick_state: dict = {}
        while not stop.wait(60):
            try:
                for project in [d.project_name for d in (core_list.list_instances().data or [])]:
                    if not core_backup_store.schedule_due(project):
                        continue
                    try:
                        core_jobs.create_job(
                            "backup-sites",
                            {"project": project},
                            {"reason": "scheduled"},
                            consent=True,
                            origin={"schedule": "backups", "account": "schedule"},
                        )
                    except CwcliError as e:
                        if e.code != "job.project_busy":
                            _warn(e)
                schedule_cmd.tick_once(tick_state)
            except Exception as e:  # the tick must never take the daemon down
                _warn(e)

    threads = [
        threading.Thread(target=_schedule_loop, daemon=True),
        threading.Thread(
            target=core_fleet.event_loop,
            args=(fleet, stop),
            kwargs={"on_error": _warn},
            daemon=True,
        ),
        threading.Thread(
            target=core_fleet.probe_loop,
            args=(fleet, stop, interval),
            kwargs={"on_error": _warn},
            daemon=True,
        ),
        threading.Thread(
            target=core_jobs.watch_loop,
            args=(_on_job, stop),
            kwargs={"active": lambda: hub.client_count() > 0},
            daemon=True,
        ),
        threading.Thread(
            target=core_metrics.metrics_loop,
            args=(
                fleet,
                metrics_store,
                lambda frame: hub.publish_event("metrics", frame),
                stop,
                interval,
            ),
            kwargs={"on_error": _warn},
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()

    _running_build = core_version.install_fingerprint()
    try:
        httpd = make_server(
            host,
            port,
            fleet,
            hub,
            env_token or secrets.token_urlsafe(32),
            catalog=catalog,
            metrics=metrics_store,
            updates=updates_store,
            advisories=advisories_store,
        )
    except OSError as e:
        stderr_console.print(f"[red]Could not bind {host}:{port}: {e}[/red]")
        stop.set()
        raise typer.Exit(code=1) from e

    bound, port = str(httpd.server_address[0]), httpd.server_address[1]
    console.print(f"[bold]cwcli ui[/bold] on [cyan]http://{_url_host(bound)}:{port}[/cyan]")
    console.print(f"  instances: {len(fleet.snapshot())}   probe interval: {interval}s")
    if not webui_available():
        console.print("  [yellow]web UI not built[/yellow] (cd webui && npm ci && npm run build)")
    # The token is NEVER printed - only which one is in force. The page signs in
    # with the administrator account (admin / admin the first time).
    in_force = f"${TOKEN_ENV}" if env_token else "per-launch token"
    console.print(f"  auth: [green]required[/green] ({in_force})")
    sign_in = _url_host((_outbound_address() or bound) if _binds_everywhere(bound) else bound)
    link = page_url(sign_in, port)
    console.print(f"  sign in: [cyan]{link}[/cyan] (admin / admin the first time)", soft_wrap=True)
    if "microsoft" in platform.uname().release.lower():
        console.print(f"  from Windows: [cyan]http://localhost:{port}/[/cyan] (WSL forwarding)")
    console.print("  Ctrl-C to stop")
    if on_ready is not None:
        on_ready(link)

    # An enabled background daemon that is not running (an upgrade, a reboot, a broken
    # boot unit) comes back now, and the log says plainly when it cannot.
    threading.Thread(target=_recover_daemons, daemon=True).start()
    # The remote half of the apps catalog: refresh it in the background when the
    # on-disk copy is missing or older than an hour; the page can ask for more.
    scanned = catalog.scanned_at()
    if scanned is None or time.time() - scanned > 3600:
        catalog.refresh(lambda data: hub.publish_event("catalog", data))

    # main.cli() resets SIGPIPE to SIG_DFL so `cwcli ls | head` exits quietly; in
    # this long-lived server that lets one closed browser tab's next stream write
    # kill the whole daemon. Ignored, the write raises BrokenPipeError instead,
    # which the handlers already treat as a client going away. POSIX only.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\nstopping.")
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()


def serve(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Port to listen on."),
    host: str = typer.Option(
        DEFAULT_HOST,
        "--host",
        help="Address to bind. Defaults to loopback, which a Windows browser still "
        f"reaches under WSL. Any value other than a loopback IP literal requires "
        f"${TOKEN_ENV}.",
    ),
    interval: float = typer.Option(
        DEFAULT_INTERVAL,
        "--interval",
        help="Seconds between FAST-tier health probes of each running instance.",
    ),
):
    """Serve the web UI and live fleet model over HTTP + SSE (foreground; Ctrl-C to stop)."""
    if interval <= 0:
        stderr_console.print("[red]--interval must be greater than 0.[/red]")
        raise typer.Exit(code=2)

    # Refuse BEFORE anything binds or probes. Reaching the daemon from the
    # network is a deliberate act, so it takes a secret the operator chose, never
    # only the per-launch token every loopback run gets.
    env_token = auth_token()
    if env_token is None and not is_loopback(host):
        stderr_console.print(
            f"[red]Refusing to bind {host} without {TOKEN_ENV}.[/red]\n"
            f"  That address is reachable from other machines, so exposing the "
            f"daemon there takes a token you chose.\n"
            f"  Set {TOKEN_ENV} to a secret of your choosing, or bind "
            f"{DEFAULT_HOST} (which a Windows browser still reaches under WSL)."
        )
        raise typer.Exit(code=2)
    run_daemon(host, port, interval, env_token=env_token)


if __name__ == "__main__":
    # The INTERNAL developer entry point: the one place a non-loopback ``--host``
    # can be given. The public command is ``cwcli ui`` (commands/ui.py), which
    # always binds loopback. ``typer.run`` gives this one function real
    # ``--host/--port/--interval`` parsing without a registered app.
    typer.run(serve)
