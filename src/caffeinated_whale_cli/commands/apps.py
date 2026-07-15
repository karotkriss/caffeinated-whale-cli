"""``cwcli apps`` - first-class Frappe app management.

A cohesive command group for listing, installing, uninstalling, and updating
Frappe apps per bench and (multi-site by default) per site. It replaces the raw
``cwcli run <project> bench ...`` escape hatch with per-bench/per-site addressing,
``--json`` output, honest aggregated exit codes, and the project's non-interactive
contract (non-TTY-without-flag refuses; auto-start gated by ``--yes``; destructive
uninstall gated by ``confirm_or_exit``/``--yes``).

Everything here is built from existing primitives - ``resolve_bench_path``,
``confirm_or_exit``, ``ensure_containers_running`` (commands/utils.py),
``bench_sites.list_sites`` for the canonical site set, and ``cache.recache_project``
for the post-mutation refresh (which routes through ``_redact_config_for_cache`` so
no secret is ever written). Nothing new is cached and no new dependency is added.

Output discipline for ``--json``: stdout carries ONLY the final JSON document. All
progress/errors go to stderr, and bench command output is captured (not streamed)
in JSON mode so it can never corrupt the JSON on stdout.
"""

import json
import shlex
import sys

import typer

from ..core.exec_stream import ExecChunk, exec_stream
from ..utils import bench_sites, cache
from ..utils.completion_utils import complete_app_names, complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import (
    get_frappe_container,
    handle_docker_errors,
)
from .update import run_app_update
from .utils import confirm_or_exit, ensure_containers_running, resolve_bench_path

app = typer.Typer(help="Manage Frappe apps: list, install, uninstall, update.")

_DEFAULT_BENCH = "/workspace/frappe-bench"


# --------------------------------------------------------------------------- helpers


def _resolve_bench(project_name, bench, bench_path, verbose):
    """Resolve the target bench path (``--bench``/``--path``, else the default).

    Falls back to the historical default only when there is no cache to resolve
    against (matching ``run``). A multi-bench project with no selector raises
    ``Exit(1)`` inside ``resolve_bench_path`` (``on_ambiguous="error"``).
    """
    return resolve_bench_path(project_name, bench, bench_path, verbose=verbose) or _DEFAULT_BENCH


def _capture_bench(frappe_container, cmd, workdir):
    """Run ``cmd`` in the container, capturing output. Returns ``(exit_code, text)``.

    The drain-and-join consumption mode of the exec-stream contract: nothing
    reaches stdout, so a ``--json`` document stays the only thing there.
    """
    text = []
    exit_code = 1
    for event in exec_stream(frappe_container, cmd, workdir=workdir):
        if isinstance(event, ExecChunk):
            text.append(event.text)
        else:
            exit_code = event.exit_code
    return exit_code, "".join(text)


def _stream_bench(frappe_container, cmd, workdir):
    """Run ``cmd`` streaming its output to stdout in real time. Returns the exit code.

    The render-each-event consumption mode - used in human (non-JSON) mode so the
    user sees bench's live progress. Both tags go to stdout, reproducing the
    combined stream this used to get from a non-demuxed exec.
    """
    exit_code = 1
    for event in exec_stream(frappe_container, cmd, workdir=workdir):
        if isinstance(event, ExecChunk):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        else:
            exit_code = event.exit_code
    return exit_code


def _run_bench(frappe_container, cmd, workdir, *, json_output, verbose):
    """Run a bench command, streaming in human mode and capturing in JSON mode.

    Returns the exit code. In JSON mode nothing is written to stdout (so the JSON
    document stays the only thing there); a failure's captured output is echoed to
    stderr for debuggability.
    """
    if verbose:
        stderr_console.print(f"[dim]$ {cmd}[/dim]")
    if json_output:
        exit_code, text = _capture_bench(frappe_container, cmd, workdir)
        if exit_code != 0 and text.strip():
            stderr_console.print(text.rstrip())
        return exit_code
    return _stream_bench(frappe_container, cmd, workdir)


