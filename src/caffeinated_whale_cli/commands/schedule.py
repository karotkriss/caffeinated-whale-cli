"""``cwcli schedule``: scheduled jobs from the terminal, and the machine-wide
scheduler service for a shared install. See ``core.schedules``."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import typer

from ..core import list as core_list
from ..core import schedules as core_schedules
from ..core.errors import CwcliError
from ..utils import shared_home, startup
from ..utils.console import console, stderr_console

app = typer.Typer(help="Scheduled jobs: add, list, run now, remove; the shared-mode service.")


def _fail(e: CwcliError) -> None:
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    raise typer.Exit(code=1) from e


def _kv(pairs: list[str]) -> dict:
    out: dict = {}
    for p in pairs:
        k, _, v = p.partition("=")
        if not k:
            continue
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


@app.command("add")
def add(
    name: str = typer.Argument(..., help="A name for the schedule."),
    verb: str = typer.Option(..., "--verb", help="The job to run (cwcli schedule verbs)."),
    project: str = typer.Option(..., "--project", "-p", help="The instance."),
    bench: str = typer.Option(None, "--bench", "-b", help="Bench index or label."),
    site: str = typer.Option(None, "--site", "-s", help="The site (for site verbs)."),
    process: str = typer.Option(None, "--process", help="The process label (restart-process)."),
    cron: str = typer.Option(None, "--cron", help="Five-field cron: 'm h dom mon dow'."),
    daily: str = typer.Option(None, "--daily", help="Every day at HH:MM."),
    weekly: str = typer.Option(None, "--weekly", help="Weekly: 'DAY HH:MM' (sun..sat)."),
    at: str = typer.Option(None, "--at", help="Once, at an ISO time (YYYY-MM-DDTHH:MM)."),
    arg: list[str] = typer.Option([], "--arg", help="Job argument key=value (repeatable)."),
    unattended: bool = typer.Option(
        False, "--allow-unattended", help="Let updates and migrate run unattended."
    ),
    missed: str = typer.Option("run", "--missed", help="run (once on wake) or skip."),
    start_if_stopped: bool = typer.Option(
        False, "--start-if-stopped", help="Start a stopped instance first."
    ),
    respect_window: bool = typer.Option(
        False, "--in-window", help="Only inside the instance's maintenance window."
    ),
) -> None:
    """Add a schedule."""
    sched: dict = {
        "name": name,
        "verb": verb,
        "target": {
            k: v
            for k, v in {
                "project": project,
                "bench": bench,
                "site": site,
                "process": process,
            }.items()
            if v
        },
        "args": _kv(arg),
        "unattended": unattended,
        "missed": missed,
        "start_if_stopped": start_if_stopped,
        "respect_window": respect_window,
    }
    if at:
        sched["kind"] = "once"
        try:
            sched["at"] = datetime.fromisoformat(at).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            sched["at"] = at
    elif daily:
        h, _, m = daily.partition(":")
        sched["cron"] = f"{int(m or 0)} {int(h)} * * *"
    elif weekly:
        day, _, hm = weekly.partition(" ")
        h, _, m = hm.partition(":")
        sched["cron"] = f"{int(m or 0)} {int(h)} * * {day.lower()[:3]}"
    elif cron:
        sched["cron"] = cron
    else:
        stderr_console.print("[bold red]Error:[/bold red] give --cron, --daily, --weekly or --at.")
        raise typer.Exit(code=2)
    try:
        row = core_schedules.save(sched, by=os.environ.get("USER", "cli"))
    except CwcliError as e:
        _fail(e)
    console.print(
        f"[green]✓[/green] {row['id']}  {row['name']}  {row['when_text']}  next {row.get('next_run') or '-'}"
    )


@app.command("list")
def list_(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """List the schedules with their next run and last result."""
    data = core_schedules.list_schedules()
    if as_json:
        console.print_json(json.dumps(data))
        return
    if not data["schedules"]:
        console.print("No schedules. Add one with: cwcli schedule add")
        return
    for r in data["schedules"]:
        last = r.get("last_run") or {}
        t = r.get("target") or {}
        where = t.get("project", "") + (f" · {t['site']}" if t.get("site") else "")
        console.print(
            f"{r['id']}  {'on ' if r.get('enabled', True) else 'off'}  {r['name']}  ({r['verb']}) {where}  "
            f"{r['when_text']}  next {r.get('next_run') or '-'}  last {last.get('state') or '-'}"
        )


@app.command("run")
def run(schedule_id: str = typer.Argument(..., help="The schedule id.")) -> None:
    """Start the schedule's job now."""
    from .. import jobs as core_jobs

    try:
        rec = core_schedules.run_now(
            schedule_id,
            lambda verb, target, args, **kw: core_jobs.create_job(
                verb,
                target,
                args,
                consent=bool(kw.get("consent")),
                origin={
                    **(kw.get("origin") or {}),
                    "via": "cli",
                    "account": os.environ.get("USER", "cli"),
                },
            ),
        )
    except CwcliError as e:
        _fail(e)
    console.print(f"[green]✓[/green] started job {rec['id']} ({rec['verb']})")


