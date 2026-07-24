"""``cwcli serve`` - the streaming Console daemon, a third frontend over the core.

A FOREGROUND command started by hand and stopped with Ctrl-C. There is no
auto-start, no background service, no boot unit: a dashboard nobody is looking at
should not be probing anyone's Docker daemon.

Stdlib only (``http.server.ThreadingHTTPServer`` + ``queue`` + ``threading``) over
``docker``, which is already a runtime dependency - no web framework, no asyncio,
no new dependency. Browser-native SSE over plain HTTP is what keeps this
cross-platform down to "any browser", including the case this was built for: a
**Windows** browser reaching a daemon bound inside **WSL**.

Endpoints (all read-only; this frontend can start, stop, or delete nothing):

* ``GET /``                            - a throwaway test page with an EventSource
* ``GET /api/snapshot``                - the whole fleet model as JSON
* ``GET /api/events[?focus=<project>]``- SSE: one ``snapshot`` event, then
  ``delta`` events tagged ``tier: instant|fast``
* ``GET /api/instance/<project>/detail``- the LAZY tier: cache-backed
  ``core.inspect``, carrying its ``served_from`` and ``installed_apps_verified``
  freshness labels through unchanged

``?focus=<project>`` is how the browser says which instance it currently has
open, and it is the whole mechanism behind the web-probe cadence: the connection
itself carries the answer, so a closed tab retracts focus with no heartbeat, no
timeout, and no extra endpoint. See ``core.fleet.Fleet.set_focus``.

**Binding.** The default is ``0.0.0.0`` because the primary environment is WSL
and a Windows browser cannot reach a WSL-only ``127.0.0.1`` listener. Every
endpoint is a read, but the fleet model does name projects, ports and sites, so
``--host 127.0.0.1`` is there for anyone on an untrusted network.
"""

from __future__ import annotations

import json
import platform
import queue
import socket
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import typer

from ..core import fleet as core_fleet
from ..core import inspect as core_inspect
from ..core.errors import CwcliError, ErrorKind
from ..utils.console import console, stderr_console

DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - see the module docstring's "Binding" note
DEFAULT_INTERVAL = 2.5
KEEPALIVE_S = 15.0

# A read-only frontend, so the only failures are "cannot answer": map the core's
# closed error kinds onto the HTTP statuses that mean the same thing.
_HTTP_FOR_KIND = {
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.NOT_RUNNING: 409,
    ErrorKind.USAGE: 400,
    ErrorKind.DOCKER: 503,
}


class _Hub:
    """Fan-out to every connected SSE client, one unbounded queue each.

    Unbounded is deliberate: a bounded queue would have to choose between
    blocking the probe thread on a stalled browser and silently dropping a delta,
    and a dropped delta is a UI that is quietly wrong. The FAST tier only speaks
    when state actually changes, so a queue that grows without bound means the
    client is gone - which the write itself then discovers.
    """

    def __init__(self, on_focus) -> None:
        self._lock = threading.Lock()
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
            self._clients.pop(client_id, None)
        self._sync_focus()

    def _sync_focus(self) -> None:
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

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # ponytail: open CORS because every endpoint is a read and the phase-3 UI
        # may well be served from a dev server on another port. Tighten if a
        # mutating endpoint is ever added here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---------------------------------------------------------------- routes

    def do_GET(self):  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(TEST_PAGE)
        elif path == "/api/snapshot":
            self._send_json({"instances": self.fleet.snapshot()})
        elif path == "/api/events":
            focus = (parse_qs(parsed.query).get("focus") or [None])[0]
            self._stream_events(focus)
        elif path.startswith("/api/instance/") and path.endswith("/detail"):
            project = unquote(path[len("/api/instance/") : -len("/detail")])
            self._send_detail(project)
        else:
            self._send_json({"error": "not found"}, status=404)

    def _send_detail(self, project: str) -> None:
        """The LAZY tier. Cache-backed, never auto-starts, freshness labels intact."""
        if not project:
            self._send_json({"error": "no project"}, status=400)
            return
        try:
            report = core_inspect.inspect(project, refresh="auto", offer_choice=False).data
        except CwcliError as e:
            self._send_json(
                {"error": {"kind": e.kind.value, "code": e.code, "message": e.message}},
                status=_HTTP_FOR_KIND.get(e.kind, 500),
            )
            return
        assert report is not None
        self._send_json(asdict(report))

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