def _list_available_apps(frappe_container, bench_path, verbose):
    """Available apps in the bench = the directories under ``apps/`` (live read)."""
    if verbose:
        stderr_console.print(f"[dim]$ ls -1 apps (in {bench_path})[/dim]")
    # Run via workdir rather than interpolating bench_path into the command, matching
    # _capture_bench/_stream_bench and avoiding any quoting hazard from a --path value.
    exit_code, output = frappe_container.exec_run("ls -1 apps", workdir=bench_path)
    if exit_code != 0:
        return []
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
    return [a for a in text.split("\n") if a.strip()]


def _list_installed_apps(frappe_container, bench_path, site, verbose):
    """Apps installed on ``site`` (live ``bench --site <site> list-apps``).

    Returns ``(ok, [app_names])``. ``ok`` is False if the bench command failed;
    only the first token of each line (the app name) is kept.
    """
    cmd = f"bench --site {shlex.quote(site)} list-apps"
    exit_code, text = _capture_bench(frappe_container, cmd, bench_path)
    if verbose:
        stderr_console.print(f"[dim]$ {cmd} -> exit {exit_code}[/dim]")
    if exit_code != 0:
        return False, []
    apps = [line.split()[0] for line in text.split("\n") if line.strip()]
    return True, apps


def _resolve_target_sites(frappe_container, bench_path, sites, verbose):
    """Resolve the target site set: explicit ``--site`` values, else ALL sites.

    Multi-site is the default: with no ``--site`` the target set is every real
    Frappe site on the bench (the canonical ``bench_sites.list_sites``). Returns a
    sorted list (deterministic fan-out order).
    """
    if sites:
        return list(dict.fromkeys(sites))  # de-dup, preserve order
    found = bench_sites.list_sites(frappe_container, bench_path, verbose)
    return sorted(found) if found else []


def _derive_app_name(target):
    """Fallback app name when the ``apps/`` before/after diff can't tell us.

    Used only when ``bench get-app`` added zero or more than one new ``apps/``
    entry (already-present app, or an ambiguous multi-dir fetch). A plain name is
    itself; a git URL clones into ``apps/<repo-basename>`` (minus a trailing
    ``.git``) by convention, which is the name ``install-app`` expects.
    """
    if "://" in target or target.endswith(".git") or "@" in target or "/" in target:
        base = target.rstrip("/").split("/")[-1]
        return base[:-4] if base.endswith(".git") else base
    return target


def _refresh_cache(project_name, verbose):
    """Refresh the cache through the existing writer so where/open/inspect stop lying.

    Uses ``cache.recache_project`` (a full inspect that persists via
    ``cache_project_data`` -> ``_redact_config_for_cache``), never a direct write.
    Degrades to a warning; the mutation itself already succeeded.
    """
    if not cache.recache_project(project_name, verbose=verbose):
        stderr_console.print(
            "[yellow]Warning:[/yellow] app mutation succeeded but refreshing the cache failed; "
            "run 'cwcli inspect --update' to refresh."
        )


def _report_and_exit(results, project_name, bench_path, json_output, *, success_msg):
    """Emit per-(app, site) results and exit non-zero if ANY step failed."""
    any_fail = any(not r["ok"] for r in results)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "project": project_name,
                    "bench": bench_path,
                    "results": results,
                    "ok": not any_fail,
                },
                indent=2,
            )
        )
    else:
        for r in results:
            mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
            where = f" on [magenta]{r['site']}[/magenta]" if r.get("site") else ""
            stderr_console.print(f"  {mark} {r['action']} [cyan]{r['app']}[/cyan]{where}")
        if any_fail:
            stderr_console.print("[bold red]Completed with errors.[/bold red]")
        else:
            console.print(f"[bold green]✓[/bold green] {success_msg}")
    if any_fail:
        raise typer.Exit(code=1)


# ----------------------------------------------------------------------------- list


