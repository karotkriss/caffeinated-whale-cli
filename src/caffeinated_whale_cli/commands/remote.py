"""``cwcli remote`` - the two sides of a hub link (renderers over ``core.remote_access``
and ``core.hosts``).

On a REMOTE machine, the one whose instances another machine's web UI should show,
run these as the user who runs cwcli there::

    cwcli remote allow --hub <name> --key "<hub public key>" [--max-role operator]
    cwcli remote revoke --hub <name>
    cwcli remote hubs

On the HUB, the machine running the web UI::

    cwcli remote key              # this hub's public key and the allow command
    cwcli remote add <pairing>    # pin a remote from the pairing string allow printed
    cwcli remote ls [--check]
    cwcli remote remove <id>

There is deliberately no ``cwcli axi remote``: granting another machine's key a
foothold on this one, or pinning a machine this hub will dial and act on, is a
person's decision.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import typer
from rich.markup import escape

from ..core import hosts as core_hosts
from ..core import remote_access
from ..core.errors import CwcliError
from ..utils.console import console, stderr_console

app = typer.Typer(
    help="Show other machines' instances in this web UI (the hub), or let a hub reach this one."
)


def _fail(e: CwcliError) -> NoReturn:
    stderr_console.print(f"[bold red]Error:[/bold red] {escape(e.message)}", soft_wrap=True)
    if e.hint:
        stderr_console.print(escape(e.hint), soft_wrap=True)
    raise typer.Exit(code=1)


def _by() -> str:
    return f"cli:{os.environ.get('SUDO_USER') or getpass.getuser()}"


# ------------------------------------------------------------------ the remote


@app.command("allow")
def allow_cmd(
    hub: str = typer.Option(..., "--hub", help="The hub's name (its web UI shows it)."),
    key: str = typer.Option(
        ..., "--key", help="The hub's public key line, as its web UI shows it."
    ),
    max_role: str = typer.Option(
        core_hosts.DEFAULT_CAP,
        "--max-role",
        help="The most the hub may do here: viewer, operator (default) or admin.",
    ),
    address: str | None = typer.Option(
        None, "--address", help="How the hub reaches this machine (default: guessed)."
    ),
    port: int = typer.Option(22, "--port", help="This machine's SSH port."),
    name: str | None = typer.Option(
        None, "--name", help="How the hub labels this machine (default: the hostname)."
    ),
    host_key_file: str | None = typer.Option(
        None,
        "--host-key-file",
        help="This machine's SSH host public key (default: /etc/ssh/ssh_host_ed25519_key.pub).",
    ),
    authorized_keys: str | None = typer.Option(
        None,
        "--authorized-keys",
        help="The authorized_keys file to write (default: ~/.ssh/authorized_keys).",
    ),
    start_daemon: bool = typer.Option(
        True,
        "--start-daemon/--no-start-daemon",
        help="Let the hub start this machine's cwcli ui daemon (as `cwcli ui` would) when it is not running.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Let a hub's web UI show and drive this machine's instances, up to a role cap.

    Writes ONE restricted line into this user's authorized_keys: the hub's key can run
    cwcli's bridge and nothing else (no shell, no forwarding), and whatever the hub's
    user is, this machine treats them as at most --max-role. Prints the pairing string
    to paste into the hub, which carries this machine's SSH host key so the hub pins it.
    """
    try:
        grant = remote_access.allow(
            hub=hub,
            key=key,
            cap=max_role,
            address=address,
            port=port,
            name=name,
            host_key_file=host_key_file,
            keys_path=None if authorized_keys is None else Path(authorized_keys),
            start_daemon=start_daemon,
        )
    except CwcliError as e:
        _fail(e)
    note = remote_access.shared_mode_note()
    if as_json:
        console.print_json(json.dumps({**asdict(grant), "note": note}))
        return
    verb = "Replaced" if grant.replaced else "Added"
    console.print(
        f"{verb} hub [bold]{escape(grant.hub)}[/bold] in {escape(grant.keys_path)} "
        f"with the [bold]{grant.cap.title()}[/bold] cap.",
        soft_wrap=True,
    )
    console.print("  The key runs only cwcli's bridge: no shell, no port forwarding.")
    if grant.ended_pids:
        console.print(
            f"  Ended {len(grant.ended_pids)} bridge process(es) still running under the old line: "
            f"{', '.join(map(str, grant.ended_pids))}."
        )
    fp = grant.fields.get("host_key_fingerprint")
    if fp:
        console.print(f"  This machine's host key: {escape(fp)}", soft_wrap=True)
    console.print()
    console.print("Paste this pairing string into the hub (Settings > Remote hosts > Add host):")
    console.print(f"[cyan]{grant.pairing}[/cyan]", soft_wrap=True)
    console.print(
        f"  it names {escape(grant.fields['user'])}@{escape(str(grant.fields['address']))}:{grant.fields['port']}; "
        "the hub can change the address if it reaches this machine another way.",
        soft_wrap=True,
    )
    for warning in grant.warnings:
        stderr_console.print(f"[yellow]Warning:[/yellow] {escape(warning)}", soft_wrap=True)
    if note:
        stderr_console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)
    console.print(f"Undo with: cwcli remote revoke --hub {escape(grant.hub)}")