@app.command("rm")
def rm(schedule_id: str = typer.Argument(..., help="The schedule id.")) -> None:
    """Remove a schedule."""
    try:
        core_schedules.delete(schedule_id)
    except CwcliError as e:
        _fail(e)
    console.print(f"[green]✓[/green] removed {schedule_id}")


@app.command("enable")
def enable(schedule_id: str = typer.Argument(...)) -> None:
    """Enable a schedule."""
    try:
        core_schedules.set_enabled(schedule_id, True, by=os.environ.get("USER", "cli"))
    except CwcliError as e:
        _fail(e)
    console.print(f"[green]✓[/green] {schedule_id} enabled")


@app.command("disable")
def disable(schedule_id: str = typer.Argument(...)) -> None:
    """Disable a schedule (kept, never fires)."""
    try:
        core_schedules.set_enabled(schedule_id, False, by=os.environ.get("USER", "cli"))
    except CwcliError as e:
        _fail(e)
    console.print(f"[green]✓[/green] {schedule_id} disabled")


def tick_once() -> list[dict]:
    """One scheduler pass outside the UI daemon (the service, or a test)."""
    from .. import jobs as core_jobs

    running = {
        d.project_name for d in (core_list.list_instances().data or []) if d.status == "running"
    }
    known = {d.project_name for d in (core_list.list_instances().data or [])}
    finished = [
        j
        for j in core_jobs.list_jobs()
        if j.get("state") in core_jobs.TERMINAL_STATES and _recent(j)
    ]
    return core_schedules.tick(
        core_jobs.create_job,
        lambda p: "running" if p in running else ("stopped" if p in known else None),
        finished_jobs=finished,
    )


def _recent(job: dict) -> bool:
    try:
        finished = datetime.fromisoformat(str(job.get("finished_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(finished.tzinfo) - finished).total_seconds() < 90


@app.command("daemon")
def daemon(
    install_service: bool = typer.Option(
        False,
        "--install-service",
        help="Shared install: install the machine-wide systemd unit (root) instead of running.",
    ),
) -> None:
    """Run the scheduler loop in this terminal (the shared-mode service runs this)."""
    if install_service:
        if not shared_home.shared_mode():
            stderr_console.print(
                "[bold red]Error:[/bold red] the machine-wide service is for a shared install (cwcli setup shared)."
            )
            raise typer.Exit(code=1)
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            stderr_console.print(
                "[bold red]Error:[/bold red] installing the service needs root (sudo)."
            )
            raise typer.Exit(code=1)
        cfg = shared_home.marker_config() or {}
        ok = startup.install_system_unit(
            startup.SCHEDULER,
            user=str(cfg.get("service_user") or "cwcli"),
            group=shared_home.group_name(),
            pid_file=str(core_schedules.store_path().with_suffix(".pid")),
        )
        console.print(
            "[green]✓[/green] cwcli-scheduler.service installed and enabled"
            if ok
            else "[red]could not install the service[/red]"
        )
        raise typer.Exit(code=0 if ok else 1)
    console.print("cwcli scheduler: ticking once a minute (Ctrl-C to stop)")
    stop = threading.Event()
    try:
        while not stop.is_set():
            try:
                for rec in tick_once():
                    console.print(f"  started {rec['verb']} job {rec['id']}")
            except Exception as e:  # the loop must outlive one bad tick
                stderr_console.print(f"[yellow]scheduler:[/yellow] {e}")
            time.sleep(60)
    except KeyboardInterrupt:
        console.print("\nstopping.")
