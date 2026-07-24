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
from importlib.resources import files
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
_ACTION_LOCK_STRIPES = 64
CONSOLE_PAGE = (
    files("caffeinated_whale_cli.commands").joinpath("console.html").read_text(encoding="utf-8")
)


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
    action_locks: tuple[threading.Lock, ...]

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
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
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
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == (self.headers.get("Host") or "").lower()
        )

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
        allowed = {
            "start_instance",
            "stop_instance",
            "restart_instance",
            "restart_process",
        }
        if action not in allowed:
            raise CwcliError(
                ErrorKind.USAGE,
                "action.unsupported",
                f"Unsupported Console action '{action}'.",
                hint=(
                    "Allowed actions are start_instance, stop_instance, "
                    "restart_instance, restart_process."
                ),
            )
        process = _required_str(payload, "process") if action == "restart_process" else None

        with self._action_lock(project):
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

            assert process is not None
            result = core_restart.restart_process(project, process, bench=bench)
            self._refresh_process(project, result.warnings)
            return _result_response(action, project, result)

    def _action_lock(self, project: str) -> threading.Lock:
        return self.action_locks[hash(project) % len(self.action_locks)]

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
            "Frappe instances already hold required ports: " + ", ".join(conflicting_projects) + "."
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
    handler = type(
        "Handler",
        (_Handler,),
        {
            "fleet": fleet,
            "hub": hub,
            "action_locks": tuple(threading.Lock() for _ in range(_ACTION_LOCK_STRIPES)),
        },
    )
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