@app.command("list")
@handle_docker_errors
def list_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="List installed apps for the named site(s). Repeatable."
    ),
    installed: bool = typer.Option(
        False, "--installed", "-i", help="Also show apps installed per site (all sites by default)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """List apps available in a bench, and (with --site/--installed) installed per site."""
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    frappe_container = get_frappe_container(project_name)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)

    available = _list_available_apps(frappe_container, resolved, verbose)

    installed_by_site = {}
    if installed or sites:
        for site in _resolve_target_sites(frappe_container, resolved, sites, verbose):
            ok, site_apps = _list_installed_apps(frappe_container, resolved, site, verbose)
            installed_by_site[site] = site_apps if ok else None

    any_fail = any(v is None for v in installed_by_site.values())

    if json_output:
        doc = {"project": project_name, "bench": resolved, "available_apps": available}
        if installed or sites:
            doc["installed"] = installed_by_site
        typer.echo(json.dumps(doc, indent=2))
        if any_fail:
            raise typer.Exit(code=1)
        return

    console.print(f"[bold]Available apps[/bold] ([dim]{resolved}[/dim]):")
    for a in available:
        console.print(f"  • [cyan]{a}[/cyan]")
    if not available:
        console.print("  [dim](none)[/dim]")
    if installed or sites:
        for site, site_apps in installed_by_site.items():
            console.print(f"\n[bold]Installed on[/bold] [magenta]{site}[/magenta]:")
            if site_apps is None:
                stderr_console.print("  [red]could not read installed apps[/red]")
            else:
                for a in site_apps:
                    console.print(f"  • [cyan]{a}[/cyan]")
    if any_fail:
        raise typer.Exit(code=1)


# -------------------------------------------------------------------------- install


@app.command("install")
@handle_docker_errors
def install_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Argument(..., help="App name(s) or git URL(s) to fetch and install."),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="Install on the named site(s). Repeatable. Omit to install on all."
    ),
    branch: str = typer.Option(None, "--branch", help="Git branch to fetch (passed to get-app)."),
    fetch_only: bool = typer.Option(
        False,
        "--fetch-only",
        help="Fetch the app(s) into the bench without installing on any site.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Fetch (bench get-app) and install app(s) on the target site(s).

    Each app is a known app name OR a git URL (passed straight to bench get-app).
    Multi-site by default: with no --site the app is installed on every site.
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    frappe_container = get_frappe_container(project_name)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)

    results = []
    fetched = []  # (original_target, installed_app_name)
    branch_arg = f"--branch {shlex.quote(branch)} " if branch else ""
    for target in apps:
        get_cmd = f"bench get-app {branch_arg}{shlex.quote(target)}"
        stderr_console.print(f"[bold cyan]Fetching[/bold cyan] {target}...")
        before = set(_list_available_apps(frappe_container, resolved, verbose))
        code = _run_bench(
            frappe_container, get_cmd, resolved, json_output=json_output, verbose=verbose
        )
        if code != 0:
            results.append({"app": target, "site": None, "action": "get-app", "ok": False})
            continue
        results.append({"app": target, "site": None, "action": "get-app", "ok": True})
        after = set(_list_available_apps(frappe_container, resolved, verbose))
        new_dirs = after - before
        app_name = new_dirs.pop() if len(new_dirs) == 1 else _derive_app_name(target)
        fetched.append((target, app_name))

    if not fetch_only:
        target_sites = _resolve_target_sites(frappe_container, resolved, sites, verbose)
        if not target_sites:
            stderr_console.print(
                "[yellow]Note:[/yellow] no sites on the bench to install on; app(s) fetched only."
            )
        for _target, app_name in fetched:
            for site in target_sites:
                install_cmd = (
                    f"bench --site {shlex.quote(site)} install-app {shlex.quote(app_name)}"
                )
                stderr_console.print(
                    f"[bold cyan]Installing[/bold cyan] {app_name} on [magenta]{site}[/magenta]..."
                )
                code = _run_bench(
                    frappe_container,
                    install_cmd,
                    resolved,
                    json_output=json_output,
                    verbose=verbose,
                )
                results.append(
                    {"app": app_name, "site": site, "action": "install-app", "ok": code == 0}
                )

    # Refresh the cache if any step succeeded: a fetch changes available apps, an
    # install changes a site's installed apps - both make the cache stale.
    if any(r["ok"] for r in results):
        _refresh_cache(project_name, verbose)

    # The banner must match what actually happened: only claim "installed" when an
    # install-app step ran (not for --fetch-only or a bench with no sites).
    installed = any(r["action"] == "install-app" for r in results)
    _report_and_exit(
        results,
        project_name,
        resolved,
        json_output,
        success_msg="App(s) installed." if installed else "App(s) fetched.",
    )


# ------------------------------------------------------------------------ uninstall


@app.command("uninstall")
@handle_docker_errors
def uninstall_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Argument(
        ..., help="App name(s) to uninstall.", autocompletion=complete_app_names
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="Uninstall from the named site(s). Repeatable. Omit for all sites."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the destructive confirmation and auto-start containers."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Uninstall app(s) from the target site(s) (destructive).

    Multi-site by default: with no --site the app is removed from every site. This
    destroys site data, so it is gated by --yes / an interactive confirmation.
    """
    ensure_containers_running(project_name, require_running=True, verbose=verbose, auto_start=yes)
    frappe_container = get_frappe_container(project_name)
    resolved = _resolve_bench(project_name, bench, bench_path, verbose)
    target_sites = _resolve_target_sites(frappe_container, resolved, sites, verbose)

    if not target_sites:
        stderr_console.print("[yellow]Note:[/yellow] no sites on the bench to uninstall from.")
        raise typer.Exit(code=0)

    # Destructive gate. --json implies non-interactive: without --yes it refuses,
    # never prompts (a prompt would fight the JSON-only-on-stdout contract).
    if json_output:
        if not yes:
            stderr_console.print(
                "[bold red]Error:[/bold red] 'apps uninstall --json' is destructive; pass --yes."
            )
            raise typer.Exit(code=1)
    else:
        confirm_or_exit(
            f"Uninstall {', '.join(apps)} from {len(target_sites)} site(s) "
            f"({', '.join(target_sites)})? This deletes their data.",
            assume_yes=yes,
            refuse_message=(
                "Uninstall is destructive and no confirmation was given. Pass --yes to proceed."
            ),
        )

    results = []
    for app_name in apps:
        for site in target_sites:
            cmd = f"bench --site {shlex.quote(site)} uninstall-app {shlex.quote(app_name)} --yes"
            stderr_console.print(
                f"[bold cyan]Uninstalling[/bold cyan] {app_name} from [magenta]{site}[/magenta]..."
            )
            code = _run_bench(
                frappe_container, cmd, resolved, json_output=json_output, verbose=verbose
            )
            results.append(
                {"app": app_name, "site": site, "action": "uninstall-app", "ok": code == 0}
            )

    if any(r["ok"] for r in results):
        _refresh_cache(project_name, verbose)

    _report_and_exit(
        results, project_name, resolved, json_output, success_msg="App(s) uninstalled."
    )


# ----------------------------------------------------------------------------- update


@app.command("update")
@handle_docker_errors
def update_apps(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    apps: list[str] = typer.Argument(
        None,
        help="App name(s) to update. Use 'frappe' to update the framework.",
        autocompletion=complete_app_names,
    ),
    bench: str = typer.Option(
        None, "--bench", help="Which bench to target: its numeric index or label."
    ),
    bench_path: str = typer.Option(
        None, "--path", "-p", help="Explicit bench directory (lower-level alternative to --bench)."
    ),
    sites: list[str] = typer.Option(
        None, "--site", help="Narrow migration to the named site(s). Repeatable."
    ),
    clear_cache: bool = typer.Option(
        False, "--clear-cache", "-c", help="Clear cache for affected sites after migration."
    ),
    clear_website_cache: bool = typer.Option(
        False, "--clear-website-cache", "-w", help="Clear website cache for affected sites."
    ),
    build: bool = typer.Option(False, "--build", "-b", help="Build assets after updating apps."),
    skip_maintenance: bool = typer.Option(
        False, "--skip-maintenance", help="Skip maintenance mode during update."
    ),
    no_recache: bool = typer.Option(
        False, "--no-recache", help="Skip re-caching after app updates."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-start stopped containers without prompting."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """Update app(s) and migrate affected sites (the canonical update path).

    Updating 'frappe' runs 'bench update --reset'. This is what the deprecated
    'cwcli update' now delegates to.
    """
    run_app_update(
        project_name,
        apps,
        bench=bench,
        bench_path=bench_path,
        verbose=verbose,
        clear_cache=clear_cache,
        clear_website_cache=clear_website_cache,
        build=build,
        skip_maintenance=skip_maintenance,
        no_recache=no_recache,
        yes=yes,
        sites=sites,
    )
