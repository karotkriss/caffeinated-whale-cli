"""``cwcli serve`` - the streaming Console daemon, a third frontend over the core.

A FOREGROUND command started by hand and stopped with Ctrl-C. There is no
auto-start, no background service, no boot unit: a dashboard nobody is looking at
should not be probing anyone's Docker daemon.

Stdlib only (``http.server.ThreadingHTTPServer`` + ``queue`` + ``threading``) over
``docker``, which is already a runtime dependency - no web framework, no asyncio,
no new dependency. Browser-native SSE over plain HTTP is what keeps this
cross-platform down to "any browser", including the case this was built for: a
**Windows** browser reaching a daemon bound inside **WSL**.

Endpoints:

* ``GET /``                            - the Console browser UI
* ``GET /api/snapshot``                - the whole fleet model as JSON
* ``GET /api/events[?focus=<project>]``- SSE: one ``snapshot`` event, then
  ``delta`` events tagged ``tier: instant|fast``
* ``GET /api/instance/<project>/detail``- the LAZY tier: cache-backed
  ``core.inspect``, carrying its ``served_from`` and ``installed_apps_verified``
  freshness labels through unchanged
* ``POST /api/action``                 - the v1 Console rail's narrow safe set:
  start/stop/restart one instance, or restart one supervised process

``?focus=<project>`` is how the browser says which instance it currently has
open, and it is the whole mechanism behind the web-probe cadence: the SSE
connection itself carries the answer, so closing the tab retracts focus when a
later delta or keepalive discovers the closed response stream. See
``core.fleet.Fleet.set_focus``.

**Binding.** The default is ``0.0.0.0`` because the primary environment is WSL
and a Windows browser cannot reach a WSL-only ``127.0.0.1`` listener. Every
endpoint names local projects, ports and sites, and the action endpoint can drive
non-destructive lifecycle operations, so ``--host 127.0.0.1`` is there for anyone
on an untrusted network. CORS remains open only for the read endpoints; the action
endpoint is same-origin only.
"""

from __future__ import annotations

import json
import platform
import queue
import socket
import threading
from dataclasses import asdict, dataclass, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import typer

from ..core import fleet as core_fleet
from ..core import inspect as core_inspect
from ..core import resolvers as core_resolvers
from ..core import restart as core_restart
from ..core import start as core_start
from ..core import stop as core_stop
from ..core.envelope import Result, Status
from ..core.errors import CwcliError, ErrorKind
from ..utils.console import console, stderr_console
from . import start as start_cmd

DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - see the module docstring's "Binding" note
DEFAULT_INTERVAL = 2.5
KEEPALIVE_S = 15.0

# A read-only frontend, so the only failures are "cannot answer": map the core's
# closed error kinds onto the HTTP statuses that mean the same thing.
_HTTP_FOR_KIND = {
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.NOT_RUNNING: 409,
    ErrorKind.CONFLICT: 409,
    ErrorKind.PRECONDITION: 412,
    ErrorKind.USAGE: 400,
    ErrorKind.DOCKER: 503,
}
_MAX_ACTION_BODY = 64 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceRestartOutcome:
    """The narrow Console action result for stop-then-start instance restart."""

    project: str
    stopped: core_stop.StopOutcome
    started: core_start.StartOutcome | None


class _Hub:
    """Fan-out to every connected SSE client, one unbounded queue each.

    Unbounded is deliberate: a bounded queue would have to choose between
    blocking the probe thread on a stalled browser and silently dropping a delta,
    and a dropped delta is a UI that is quietly wrong. The FAST tier only speaks
    when state actually changes, so a queue that grows without bound means the
    client is stalled rather than ordinary steady-state traffic.
    """

    def __init__(self, on_focus) -> None:
        self._lock = threading.Lock()
        self._focus_lock = threading.Lock()
        self._clients: dict[int, tuple[queue.Queue, str | None]] = {}
        self._next = 0
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

    def publish(self, tier, project, state, cause) -> None:
        payload = {
            "tier": tier,
            "project": project,
            "instance": core_fleet.as_json(state) if state is not None else None,
            "removed": state is None,
        }
        if cause:
            payload["cause"] = cause
        frame = _sse_frame("delta", payload)
        with self._lock:
            queues = [q for q, _f in self._clients.values()]
        for q in queues:
            q.put(frame)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


