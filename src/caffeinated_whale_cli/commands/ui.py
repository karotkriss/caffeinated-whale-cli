"""``cwcli ui`` - open the web UI: start the local daemon (or reuse it) and sign in.

Three shapes:

* ``cwcli ui`` - the daily one. If a daemon this user started is still alive
  (pid file under ``cwcli_home()/run/``, identity-checked) and runs the installed
  code (its state-file ``build``), reuse it; a daemon that predates a reinstall is
  stopped and respawned. Otherwise spawn ``cwcli ui --foreground --no-browser``
  detached. Then open the browser on ``http://127.0.0.1:<port>/``, where the page
  asks for the administrator account (``admin`` / ``admin`` the first time, with
  a forced password change; ``cwcli admin reset-password`` recovers it).
* ``cwcli ui --foreground`` - run the daemon in this terminal (development, CI,
  the E2E harness). Ctrl-C stops it.
* ``cwcli ui --stop`` - stop the detached daemon.

The daemon ALWAYS binds loopback. The bearer token the detached daemon runs
under is generated here, handed to the child through ``CWCLI_SERVE_TOKEN`` (the
environment, never argv), and kept in a 0600 state file beside the pid file so a
later ``cwcli ui`` (and the self-update restart) can talk to the daemon: the
token file's trust boundary is the same as the pid file's (this user's home).
The browser never sees the token - it signs in with the account.

Why spawn ``cwcli ui --foreground`` rather than the serve module: the console
script installs the SIGTERM/SIGHUP unwind handlers (``utils/signals.py``), so
``--stop``'s SIGTERM lands as the same clean shutdown Ctrl-C gets.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import typer

from ..core import daemon_recovery
from ..core.version import _is_frozen, install_fingerprint
from ..utils import daemon_identity, shared_home
from ..utils.config_utils import cwcli_home
from ..utils.console import console, stderr_console
from . import serve as serve_cmd

HOST = "127.0.0.1"
_READY_TIMEOUT_S = 20.0
_STOP_TIMEOUT_S = 10.0


def run_dir() -> Path:
    return cwcli_home() / "run"


def _stem() -> str:
    """``ui``, or ``ui-<uid>`` in shared mode, where ``run/`` is one dir for the whole group."""
    return f"ui-{os.getuid()}" if shared_home.shared_mode() else "ui"


def pid_file() -> Path:
    return run_dir() / f"{_stem()}.pid"


def state_file() -> Path:
    """``{"port": int}`` plus the bearer token; 0600 (the token lives here)."""
    return run_dir() / f"{_stem()}.json"


def log_file() -> Path:
    return run_dir() / f"{_stem()}.log"


def _write_state(port: int, token: str) -> None:
    """The ``build`` is the code this daemon runs; a later ``cwcli ui`` compares it to
    the installed cwcli to tell a daemon that predates an upgrade."""
    run_dir().mkdir(parents=True, exist_ok=True)
    path = state_file()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": token, "build": install_fingerprint()}, f)
    os.chmod(path, 0o600)


def _read_state() -> dict | None:
    try:
        data = json.loads(state_file().read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("port"), int) and data.get("token"):
        return data
    return None


def _clear_state() -> None:
    state_file().unlink(missing_ok=True)
    daemon_identity.clear_pid_file(pid_file())


def _base(port: int) -> str:
    return f"http://{HOST}:{port}"


def _meta_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(_base(port) + "/api/v1/meta", timeout=2) as r:  # noqa: S310
            return bool(r.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _spawn_detached(port: int, token: str) -> None:
    """Start ``cwcli ui --foreground --no-browser`` as its own session, logging to ``ui.log``."""
    run_dir().mkdir(parents=True, exist_ok=True)
    prefix = [sys.argv[0]] if _is_frozen() else [sys.executable, "-m", "caffeinated_whale_cli"]
    argv = [
        *prefix,
        "ui",
        "--foreground",
        "--no-browser",
        "--port",
        str(port),
    ]
    env = {**os.environ, serve_cmd.TOKEN_ENV: token}
    if sys.platform == "win32":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        new_session = False
    else:
        creationflags = 0
        new_session = True
    fd = os.open(log_file(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(log_file(), 0o600)
    with os.fdopen(fd, "a") as log:
        subprocess.Popen(
            argv,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            creationflags=creationflags,
            start_new_session=new_session,
        )


def _open(url: str, *, no_browser: bool) -> None:
    console.print(f"  open: [cyan]{url}[/cyan]", soft_wrap=True)
    if no_browser:
        return
    try:
        opened = webbrowser.open(url)
    except Exception:  # pragma: no cover - platform browser plumbing
        opened = False
    if not opened:
        console.print("  (no browser could be opened here; paste the link into one)")


def _stop() -> None:
    if not daemon_identity.is_running(pid_file()):
        _clear_state()
        console.print("cwcli ui is not running.")
        return
    pid = daemon_identity.get_pid(pid_file())
    assert pid is not None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        stderr_console.print(f"[red]Could not stop cwcli ui (pid {pid}): {e}[/red]")
        raise typer.Exit(code=1) from e
    deadline = time.monotonic() + _STOP_TIMEOUT_S
    while time.monotonic() < deadline and daemon_identity.pid_alive(pid):
        time.sleep(0.1)
    if daemon_identity.pid_alive(pid):
        stderr_console.print(
            f"[red]cwcli ui (pid {pid}) did not stop within {_STOP_TIMEOUT_S:.0f}s.[/red]"
        )
        raise typer.Exit(code=1)
    _clear_state()
    console.print(f"cwcli ui stopped (pid {pid}).")


def _is_stale(state: dict) -> bool:
    """True when the installed cwcli is no longer the code the daemon was started with.

    A state file with no ``build`` was written by a daemon that predates the check,
    so it is stale by definition. An unreadable install (mid-reinstall) is not a
    reason to restart anything.
    """
    installed = install_fingerprint()
    return not installed.endswith(":?") and state.get("build") != installed


def _foreground(port: int, *, no_browser: bool) -> None:
    if daemon_identity.is_running(pid_file()):
        stderr_console.print(
            "[red]cwcli ui is already running (detached).[/red] "
            "Run 'cwcli ui --stop' first to run it in the foreground."
        )
        raise typer.Exit(code=1)
    token = serve_cmd.auth_token() or secrets.token_urlsafe(32)

    def _ready(link: str) -> None:
        _write_state(port, token)
        daemon_identity.write_pid_file(pid_file())
        if not no_browser:
            threading.Thread(
                target=_open, args=(link,), kwargs={"no_browser": False}, daemon=True
            ).start()

    try:
        serve_cmd.run_daemon(
            HOST, port, serve_cmd.DEFAULT_INTERVAL, env_token=token, on_ready=_ready
        )
    finally:
        if daemon_identity.get_pid(pid_file()) == os.getpid():
            _clear_state()


def _recover_daemons() -> None:
    """Bring back an enabled background daemon that is not running, saying so."""
    for note in daemon_recovery.recover():
        (console if note.started else stderr_console).print(note.note)


def _start_or_reuse(port: int, *, no_browser: bool) -> None:
    _recover_daemons()
    state = _read_state() if daemon_identity.is_running(pid_file()) else None
    if state is not None and _is_stale(state):
        console.print("cwcli was updated since this cwcli ui daemon started; restarting it.")
        _stop()
        if port == serve_cmd.DEFAULT_PORT:
            port = state["port"]
        state = None
    if state is not None:
        if state["port"] != port and port != serve_cmd.DEFAULT_PORT:
            stderr_console.print(
                f"[red]cwcli ui is already running on port {state['port']}.[/red] "
                "Run 'cwcli ui --stop' first to move it."
            )
            raise typer.Exit(code=1)
        port = state["port"]
        console.print(f"[bold]cwcli ui[/bold] already running on [cyan]{_base(port)}[/cyan]")
        token = state["token"]
    else:
        token = secrets.token_urlsafe(32)
        _spawn_detached(port, token)
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            state = _read_state() if daemon_identity.get_pid(pid_file()) is not None else None
            if state is not None and state["port"] == port and _meta_answers(port):
                token = state["token"]
                break
            time.sleep(0.2)
        else:
            stderr_console.print(
                f"[red]cwcli ui did not come up on port {port} within {_READY_TIMEOUT_S:.0f}s.[/red]\n"
                f"  See {log_file()} (a port already in use is the usual cause; try --port).",
                soft_wrap=True,
            )
            raise typer.Exit(code=1)
        console.print(f"[bold]cwcli ui[/bold] started on [cyan]{_base(port)}[/cyan] (detached)")
        console.print("  stop it with: cwcli ui --stop")
    console.print("  sign in with the administrator account (admin / admin the first time)")
    _open(serve_cmd.page_url(HOST, port), no_browser=no_browser)


def ui(
    port: int = typer.Option(serve_cmd.DEFAULT_PORT, "--port", "-p", help="Port to listen on."),
    foreground: bool = typer.Option(
        False, "--foreground", help="Run the daemon in this terminal instead of detached."
    ),
    stop: bool = typer.Option(False, "--stop", help="Stop the detached daemon and exit."),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the sign-in link instead of opening a browser."
    ),
    job: str | None = typer.Option(None, "--job", hidden=True),
):
    """Open the cwcli web UI in your browser (starts the local daemon if needed)."""
    if job is not None:
        # The detached job worker's re-entry for a frozen standalone exe, which has
        # no `python -m caffeinated_whale_cli.jobs`; a normal install never uses it.
        from .. import jobs

        raise typer.Exit(code=jobs.run_job(job))
    if stop and foreground:
        stderr_console.print("[red]--stop and --foreground are mutually exclusive.[/red]")
        raise typer.Exit(code=2)
    if stop:
        _stop()
    elif foreground:
        _foreground(port, no_browser=no_browser)
    else:
        _start_or_reuse(port, no_browser=no_browser)