TEST_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>cwcli serve</title>
<style>
 body{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;background:#12141a;color:#d8dee9;margin:0;padding:20px}
 h1{font-size:15px;margin:0 0 4px}
 .sub{color:#7a8494;margin-bottom:16px}
 table{border-collapse:collapse;width:100%;margin-bottom:20px}
 th,td{text-align:left;padding:4px 10px;border-bottom:1px solid #232733}
 th{color:#7a8494;font-weight:400}
 .pill{padding:1px 7px;border-radius:9px;font-size:11px}
 .running{background:#1c3a2a;color:#7ee2a8}
 .degraded{background:#4a2a1c;color:#f0a868}
 .offline{background:#2a2d36;color:#8b93a3}
 .online{background:#1c2f4a;color:#78b4f0}
 .unknown{background:#3a2a4a;color:#c79ae8}
 #log{white-space:pre-wrap;color:#8b93a3;max-height:40vh;overflow:auto}
 .instant{color:#78b4f0}.fast{color:#7ee2a8}
</style></head><body>
<h1>cwcli serve <span id="state">connecting</span></h1>
<div class="sub">throwaway test page - the Console UI is phase 3</div>
<table><thead><tr><th>instance</th><th>docker</th><th>health</th><th>web</th>
<th>processes</th><th>probe</th></tr></thead><tbody id="rows"></tbody></table>
<div id="log"></div>
<script>
const model = new Map();
const focus = new URLSearchParams(location.search).get('focus');
const es = new EventSource('/api/events' + (focus ? '?focus=' + encodeURIComponent(focus) : ''));
const log = (cls, msg) => {
  const d = document.getElementById('log');
  d.innerHTML = `<span class="${cls}">${new Date().toLocaleTimeString()} ${msg}</span>\\n` + d.innerHTML;
};
// A null is "could not find out" and must never render as 0 or a healthy dash.
const unknown = t => `<span style="color:#6b7280">${t}</span>`;
const web = i => {
  if (!i.container_running) return unknown('-');
  const b = i.benches[0];
  if (!b) return unknown('no bench probed');
  if (!b.web_port_verified) return unknown('port unknown, not probed');
  if (!i.web_probed) return unknown(`:${b.web_port} not probed`);
  if (b.web_http_code === null) return unknown(`:${b.web_port} no answer`);
  return `${b.web_http_code} :${b.web_port} ${b.web_site || unknown('(no site)')}`;
};
const procs = i => i.benches.flatMap(b => b.processes.map(p =>
  `${p.label}${p.up ? '' : '!'}${p.state && p.state !== 'RUNNING' ? ':' + p.state : ''}`)).join(' ') || unknown('-');
const render = () => {
  document.getElementById('rows').innerHTML = [...model.values()].sort((a, b) =>
    a.project.localeCompare(b.project)).map(i => `<tr>
      <td>${i.project}</td><td>${i.docker_status}</td>
      <td><span class="pill ${i.overall}">${i.overall}</span></td>
      <td>${web(i)}</td><td>${procs(i)}</td>
      <td>${i.probe_ms === null ? unknown('never') : i.probe_ms.toFixed(0) + 'ms'}</td></tr>`).join('');
};
es.addEventListener('snapshot', e => {
  model.clear();
  JSON.parse(e.data).instances.forEach(i => model.set(i.project, i));
  document.getElementById('state').textContent = 'open' + (focus ? ' focus=' + focus : '');
  log('instant', `SNAPSHOT ${model.size} instances`);
  render();
});
es.addEventListener('delta', e => {
  const d = JSON.parse(e.data);
  if (d.removed) { model.delete(d.project); log(d.tier, `${d.tier.toUpperCase()} ${d.project} removed`); }
  else {
    model.set(d.project, d.instance);
    const c = d.cause ? ` ${d.cause.action}(${d.cause.service})` : '';
    log(d.tier, `${d.tier.toUpperCase()} ${d.project}${c} -> ${d.instance.overall}`);
  }
  render();
});
es.onerror = () => { document.getElementById('state').textContent = 'reconnecting'; };
</script></body></html>
"""