def _sse_frame(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cwcli-serve"

    fleet: core_fleet.Fleet
    hub: _Hub

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook name
        """Silence per-request logging; a dashboard polls, and the noise buries the banner."""

    # ------------------------------------------------------------- responses

    def _send_json(self, payload: dict, status: int = 200, *, cors: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self._send_cors_headers(methods="GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self, *, methods: str) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", methods)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_html(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):  # noqa: N802 - stdlib hook name
        if urlparse(self.path).path == "/api/action":
            self.send_response(204)
            self.send_header("Allow", "POST, OPTIONS")
            self.end_headers()
            return
        self.send_response(204)
        self._send_cors_headers(methods="GET, OPTIONS")
        self.end_headers()

    # ---------------------------------------------------------------- routes

    def do_GET(self):  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(CONSOLE_PAGE)
        elif path == "/api/snapshot":
            self._send_json({"instances": self.fleet.snapshot()}, cors=True)
        elif path == "/api/events":
            focus = (parse_qs(parsed.query).get("focus") or [None])[0]
            self._stream_events(focus)
        elif path.startswith("/api/instance/") and path.endswith("/detail"):
            project = unquote(path[len("/api/instance/") : -len("/detail")])
            self._send_detail(project)
        else:
            self._send_json({"error": "not found"}, status=404, cors=True)

    def do_POST(self):  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self._send_json({"ok": False, "error": {"message": "not found"}}, status=404)
            return
        if not self._action_same_origin():
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "kind": ErrorKind.PRECONDITION.value,
                        "code": "action.origin_forbidden",
                        "message": "Console actions must be sent from this daemon's own page.",
                    },
                },
                status=403,
            )
            return
        if not self._action_json_content_type():
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "kind": ErrorKind.USAGE.value,
                        "code": "request.content_type_invalid",
                        "message": "Console action request body must be application/json.",
                    },
                },
                status=415,
            )
            return
        try:
            payload = self._read_action_body()
            status, body = self._dispatch_action(payload)
        except CwcliError as e:
            self._send_core_error(e)
            return
        except Exception as e:  # pragma: no cover - defensive HTTP boundary
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "kind": ErrorKind.INTERNAL.value,
                        "code": "action.internal_error",
                        "message": str(e),
                    },
                },
                status=500,
            )
            return
        self._send_json(body, status=status)

    def _action_same_origin(self) -> bool:
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if sec_fetch_site and sec_fetch_site not in {"same-origin", "none"}:
            return False

        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.netloc.lower() == (
            self.headers.get("Host") or ""
        ).lower()

    def _action_json_content_type(self) -> bool:
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return media_type == "application/json"

    def _read_action_body(self) -> dict:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError as e:
            raise CwcliError(
                ErrorKind.USAGE,
                "request.length_invalid",
                "Invalid Content-Length for action request.",
            ) from e
        if length > _MAX_ACTION_BODY:
            raise CwcliError(
                ErrorKind.USAGE,
                "request.too_large",
                "Action request body is too large.",
            )
        raw = self.rfile.read(length) if length else b"{}"
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

    def _dispatch_action(self, payload: dict) -> tuple[int, dict]:
        action = _required_str(payload, "action")
        project = _required_str(payload, "project")
        bench = _optional_str(payload.get("bench"))

        if action == "start_instance":
            _check_start_port_conflicts(project)
            result = core_start.start(project, bench=bench)
            self._refresh_lifecycle(project, result.warnings)
            return _result_response(action, project, result)
        if action == "stop_instance":
            result = core_stop.stop(project)
            self._refresh_lifecycle(project, result.warnings)
            return _result_response(action, project, result)
        if action == "restart_instance":
            return self._restart_instance(project, bench)
        if action == "restart_process":
            process = _required_str(payload, "process")
            result = core_restart.restart_process(project, process, bench=bench)
            self._refresh_process(project, result.warnings)
            return _result_response(action, project, result)

        raise CwcliError(
            ErrorKind.USAGE,
            "action.unsupported",
            f"Unsupported Console action '{action}'.",
            hint="Allowed actions are start_instance, stop_instance, restart_instance, restart_process.",
        )

    def _restart_instance(self, project: str, bench: str | None) -> tuple[int, dict]:
        _check_start_port_conflicts(project)
        bench_result = core_resolvers.resolve_bench(project, bench, None)
        if bench_result is None:
            bench_path = core_resolvers.DEFAULT_BENCH_PATH
            bench_warnings = [
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
        return (
            200,
            {
                "ok": True,
                "action": "restart_instance",
                "project": project,
                "status": Status.OK.value,
                "outcome": _plain(outcome),
                "warnings": [_plain(w) for w in warnings],
            },
        )

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
        self._send_json(
            {
                "ok": False,
                "error": {
                    "kind": e.kind.value,
                    "code": e.code,
                    "message": e.message,
                    "hint": e.hint,
                    "detail": e.detail,
                },
            },
            status=_HTTP_FOR_KIND.get(e.kind, 500),
        )

    def _send_detail(self, project: str) -> None:
        """The LAZY tier. Cache-backed, never auto-starts, freshness labels intact."""
        if not project:
            self._send_json({"error": "no project"}, status=400, cors=True)
            return
        try:
            report = core_inspect.inspect(project, refresh="auto", offer_choice=False).data
        except CwcliError as e:
            self._send_json(
                {"error": {"kind": e.kind.value, "code": e.code, "message": e.message}},
                status=_HTTP_FOR_KIND.get(e.kind, 500),
                cors=True,
            )
            return
        assert report is not None
        self._send_json(asdict(report), cors=True)

    def _stream_events(self, focus: str | None) -> None:
        client_id, q = self.hub.subscribe(focus)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # No Content-Length and no chunking: the body ends when the connection
            # does, which is what an infinite stream means over HTTP/1.1.
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(_sse_frame("snapshot", {"instances": self.fleet.snapshot()}))
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


def _required_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CwcliError(
            ErrorKind.USAGE,
            f"action.{key}_required",
            f"Console action requires a non-empty '{key}' string.",
        )
    return value.strip()


def _optional_str(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise CwcliError(
            ErrorKind.USAGE,
            "action.bench_invalid",
            "Console action 'bench' must be a string when present.",
        )
    return value.strip() or None


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _result_response(action: str, project: str, result: Result) -> tuple[int, dict]:
    if result.status is Status.NEEDS_CHOICE:
        return _choice_response(action, project, result)
    return (
        200,
        {
            "ok": True,
            "action": action,
            "project": project,
            "status": result.status.value,
            "outcome": _plain(result.data),
            "warnings": [_plain(w) for w in result.warnings],
        },
    )


def _choice_response(action: str, project: str, result: Result) -> tuple[int, dict]:
    choice = result.choice
    message = choice.prompt if choice is not None else "Console action requires a choice."
    code = choice.kind if choice is not None else "choice_required"
    return (
        409,
        {
            "ok": False,
            "action": action,
            "project": project,
            "error": {
                "kind": "needs_choice",
                "code": code,
                "message": message,
                "hint": "Select a single bench or process, then retry.",
            },
            "choice": _plain(choice) if choice is not None else None,
            "warnings": [_plain(w) for w in result.warnings],
        },
    )


def _check_start_port_conflicts(project: str) -> None:
    if start_cmd._frappe_running(project):
        return
    conflicting_projects, blocking_ports = start_cmd.detect_port_conflicts(project)
    if not conflicting_projects and not blocking_ports:
        return

    parts = []
    if conflicting_projects:
        parts.append(
            "Frappe instances already hold required ports: "
            + ", ".join(conflicting_projects)
            + "."
        )
    if blocking_ports:
        parts.append(
            "Host processes already hold required ports: "
            + ", ".join(str(p) for p in blocking_ports)
            + "."
        )
    raise CwcliError(
        ErrorKind.CONFLICT,
        "start.port_conflict",
        " ".join(parts),
        hint=(
            "Free those ports first. Use the terminal `cwcli start` command if you "
            "want interactive conflict resolution."
        ),
    )


def make_server(host: str, port: int, fleet: core_fleet.Fleet, hub: _Hub) -> ThreadingHTTPServer:
    """Bind the HTTP server, handing the handler class its fleet and hub."""
    handler = type("Handler", (_Handler,), {"fleet": fleet, "hub": hub})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


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


def serve(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Port to listen on."),
    host: str = typer.Option(
        DEFAULT_HOST,
        "--host",
        help="Address to bind. The 0.0.0.0 default is what lets a browser outside "
        "WSL reach the daemon; use 127.0.0.1 to keep it to this machine.",
    ),
    interval: float = typer.Option(
        DEFAULT_INTERVAL,
        "--interval",
        help="Seconds between FAST-tier health probes of each running instance.",
    ),
):
    """Serve the live fleet model over HTTP + SSE (foreground; Ctrl-C to stop)."""
    if interval <= 0:
        stderr_console.print("[red]--interval must be greater than 0.[/red]")
        raise typer.Exit(code=2)

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

    threads = [
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
    ]
    for t in threads:
        t.start()

    try:
        httpd = make_server(host, port, fleet, hub)
    except OSError as e:
        stderr_console.print(f"[red]Could not bind {host}:{port}: {e}[/red]")
        stop.set()
        raise typer.Exit(code=1) from e

    console.print(f"[bold]cwcli serve[/bold] on [cyan]http://{host}:{port}[/cyan]")
    console.print(f"  instances: {len(fleet.snapshot())}   probe interval: {interval}s")
    if host == "0.0.0.0":  # noqa: S104
        addr = _outbound_address()
        if addr:
            console.print(f"  reachable at [cyan]http://{addr}:{port}[/cyan]")
        if "microsoft" in platform.uname().release.lower():
            console.print(f"  from Windows: [cyan]http://localhost:{port}[/cyan] (WSL forwarding)")
    console.print("  Ctrl-C to stop")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\nstopping.")
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()


CONSOLE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cwcli Console</title>
<style>
:root {
  color-scheme: dark;
  --bg: #11110f;
  --panel: #181816;
  --panel-2: #20201d;
  --line: #33342f;
  --line-soft: #282923;
  --text: #eee9dd;
  --muted: #b3ad9e;
  --quiet: #827d72;
  --green: #80d89d;
  --green-bg: #153424;
  --amber: #f0b66d;
  --amber-bg: #3f2a16;
  --red: #ff8f78;
  --red-bg: #3d201a;
  --blue: #91c5ff;
  --blue-bg: #1b2d3f;
  --violet: #d2a7ff;
  --violet-bg: #32233f;
  --focus: #f6c05f;
  --shadow: 0 18px 50px rgba(0, 0, 0, 0.36);
}
* { box-sizing: border-box; }
html, body { min-height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
button, input { font: inherit; }
button { color: inherit; }
.app {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(260px, 320px) minmax(0, 1fr) minmax(260px, 300px);
  background:
    linear-gradient(180deg, rgba(246, 192, 95, 0.05), transparent 18rem),
    var(--bg);
}
.sidebar, .rail {
  min-width: 0;
  background: rgba(24, 24, 22, 0.96);
  border-color: var(--line);
  border-style: solid;
}
.sidebar {
  border-width: 0 1px 0 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
}
.rail {
  border-width: 0 0 0 1px;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}
.brand, .rail-head {
  padding: 18px 18px 14px;
  border-bottom: 1px solid var(--line-soft);
}
.brand h1, .rail-head h2 {
  margin: 0;
  font-size: 16px;
  line-height: 1.2;
  font-weight: 700;
  letter-spacing: 0;
}
.connection {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 9px;
  color: var(--muted);
  font-size: 12px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--quiet);
  flex: 0 0 auto;
}
.dot.open { background: var(--green); }
.dot.reconnecting { background: var(--amber); }
.tree {
  min-width: 0;
  overflow: auto;
  padding: 10px;
}
.tree-empty, .empty {
  color: var(--muted);
  padding: 18px;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
}
.instance-group { margin-bottom: 6px; }
.tree-line {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  align-items: stretch;
  gap: 2px;
}
.tree-row, .twisty, .tab, .action-button {
  border: 0;
  background: transparent;
}
.twisty {
  width: 24px;
  min-height: 34px;
  color: var(--quiet);
  border-radius: 6px;
  cursor: pointer;
}
.twisty:hover { background: var(--panel-2); color: var(--text); }
.tree-row {
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 8px 9px;
  border-radius: 7px;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.tree-row:hover { background: rgba(255, 255, 255, 0.04); }
.tree-row.selected {
  background: rgba(246, 192, 95, 0.13);
  box-shadow: inset 0 0 0 1px rgba(246, 192, 95, 0.45);
}
.tree-row:focus-visible, .tab:focus-visible, .action-button:focus-visible, .twisty:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.tree-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 650;
}
.tree-meta {
  min-width: 0;
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.children {
  margin-left: 28px;
  padding-left: 10px;
  border-left: 1px solid var(--line-soft);
}
.tree-row.bench, .tree-row.process {
  grid-template-columns: minmax(0, 1fr) auto;
  padding-top: 7px;
  padding-bottom: 7px;
}
.tree-row.process .tree-label {
  font-weight: 520;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.pill {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  min-height: 22px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}
.pill.running, .pill.up { color: var(--green); background: var(--green-bg); }
.pill.degraded, .pill.down { color: var(--red); background: var(--red-bg); }
.pill.offline { color: var(--muted); background: #292925; }
.pill.online { color: var(--blue); background: var(--blue-bg); }
.pill.unknown { color: var(--violet); background: var(--violet-bg); }
.pill.neutral { color: var(--muted); background: #282923; }
.main {
  min-width: 0;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
}
.topbar {
  min-width: 0;
  padding: 20px 24px 16px;
  border-bottom: 1px solid var(--line-soft);
  background: rgba(17, 17, 15, 0.9);
}
.crumb {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.title-row {
  display: flex;
  gap: 12px;
  align-items: center;
  min-width: 0;
}
.title-row h2 {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 28px;
  line-height: 1.1;
  letter-spacing: 0;
}
.subtitle {
  margin-top: 8px;
  color: var(--muted);
  max-width: 78ch;
}
.tabs {
  display: flex;
  gap: 2px;
  padding: 10px 24px 0;
  border-bottom: 1px solid var(--line-soft);
  overflow-x: auto;
}
.tab {
  padding: 10px 12px;
  color: var(--muted);
  border-radius: 7px 7px 0 0;
  cursor: pointer;
  white-space: nowrap;
}
.tab.active {
  color: var(--text);
  background: var(--panel);
  box-shadow: inset 0 -2px 0 var(--focus);
}
.tab small {
  color: var(--quiet);
  margin-left: 4px;
  font-size: 11px;
}
.detail {
  min-width: 0;
  overflow: auto;
  padding: 22px 24px 28px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.metric {
  min-width: 0;
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  padding: 13px;
}
.metric .label {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 5px;
}
.metric .value {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 18px;
  font-weight: 700;
}
.section {
  margin-top: 22px;
}
.section h3 {
  margin: 0 0 10px;
  font-size: 15px;
  letter-spacing: 0;
}
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: var(--panel);
}
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 620px;
}
th, td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line-soft);
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
tr:last-child td { border-bottom: 0; }
td.code, .code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.muted { color: var(--muted); }
.unknown-text { color: var(--violet); }
.site-apps {
  display: grid;
  gap: 12px;
}
.inventory-block {
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: var(--panel);
  padding: 14px;
  min-width: 0;
}
.inventory-title {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
  margin-bottom: 10px;
  font-weight: 700;
}
.inventory-title span:first-child {
  min-width: 0;
  overflow-wrap: anywhere;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  max-width: 100%;
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 8px;
  color: var(--text);
  background: rgba(255, 255, 255, 0.03);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.rail-body {
  min-width: 0;
  overflow: auto;
  padding: 16px;
}
.target {
  min-width: 0;
  padding: 13px;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
}
.target-name {
  min-width: 0;
  overflow-wrap: anywhere;
  font-weight: 700;
}
.target-meta {
  margin-top: 5px;
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.action-group {
  margin-top: 18px;
}
.action-group h3 {
  margin: 0 0 8px;
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.action-button {
  width: 100%;
  min-height: 40px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-2);
  cursor: pointer;
}
.action-button:hover:not(:disabled) {
  border-color: rgba(246, 192, 95, 0.65);
  background: #282821;
}
.action-button:disabled {
  color: var(--quiet);
  cursor: not-allowed;
  background: #171714;
}
.action-cost {
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.event-log {
  padding: 12px;
  border-top: 1px solid var(--line-soft);
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  max-height: 180px;
  overflow: auto;
}
.event-log div { margin-bottom: 5px; overflow-wrap: anywhere; }
.event-log .instant { color: var(--blue); }
.event-log .fast { color: var(--green); }
.event-log .error { color: var(--red); }
.notice {
  margin-top: 12px;
  padding: 10px;
  border-radius: 8px;
  border: 1px solid var(--line-soft);
  color: var(--muted);
  background: rgba(255, 255, 255, 0.025);
  overflow-wrap: anywhere;
}
.notice.error {
  color: var(--red);
  border-color: rgba(255, 143, 120, 0.45);
  background: rgba(255, 143, 120, 0.08);
}
@media (max-width: 1050px) {
  .app {
    grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
    grid-template-areas:
      "sidebar main"
      "rail rail";
  }
  .sidebar { grid-area: sidebar; }
  .main { grid-area: main; }
  .rail {
    grid-area: rail;
    border-width: 1px 0 0;
  }
  .rail-body {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, 320px);
    gap: 16px;
  }
}
@media (max-width: 760px) {
  .app {
    display: block;
  }
  .sidebar, .rail {
    border-width: 0 0 1px;
  }
  .tree {
    max-height: 42vh;
  }
  .topbar, .detail {
    padding-left: 16px;
    padding-right: 16px;
  }
  .tabs {
    padding-left: 16px;
    padding-right: 16px;
  }
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .rail-body {
    display: block;
  }
}
@media (max-width: 460px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }
  .title-row h2 {
    font-size: 22px;
  }
}
</style>
</head>
<body>
<div class="app" data-app="cw-console">
  <aside class="sidebar" aria-label="Fleet tree">
    <div class="brand">
      <h1>cwcli Console</h1>
      <div class="connection"><span id="conn-dot" class="dot"></span><span id="conn-text">connecting</span></div>
    </div>
    <nav id="tree" class="tree"></nav>
    <div id="event-log" class="event-log" aria-live="polite"></div>
  </aside>
  <main class="main">
    <header id="topbar" class="topbar"></header>
    <div id="tabs" class="tabs" role="tablist"></div>
    <section id="detail" class="detail"></section>
  </main>
  <aside class="rail" aria-label="Actions">
    <div class="rail-head"><h2>Actions</h2></div>
    <div id="rail-body" class="rail-body"></div>
  </aside>
</div>
<script>
(() => {
  "use strict";

  const model = new Map();
  const detailCache = new Map();
  const expanded = new Set();
  const eventRows = [];
  const tabs = ["processes", "sites", "apps"];
  const initialFocus = new URLSearchParams(location.search).get("focus");
  let selected = initialFocus ? {type: "instance", project: initialFocus} : {type: "empty"};
  let activeTab = "processes";
  let source = null;
  let activeFocus = null;
  let pendingAction = null;
  let lastAction = null;

  const el = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[ch]);
  const unknown = text => `<span class="unknown-text">${esc(text || "unknown")}</span>`;
  const safeKey = value => encodeURIComponent(String(value ?? ""));
  const parseKey = value => decodeURIComponent(value || "");

  function statusText(token) {
    return ({
      running: "running",
      degraded: "degraded",
      offline: "offline",
      online: "never started",
      unknown: "unknown"
    })[token] || "unknown";
  }

  function statusPill(token) {
    const safe = ["running", "degraded", "offline", "online", "unknown"].includes(token) ? token : "unknown";
    return `<span class="pill ${safe}">${esc(statusText(token))}</span>`;
  }

  function processPill(process) {
    if (!process) return `<span class="pill unknown">unknown</span>`;
    if (process.state && process.state !== "RUNNING" && process.state !== "STARTING") {
      return `<span class="pill down">${esc(process.state)}</span>`;
    }
    if (process.up) return `<span class="pill up">${esc(process.state || "up")}</span>`;
    return `<span class="pill down">${esc(process.state || "down")}</span>`;
  }

  function sortedInstances() {
    return [...model.values()].sort((a, b) => a.project.localeCompare(b.project));
  }

  function benchKey(bench) {
    if (!bench) return "";
    return bench.index === null || bench.index === undefined
      ? `path:${bench.bench_path}`
      : `index:${bench.index}`;
  }

  function benchSelector(bench) {
    if (!bench) return null;
    if (bench.label) return bench.label;
    if (bench.index !== null && bench.index !== undefined) return String(bench.index);
    return null;
  }

  function selectedInstance() {
    return selected.project ? model.get(selected.project) || null : null;
  }

  function selectedBench() {
    const inst = selectedInstance();
    if (!inst || !selected.benchKey) return null;
    return inst.benches.find(b => benchKey(b) === selected.benchKey) || null;
  }

  function selectedProcess() {
    const bench = selectedBench();
    if (!bench || !selected.process) return null;
    return bench.processes.find(p => p.label === selected.process) || null;
  }

  function select(next) {
    selected = next;
    activeTab = "processes";
    if (selected.project) {
      expanded.add(selected.project);
      ensureDetail(selected.project);
    }
    render();
    syncFocus();
  }

  function ensureSelection() {
    const instances = sortedInstances();
    if (!instances.length) {
      selected = {type: "empty"};
      return;
    }
    if (!selected.project || !model.has(selected.project)) {
      selected = {type: "instance", project: instances[0].project};
      expanded.add(instances[0].project);
      ensureDetail(instances[0].project);
      return;
    }
    const inst = selectedInstance();
    if (selected.benchKey && !inst.benches.some(b => benchKey(b) === selected.benchKey)) {
      selected = {type: "instance", project: selected.project};
    }
  }

  function syncFocus() {
    const focus = selected.project || "";
    if (focus === activeFocus && source) return;
    activeFocus = focus;
    connectEvents();
  }

  function connectEvents() {
    if (source) source.close();
    const url = "/api/events" + (activeFocus ? `?focus=${encodeURIComponent(activeFocus)}` : "");
    source = new EventSource(url);
    setConnection("connecting");
    source.addEventListener("open", () => setConnection("open"));
    source.addEventListener("snapshot", event => {
      model.clear();
      JSON.parse(event.data).instances.forEach(instance => {
        model.set(instance.project, instance);
        if (!expanded.has(instance.project)) expanded.add(instance.project);
      });
      ensureSelection();
      addEvent("instant", `SNAPSHOT ${model.size} instances`);
      setConnection("open");
      render();
      syncFocus();
    });
    source.addEventListener("delta", event => {
      const delta = JSON.parse(event.data);
      if (delta.removed) {
        model.delete(delta.project);
        detailCache.delete(delta.project);
        addEvent(delta.tier, `${delta.tier.toUpperCase()} ${delta.project} removed`);
      } else {
        model.set(delta.project, delta.instance);
        const cause = delta.cause ? ` ${delta.cause.action}(${delta.cause.service || "container"})` : "";
        addEvent(delta.tier, `${delta.tier.toUpperCase()} ${delta.project}${cause} -> ${delta.instance.overall}`);
      }
      ensureSelection();
      render();
    });
    source.onerror = () => setConnection("reconnecting");
  }

  function setConnection(state) {
    el("conn-text").textContent = activeFocus ? `${state} - focus ${activeFocus}` : state;
    el("conn-dot").className = `dot ${state === "open" ? "open" : "reconnecting"}`;
  }

  function addEvent(kind, text) {
    if (eventRows[0] && eventRows[0].kind === kind && eventRows[0].text === text) return;
    eventRows.unshift({kind, text, at: new Date().toLocaleTimeString()});
    eventRows.splice(60);
    renderEvents();
  }

  function renderEvents() {
    el("event-log").innerHTML = eventRows.length
      ? eventRows.map(row => `<div class="${esc(row.kind)}">${esc(row.at)} ${esc(row.text)}</div>`).join("")
      : `<div class="muted">No stream events yet</div>`;
  }

  function ensureDetail(project) {
    const cached = detailCache.get(project);
    if (cached && cached.state !== "error") return;
    detailCache.set(project, {state: "loading"});
    fetch(`/api/instance/${encodeURIComponent(project)}/detail`)
      .then(async response => {
        const body = await response.json();
        if (!response.ok) throw body;
        detailCache.set(project, {state: "ready", data: body});
        render();
      })
      .catch(error => {
        const message = error && error.error ? error.error.message : "could not find out";
        detailCache.set(project, {state: "error", message});
        render();
      });
  }

  function currentDetail() {
    return selected.project ? detailCache.get(selected.project) || null : null;
  }

  function render() {
    ensureSelection();
    renderTree();
    renderTopbar();
    renderTabs();
    renderDetail();
    renderRail();
    renderEvents();
  }

  function renderTree() {
    const instances = sortedInstances();
    if (!instances.length) {
      el("tree").innerHTML = `<div class="tree-empty">No instances found</div>`;
      return;
    }
    el("tree").innerHTML = instances.map(instance => {
      const open = expanded.has(instance.project);
      const instanceSelected = selected.type === "instance" && selected.project === instance.project;
      const benches = open ? instance.benches.map(bench => renderBenchNode(instance, bench)).join("") : "";
      const pending = instance.overall === "unknown" ? " - could not find out yet" : "";
      return `<div class="instance-group">
        <div class="tree-line">
          <button class="twisty" data-toggle="${safeKey(instance.project)}" aria-label="${open ? "Collapse" : "Expand"} ${esc(instance.project)}">${open ? "v" : ">"}</button>
          <button class="tree-row ${instanceSelected ? "selected" : ""}" data-select="instance" data-project="${safeKey(instance.project)}">
            <span class="tree-label">${esc(instance.project)}</span>
            ${statusPill(instance.overall)}
            <span class="tree-meta">${esc(instance.docker_status)}${esc(pending)}</span>
          </button>
        </div>
        ${open ? `<div class="children">${benches || renderNoBenchNode(instance)}</div>` : ""}
      </div>`;
    }).join("");
  }

  function renderBenchNode(instance, bench) {
    const key = benchKey(bench);
    const selectedRow = selected.project === instance.project && selected.benchKey === key && selected.type === "bench";
    const meta = bench.not_cwcli_supervised ? "not cwcli supervised" : webSummary(bench, instance.web_probed);
    const processes = (bench.processes || []).map(process => renderProcessNode(instance, bench, process)).join("");
    return `<div>
      <button class="tree-row bench ${selectedRow ? "selected" : ""}" data-select="bench" data-project="${safeKey(instance.project)}" data-bench="${safeKey(key)}">
        <span>
          <span class="tree-label">${esc(bench.label || bench.bench_path)}</span>
          <span class="tree-meta">${esc(meta)}</span>
        </span>
        ${statusPill(bench.overall)}
      </button>
      <div class="children">${processes || `<div class="tree-meta">processes unknown</div>`}</div>
    </div>`;
  }

  function renderProcessNode(instance, bench, process) {
    const selectedRow = selected.project === instance.project
      && selected.benchKey === benchKey(bench)
      && selected.process === process.label
      && selected.type === "process";
    return `<button class="tree-row process ${selectedRow ? "selected" : ""}" data-select="process" data-project="${safeKey(instance.project)}" data-bench="${safeKey(benchKey(bench))}" data-process="${safeKey(process.label)}">
      <span>
        <span class="tree-label">PROCESS ${esc(process.label)}</span>
        <span class="tree-meta">${esc(process.pid == null ? "pid unknown" : "pid " + process.pid)}</span>
      </span>
      ${processPill(process)}
    </button>`;
  }

  function renderNoBenchNode(instance) {
    const text = instance.container_running ? "health unknown - no bench probed" : "instance offline";
    return `<div class="tree-meta">${esc(text)}</div>`;
  }

  function renderTopbar() {
    const inst = selectedInstance();
    if (!inst) {
      el("topbar").innerHTML = `<div class="crumb">fleet</div><div class="title-row"><h2>No instance selected</h2>${statusPill("unknown")}</div>`;
      return;
    }
    const bench = selectedBench();
    const process = selectedProcess();
    let title = inst.project;
    let crumb = "fleet / " + inst.project;
    let token = inst.overall;
    let subtitle = instanceSubtitle(inst);
    if (bench) {
      title = bench.label || bench.bench_path;
      crumb += " / " + title;
      token = bench.overall;
      subtitle = benchSubtitle(bench, inst);
    }
    if (process) {
      title = "PROCESS " + process.label;
      crumb += " / PROCESS " + process.label;
      subtitle = processSubtitle(process, bench);
    }
    el("topbar").innerHTML = `<div class="crumb">${esc(crumb)}</div>
      <div class="title-row"><h2>${esc(title)}</h2>${process ? processPill(process) : statusPill(token)}</div>
      <div class="subtitle">${subtitle}</div>`;
  }

  function instanceSubtitle(instance) {
    const bits = [
      `Docker ${instance.docker_status}`,
      instance.container_running ? "container running" : "container stopped",
      instance.probe_ms == null ? "probe never completed" : `last probe ${Math.round(instance.probe_ms)}ms`
    ];
    if (instance.probe_error) bits.push("probe error: " + instance.probe_error);
    return bits.map(esc).join(" · ");
  }

  function benchSubtitle(bench, instance) {
    const bits = [benchStatusSentence(bench), webSummary(bench, instance.web_probed)];
    if (bench.not_cwcli_supervised) bits.push("not cwcli supervised");
    return bits.map(esc).join(" · ");
  }

  function processSubtitle(process, bench) {
    const state = process.state || (bench && bench.not_cwcli_supervised ? "supervisord state unknown" : "state unknown");
    const pid = process.pid == null ? "pid unknown" : "pid " + process.pid;
    return [state, pid, process.up ? "process up" : "process down"].map(esc).join(" · ");
  }

  function benchStatusSentence(bench) {
    if (bench.overall === "online") return "never started";
    if (bench.overall === "unknown") return "could not find out";
    return bench.overall;
  }

  function webSummary(bench, instanceWebProbed) {
    if (!bench.web_port_verified) return "port unknown, not probed";
    if (bench.web_port === null || bench.web_port === undefined) return "port unknown, not probed";
    if (!instanceWebProbed) return `:${bench.web_port} not probed`;
    if (bench.web_http_code === null || bench.web_http_code === undefined) {
      return `${bench.web_site || "site unknown"}:${bench.web_port} -> no answer`;
    }
    return `${bench.web_site || "site unknown"}:${bench.web_port} -> ${bench.web_http_code}`;
  }

  function renderTabs() {
    el("tabs").innerHTML = tabs.map(tab => {
      const label = tab === "sites" ? "Sites <small>cached</small>" : tab === "apps" ? "Apps <small>cached</small>" : "Processes";
      return `<button class="tab ${activeTab === tab ? "active" : ""}" role="tab" data-tab="${tab}">${label}</button>`;
    }).join("");
  }

  function renderDetail() {
    if (activeTab === "sites") {
      renderSites();
    } else if (activeTab === "apps") {
      renderApps();
    } else {
      renderProcesses();
    }
  }

  function renderProcesses() {
    const inst = selectedInstance();
    if (!inst) {
      el("detail").innerHTML = `<div class="empty">No live fleet data</div>`;
      return;
    }
    const benches = selectedBench() ? [selectedBench()] : inst.benches;
    const process = selectedProcess();
    const summary = `<div class="summary-grid">
      ${metric("Instance", inst.project)}
      ${metric("Health", statusText(inst.overall))}
      ${metric("Docker", inst.docker_status)}
      ${metric("Probe", inst.probe_ms == null ? "never completed" : Math.round(inst.probe_ms) + "ms")}
    </div>`;
    if (process) {
      el("detail").innerHTML = summary + renderProcessCard(process, selectedBench());
      return;
    }
    if (!benches.length) {
      el("detail").innerHTML = summary + `<div class="empty">${inst.container_running ? "Health unknown - no bench probed yet" : "Instance offline"}</div>`;
      return;
    }
    const rows = benches.flatMap(bench => (bench.processes || []).map(process => ({bench, process})));
    if (!rows.length) {
      el("detail").innerHTML = summary + `<div class="empty">Process state unknown - could not find out</div>`;
      return;
    }
    el("detail").innerHTML = summary + `<div class="section"><h3>Processes</h3><div class="table-wrap"><table>
      <thead><tr><th>Bench</th><th>Process</th><th>State</th><th>PID</th><th>CPU</th><th>Memory</th><th>Uptime</th></tr></thead>
      <tbody>${rows.map(({bench, process}) => `<tr>
        <td class="code">${esc(bench.label || bench.bench_path)}</td>
        <td class="code">PROCESS ${esc(process.label)}</td>
        <td>${processPill(process)}</td>
        <td>${process.pid == null ? unknown("unknown") : esc(process.pid)}</td>
        <td>${process.cpu_pct == null ? unknown("unknown") : esc(process.cpu_pct + "%")}</td>
        <td>${process.rss_kb == null ? unknown("unknown") : esc(Math.round(process.rss_kb / 1024) + " MB")}</td>
        <td>${process.uptime_s == null ? unknown("unknown") : esc(formatDuration(process.uptime_s))}</td>
      </tr>`).join("")}</tbody>
    </table></div></div>`;
  }

  function renderProcessCard(process, bench) {
    return `<div class="summary-grid">
      ${metric("Process", "PROCESS " + process.label)}
      ${metric("State", process.state || "unknown")}
      ${metric("PID", process.pid == null ? "unknown" : process.pid)}
      ${metric("Bench", bench ? (bench.label || bench.bench_path) : "unknown")}
    </div>`;
  }

  function metric(label, value) {
    const text = value === null || value === undefined || value === "" ? "unknown" : String(value);
    return `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(text)}</div></div>`;
  }

  function formatDuration(seconds) {
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }

  function detailBenches() {
    const detail = currentDetail();
    if (!detail || detail.state !== "ready") return [];
    if (!selected.benchKey) return detail.data.benches || [];
    return (detail.data.benches || []).filter(bench => {
      const key = bench.index === null || bench.index === undefined ? `path:${bench.path}` : `index:${bench.index}`;
      return key === selected.benchKey;
    });
  }

  function renderDetailState(kind) {
    const detail = currentDetail();
    if (!selected.project) return `<div class="empty">No instance selected</div>`;
    if (!detail || detail.state === "loading") return `<div class="empty">Loading cached ${kind}</div>`;
    if (detail.state === "error") return `<div class="empty">${esc(detail.message || "could not find out")}</div>`;
    return null;
  }

  function renderSites() {
    const state = renderDetailState("sites");
    if (state) {
      el("detail").innerHTML = state;
      return;
    }
    const detail = currentDetail().data;
    const benches = detailBenches();
    const freshness = metric("Inspect freshness", detail.served_from || "unknown");
    const blocks = benches.map(bench => {
      const sites = bench.sites || [];
      return `<div class="inventory-block">
        <div class="inventory-title"><span>${esc(bench.label || bench.path)}</span><span class="pill neutral">cached</span></div>
        ${sites.length ? `<div class="table-wrap"><table><thead><tr><th>Site</th><th>Default</th><th>Config</th><th>Apps</th></tr></thead><tbody>
          ${sites.map(site => `<tr>
            <td class="code">${esc(site.name)}</td>
            <td>${site.name === bench.default_site ? "default" : `<span class="muted">not default</span>`}</td>
            <td>${site.has_site_config ? "present" : unknown("unknown")}</td>
            <td>${site.installed_apps_verified ? "verified" : unknown("remembered - could not verify")}</td>
          </tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">No sites in cached inspect result</div>`}
      </div>`;
    }).join("");
    el("detail").innerHTML = `<div class="summary-grid">${freshness}${metric("Project", detail.project)}${metric("Benches", benches.length)}${metric("State", detail.degraded ? "degraded" : "cached")}</div>
      <div class="site-apps">${blocks || `<div class="empty">No cached bench detail</div>`}</div>`;
  }

  function renderApps() {
    const state = renderDetailState("apps");
    if (state) {
      el("detail").innerHTML = state;
      return;
    }
    const detail = currentDetail().data;
    const benches = detailBenches();
    const blocks = benches.map(bench => {
      const available = bench.available_apps || [];
      const sites = bench.sites || [];
      const installedRows = sites.flatMap(site => (site.installed_apps || []).map(app => ({site, app})));
      return `<div class="inventory-block">
        <div class="inventory-title"><span>${esc(bench.label || bench.path)}</span><span class="pill neutral">cached</span></div>
        <div class="section"><h3>Available apps</h3><div class="chips">${available.length ? available.map(app => `<span class="chip">${esc(app)}</span>`).join("") : unknown("could not find out")}</div></div>
        <div class="section"><h3>Installed apps</h3>
          ${installedRows.length ? `<div class="table-wrap"><table><thead><tr><th>Site</th><th>App</th><th>Verification</th></tr></thead><tbody>
            ${installedRows.map(row => `<tr>
              <td class="code">${esc(row.site.name)}</td>
              <td class="code">${esc(row.app)}</td>
              <td>${row.site.installed_apps_verified ? "verified" : unknown("remembered - could not verify")}</td>
            </tr>`).join("")}
          </tbody></table></div>` : `<div class="empty">No installed apps in cached inspect result</div>`}
        </div>
      </div>`;
    }).join("");
    el("detail").innerHTML = `<div class="summary-grid">${metric("Inspect freshness", detail.served_from || "unknown")}${metric("Project", detail.project)}${metric("Benches", benches.length)}${metric("Apps", countApps(benches))}</div>
      <div class="site-apps">${blocks || `<div class="empty">No cached app detail</div>`}</div>`;
  }

  function countApps(benches) {
    const names = new Set();
    benches.forEach(bench => {
      (bench.available_apps || []).forEach(name => names.add(name));
      (bench.sites || []).forEach(site => (site.installed_apps || []).forEach(name => names.add(name)));
    });
    return names.size;
  }

  function renderRail() {
    const inst = selectedInstance();
    if (!inst) {
      el("rail-body").innerHTML = `<div class="target"><div class="target-name">No target</div><div class="target-meta">unknown</div></div>`;
      return;
    }
    const bench = selectedBench();
    const process = selectedProcess();
    const targetName = process ? `PROCESS ${process.label}` : bench ? (bench.label || bench.bench_path) : inst.project;
    const targetMeta = process ? `${inst.project} / ${bench ? bench.bench_path : "bench unknown"}` : bench ? inst.project : "instance";
    const busy = pendingAction !== null;
    const processDisabled = busy || !process;
    const message = lastAction ? `<div class="notice ${lastAction.ok ? "" : "error"}">${esc(lastAction.text)}</div>` : "";
    el("rail-body").innerHTML = `<div>
      <div class="target"><div class="target-name">${esc(targetName)}</div><div class="target-meta">${esc(targetMeta)}</div></div>
      <div class="action-group">
        <h3>Instance</h3>
        ${actionButton("start_instance", "Start instance", "starts bench", busy)}
        ${actionButton("stop_instance", "Stop instance", "stops containers", busy)}
        ${actionButton("restart_instance", "Restart instance", "stop then start", busy)}
      </div>
      <div class="action-group">
        <h3>Process</h3>
        ${actionButton("restart_process", "Restart process", process ? "one process" : "select process", processDisabled)}
      </div>
      ${message}
    </div>`;
  }

  function actionButton(action, label, cost, disabled) {
    const busy = pendingAction === action ? "working" : cost;
    return `<button class="action-button" data-action="${action}" ${disabled ? "disabled" : ""}>
      <span>${esc(label)}</span><span class="action-cost">${esc(busy)}</span>
    </button>`;
  }

  async function runAction(action) {
    const inst = selectedInstance();
    if (!inst || pendingAction) return;
    const bench = selectedBench();
    const process = selectedProcess();
    const payload = {action, project: inst.project};
    const selector = benchSelector(bench);
    if (selector && (action === "start_instance" || action === "restart_instance" || action === "restart_process")) {
      payload.bench = selector;
    }
    if (action === "restart_process") {
      if (!process) return;
      payload.process = process.label;
    }
    pendingAction = action;
    lastAction = null;
    renderRail();
    try {
      const response = await fetch("/api/action", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const body = await response.json();
      if (!response.ok || !body.ok) {
        const error = body.error || {};
        const choice = body.choice && body.choice.options ? " Options: " + body.choice.options.map(o => o.label || o.value).join(", ") : "";
        throw new Error((error.message || "action failed") + choice);
      }
      lastAction = {ok: true, text: `${action.replaceAll("_", " ")} accepted for ${inst.project}`};
      addEvent("fast", `ACTION ${action.replaceAll("_", " ")} ${inst.project}`);
    } catch (error) {
      lastAction = {ok: false, text: error.message || "action failed"};
      addEvent("error", `ACTION ${action} failed`);
    } finally {
      pendingAction = null;
      renderRail();
    }
  }

  el("tree").addEventListener("click", event => {
    const toggle = event.target.closest("[data-toggle]");
    if (toggle) {
      const project = parseKey(toggle.dataset.toggle);
      if (expanded.has(project)) expanded.delete(project);
      else expanded.add(project);
      renderTree();
      return;
    }
    const row = event.target.closest("[data-select]");
    if (!row) return;
    const project = parseKey(row.dataset.project);
    const type = row.dataset.select;
    if (type === "instance") select({type, project});
    if (type === "bench") select({type, project, benchKey: parseKey(row.dataset.bench)});
    if (type === "process") {
      select({
        type,
        project,
        benchKey: parseKey(row.dataset.bench),
        process: parseKey(row.dataset.process)
      });
    }
  });

  el("tabs").addEventListener("click", event => {
    const tab = event.target.closest("[data-tab]");
    if (!tab) return;
    activeTab = tab.dataset.tab;
    if (selected.project) ensureDetail(selected.project);
    render();
  });

  el("rail-body").addEventListener("click", event => {
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    runAction(button.dataset.action);
  });

  renderEvents();
  connectEvents();
})();
</script>
</body>
</html>
"""