@app.command("revoke")
def revoke_cmd(
    hub: str = typer.Option(..., "--hub", help="The hub to revoke."),
    authorized_keys: str | None = typer.Option(
        None,
        "--authorized-keys",
        help="The authorized_keys file (default: ~/.ssh/authorized_keys and the file `allow` "
        "recorded for the hub).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Withdraw a hub's access: remove its line and end its running bridge processes.

    When the hub's recorded grant is in a file this account cannot read (another account's
    authorized_keys), it exits 1 naming the account to run it as.
    """
    try:
        done = remote_access.revoke(
            hub=hub,
            keys_path=None if authorized_keys is None else Path(authorized_keys),
        )
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps(asdict(done)))
        return
    if done.removed:
        changed = ", ".join(path for path, n in done.files.items() if n)
        console.print(
            f"Revoked hub [bold]{escape(done.hub)}[/bold]: removed {done.removed} line(s) from {escape(changed)}.",
            soft_wrap=True,
        )
    else:
        console.print(
            f"Hub {escape(done.hub)} had no line in {escape(', '.join(done.files))}; nothing to remove.",
            soft_wrap=True,
        )
    if done.ended_pids:
        console.print(
            f"  Ended {len(done.ended_pids)} running bridge process(es): {', '.join(map(str, done.ended_pids))}."
        )


@app.command("hubs")
def hubs_cmd(
    authorized_keys: str | None = typer.Option(
        None,
        "--authorized-keys",
        help="The authorized_keys file to read (default: ~/.ssh/authorized_keys and every file "
        "`allow` wrote a grant to).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """The hubs this machine lets in, with each one's role cap and the cwcli home its bridge
    serves. A recorded grant whose file this account cannot read is listed from the record."""
    try:
        grants = remote_access.list_grants(
            None if authorized_keys is None else Path(authorized_keys)
        )
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(json.dumps([asdict(g) for g in grants]))
        return
    if not grants:
        console.print("No hub is allowed on this machine.")
        return
    for g in grants:
        if g.unreadable:
            console.print(
                f"[bold]{escape(g.hub)}[/bold]  recorded by {escape(g.recorded_by)}, not readable from "
                f"this account  cap {g.cap or '?'}  {escape(g.fingerprint)}  home {escape(g.home or '?')}  "
                f"{escape(g.keys_path)}",
                soft_wrap=True,
            )
            continue
        console.print(
            f"[bold]{escape(g.hub)}[/bold]  cap {g.cap or '?'}  {escape(g.key_type)} {escape(g.fingerprint)}  "
            f"home {escape(g.home or '?')}  {escape(g.keys_path)}",
            soft_wrap=True,
        )


# --------------------------------------------------------------------- the hub


@app.command("key")
def key_cmd(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """This hub's name and public key, and the command a remote's owner runs to allow it.
    Creates the key pair the first time."""
    try:
        name = core_hosts.hub_name()
        key = core_hosts.ensure_hub_key(name)
    except CwcliError as e:
        _fail(e)
    if as_json:
        console.print_json(
            json.dumps(
                {"hub": name, "public_key": key, "allow_command": core_hosts.allow_command()}
            )
        )
        return
    console.print(f"Hub: [bold]{escape(name)}[/bold]")
    console.print(f"Public key: {escape(key)}", soft_wrap=True)
    console.print("On each remote machine, as the user who runs cwcli there:")
    console.print(f"  [cyan]{escape(core_hosts.allow_command())}[/cyan]", soft_wrap=True)


def _probe_line(rec: core_hosts.HostRecord) -> str:
    try:
        probe = core_hosts.probe_host(core_hosts.SshTransport(), rec)
    except core_hosts.LinkError as e:
        return f"{e.state}: {e.reason}"
    detail = ""
    if probe.remote:
        detail = f" cwcli {probe.remote.get('version')} (protocol {probe.remote.get('protocol')})"
    cap = f" cap {probe.cap}" if probe.cap else ""
    return f"{probe.state}{cap}{detail}{': ' + probe.reason if probe.reason else ''}"


@app.command("add")
def add_cmd(
    pairing: str | None = typer.Argument(
        None, help="The pairing string `cwcli remote allow` printed on the remote."
    ),
    name: str | None = typer.Option(
        None, "--name", help="A label for the host (default: from the pairing string)."
    ),
    address: str | None = typer.Option(None, "--address", help="Override the address."),
    port: int | None = typer.Option(None, "--port", help="Override the SSH port."),
    user: str | None = typer.Option(None, "--user", help="Override the SSH user."),
    host_key: str | None = typer.Option(
        None,
        "--host-key",
        help="The remote's SSH host public key line, when the pairing string lacks it.",
    ),
    check: bool = typer.Option(
        True, "--check/--no-check", help="Connect once and report the link state."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Pin a remote machine on this hub. Nothing is trusted on first contact: the host
    key comes from the pairing string (or --host-key) and a different key later is a
    hard stop."""
    try:
        rec = core_hosts.add_host(
            pairing,
            name=name,
            address=address,
            port=port,
            user=user,
            host_key=host_key,
            by=_by(),
        )
    except CwcliError as e:
        _fail(e)
    state = _probe_line(rec) if check else None
    on = bool(core_hosts.transports())
    note = core_hosts.shared_mode_note()
    if as_json:
        console.print_json(
            json.dumps(
                {
                    **asdict(rec),
                    "fingerprint": rec.fingerprint(),
                    "link": state,
                    "transport_on": on,
                    "note": note,
                }
            )
        )
        return
    console.print(
        f"Added [bold]{escape(rec.id)}[/bold] ({escape(rec.name)}): {escape(rec.user)}@{escape(rec.address)}:{rec.port}, host key {escape(rec.fingerprint())}",
        soft_wrap=True,
    )
    if state:
        console.print(f"  link: {escape(state)}", soft_wrap=True)
    if not on:
        stderr_console.print(
            "[yellow]Note:[/yellow] the Remote hosts over SSH integration is off; turn it on in "
            "Settings > Integrations for the web UI to connect.",
            soft_wrap=True,
        )
    if note:
        stderr_console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)


@app.command("ls")
def ls_cmd(
    check: bool = typer.Option(
        False, "--check", help="Connect to each host now and report its link state."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """The remote hosts this hub knows."""
    try:
        records = core_hosts.list_hosts()
    except CwcliError as e:
        _fail(e)
    rows = []
    for rec in records:
        rows.append(
            {
                **asdict(rec),
                "fingerprint": rec.fingerprint(),
                "link": _probe_line(rec) if check else None,
            }
        )
    note = core_hosts.shared_mode_note() if check else None
    if note:
        stderr_console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)
    if as_json:
        console.print_json(json.dumps(rows))
        return
    if not rows:
        console.print("No remote hosts. Add one with: cwcli remote add <pairing string>")
        return
    for row in rows:
        when = (
            datetime.fromtimestamp(row["added_at"]).strftime("%Y-%m-%d %H:%M")
            if row["added_at"]
            else "-"
        )
        console.print(
            f"[bold]{escape(row['id'])}[/bold]  {escape(row['name'])}  {escape(row['user'])}@{escape(row['address'])}:{row['port']}  "
            f"{escape(row['fingerprint'])}  added {when}"
            + (f"  {escape(row['link'])}" if row["link"] else ""),
            soft_wrap=True,
        )


@app.command("remove")
def remove_cmd(
    host_id: str = typer.Argument(..., help="The host id (see `cwcli remote ls`).")
) -> None:
    """Forget a remote host and drop its connection. The remote keeps its grant until its
    owner runs `cwcli remote revoke` there."""
    try:
        rec = core_hosts.remove_host(host_id)
    except CwcliError as e:
        _fail(e)
    core_hosts.SshTransport().close(rec)
    console.print(
        f"Removed {escape(rec.id)} ({escape(rec.user)}@{escape(rec.address)}:{rec.port}).",
        soft_wrap=True,
    )
    console.print(
        f"  On that machine, `cwcli remote revoke --hub {escape(core_hosts.hub_name())}` withdraws this hub's key."
    )
