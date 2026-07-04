import json
import time

import docker
import questionary
import typer
from rich.console import Console
from rich.tree import Tree

from ..utils import bench_labels, config_utils, db_utils
from ..utils.completion_utils import complete_project_names
from ..utils.docker_utils import (
    get_frappe_container,
    get_project_containers,
    handle_docker_errors,
)
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running

console_out = Console()
console_err = Console(stderr=True)


def _run_command(
    container: docker.models.containers.Container,
    cmd: str,
    verbose: bool = False,
    workdir: str | None = None,
) -> tuple[int, str]:
    if verbose:
        console_err.print(f"[dim]$ {cmd}[/dim]")
    exit_code, output = container.exec_run(cmd, workdir=workdir)
    stdout_bytes = output[0] if isinstance(output, tuple) else output
    decoded_output = stdout_bytes.decode("utf-8").strip()
    if verbose:
        console_err.print(f"[bold yellow]VERBOSE: Exit Code:[/bold yellow] {exit_code}")
        console_err.print(
            f"[bold yellow]VERBOSE: Output:[/bold yellow]\n---\n{decoded_output}\n---"
        )
    return exit_code, decoded_output


def _is_bench_directory(
    container: docker.models.containers.Container, path: str, verbose: bool = False
) -> bool:
    check_command = f'sh -c "test -d {path}/sites && test -d {path}/apps && test -f {path}/sites/common_site_config.json"'
    exit_code, _ = _run_command(container, check_command, verbose)
    return exit_code == 0


def _get_sites(
    container: docker.models.containers.Container, bench_dir: str, verbose: bool = False
) -> list[str]:
    exit_code, output = _run_command(container, f"ls -1 {bench_dir}/sites", verbose)
    if exit_code != 0:
        return []
    excluded = {"apps.txt", "assets", "common_site_config.json", "example.com", "apps.json"}
    return [item for item in output.split("\n") if item and item not in excluded]


def _get_installed_apps(
    container: docker.models.containers.Container, bench_dir: str, site: str, verbose: bool = False
) -> list[str]:
    cmd = f"bench --site {site} list-apps"
    exit_code, output = _run_command(container, cmd, verbose, workdir=bench_dir)
    if exit_code != 0:
        return [f"Error fetching apps for site {site}"]
    return [app for app in output.split("\n") if app]


def _get_available_apps(
    container: docker.models.containers.Container, bench_dir: str, verbose: bool = False
) -> list[str]:
    exit_code, output = _run_command(container, f"ls -1 {bench_dir}/apps", verbose)
    if exit_code != 0:
        return []
    return [app for app in output.split("\n") if app]


def _find_bench_instances(
    container: docker.models.containers.Container, verbose: bool = False
) -> list[str]:
    """Finds all potential bench directories using default and custom TOML config paths."""
    benches_found = []

    default_search_roots = [
        "/home/frappe",
        "/home/frappe/workspace/development",
        "/workspace/development",
    ]
    config = config_utils.load_config()
    custom_search_roots = config.get("search_paths", {}).get("custom_bench_paths", [])

    all_search_roots = list(set(default_search_roots + custom_search_roots))

    for root in all_search_roots:
        if verbose:
            console_err.print(f"VERBOSE: Searching for benches in '{root}'...")
        # Find directories named 'apps' which are a reliable indicator of a bench's parent.
        find_cmd = f"find {root} -maxdepth 2 -type d -name 'apps'"
        exit_code, output = _run_command(container, find_cmd, verbose)
        if exit_code == 0:
            for path in output.strip().split("\n"):
                if path:
                    # The bench dir is the parent of the 'apps' dir
                    bench_dir = path.removesuffix("/apps")
                    if _is_bench_directory(container, bench_dir, verbose):
                        benches_found.append(bench_dir)

    # Sort for a STABLE discovery order. Each bench's position in this list is its
    # numeric label / index (0, 1, 2, ...), so the ordering must be deterministic
    # across runs - a bare ``set`` iteration order is not. Sorting by path is stable
    # for a fixed set of benches (see utils/bench_labels.py for the label model).
    return sorted(set(benches_found))


def _get_common_site_config(
    frappe_container: docker.models.containers.Container, bench_dir: str, verbose: bool
) -> dict | None:
    """Fetches common_site_config.json from the bench directory."""
    config_path = f"{bench_dir}/sites/common_site_config.json"
    cmd = f"cat {config_path}"

    exit_code, output = _run_command(frappe_container, cmd, verbose)

    if exit_code == 0 and output:
        try:
            config: dict = json.loads(output)
            if verbose:
                console_err.print(
                    f"[dim]VERBOSE: Found common_site_config with {len(config)} keys[/dim]"
                )
            return config
        except json.JSONDecodeError:
            if verbose:
                console_err.print(
                    "[dim yellow]VERBOSE: Failed to parse common_site_config.json[/dim yellow]"
                )
            return None
    else:
        if verbose:
            console_err.print(
                "[dim yellow]VERBOSE: common_site_config.json not found or not readable[/dim yellow]"
            )
        return None


def _get_site_config(
    frappe_container: docker.models.containers.Container,
    bench_dir: str,
    site_name: str,
    verbose: bool,
) -> dict | None:
    """Fetches site_config.json for a specific site."""
    config_path = f"{bench_dir}/sites/{site_name}/site_config.json"
    cmd = f"cat {config_path}"

    exit_code, output = _run_command(frappe_container, cmd, verbose)

    if exit_code == 0 and output:
        try:
            config: dict = json.loads(output)
            if verbose:
                console_err.print(
                    f"[dim]VERBOSE: Found site_config for {site_name} with {len(config)} keys[/dim]"
                )
            return config
        except json.JSONDecodeError:
            if verbose:
                console_err.print(
                    f"[dim yellow]VERBOSE: Failed to parse site_config.json for {site_name}[/dim yellow]"
                )
            return None
    else:
        if verbose:
            console_err.print(
                f"[dim yellow]VERBOSE: site_config.json not found for {site_name}[/dim yellow]"
            )
        return None


def _gather_bench_data(
    frappe_container: docker.models.containers.Container, bench_dir: str, verbose: bool
) -> dict:
    """Gathers sites, apps, and configs for a single bench instance."""
    if verbose:
        console_err.print(f"VERBOSE: Inspecting Bench Instance: {bench_dir}")

    available_apps = _get_available_apps(frappe_container, bench_dir, verbose)

    # Fetch common site config
    common_site_config = _get_common_site_config(frappe_container, bench_dir, verbose)

    sites = _get_sites(frappe_container, bench_dir, verbose)
    sites_info = []
    for site in sites:
        if verbose:
            console_err.print(f"VERBOSE:   - Found Site: {site}")

        installed_apps = _get_installed_apps(frappe_container, bench_dir, site, verbose)

        # Fetch site-specific config
        site_config = _get_site_config(frappe_container, bench_dir, site, verbose)

        site_data: dict = {"name": site, "installed_apps": installed_apps}
        if site_config is not None:
            site_data["site_config"] = site_config

        sites_info.append(site_data)

    bench_data: dict = {"path": bench_dir, "sites": sites_info, "available_apps": available_apps}

    # Recover the user label from the per-bench marker file. This is what lets a
    # full inspect rebuild labels after the SQLite cache is lost: the marker lives
    # inside the bench, so it survives a cache wipe. The marker is the source of
    # truth for labels; the rest of the bench config is re-derived live as above.
    marker_label = bench_labels.read_label_marker(frappe_container, bench_dir, verbose)
    if marker_label:
        bench_data["label"] = marker_label
        if verbose:
            console_err.print(f"[dim]VERBOSE: Recovered label '{marker_label}' from marker[/dim]")

    if common_site_config is not None:
        bench_data["common_site_config"] = common_site_config

    return bench_data


def partial_inspect_known_benches(
    frappe_container: docker.models.containers.Container,
    cached_bench_instances: list[dict],
    verbose: bool = False,
) -> tuple[list[dict], bool]:
    """Read-only freshness pass (the "T2 partial inspect") over KNOWN bench paths.

    This is a pure drift detector: for each bench path already in the cache it
    cheaply re-reads only the inexpensive, filesystem-level facts via
    ``test``/``ls`` (the bench check, the available-apps list, and the site list).
    It deliberately does NOT:

    - re-discover bench instances (no ``find`` over the search roots); a brand-new
      bench is only picked up by the full inspect,
    - run the deep per-site ``bench list-apps`` (which boots Frappe); the cached
      per-site ``installed_apps`` are carried forward instead, and
    - re-read any config files (``common_site_config.json`` /
      ``site_config.json``); the cached configs are carried forward, so a
      transient unreadable or half-written config can never silently drop the
      cached ``common_site_config`` / ``default_site`` label.

    It never writes the cache. A freshly installed app shows up in ``apps/``
    immediately, so the cheap ``ls apps`` here catches it - which is exactly what
    fixes ``open --app`` and inspect's "Available Apps" without a manual
    ``inspect -u``.

    Returns ``(refreshed_bench_instances, drift)`` where ``drift`` is True when the
    on-disk available-apps or site set diverged from the cache (or a known bench
    vanished). The caller escalates to a full inspect on drift so the per-site
    installed lists (and any brand-new bench) are also brought up to date; on no
    drift the caller serves the cache unchanged without persisting.
    """
    refreshed: list[dict] = []
    drift = False

    for cached_bench in cached_bench_instances:
        bench_dir = cached_bench["path"]

        # Known bench vanished -> stale cache, force a full re-inspect.
        if not _is_bench_directory(frappe_container, bench_dir, verbose):
            if verbose:
                console_err.print(
                    f"VERBOSE: Cached bench '{bench_dir}' no longer present; marking drift."
                )
            drift = True
            continue

        fresh_available = _get_available_apps(frappe_container, bench_dir, verbose)
        fresh_sites = _get_sites(frappe_container, bench_dir, verbose)

        cached_site_names = {s["name"] for s in cached_bench.get("sites", [])}
        if (
            set(fresh_available) != set(cached_bench.get("available_apps", []))
            or set(fresh_sites) != cached_site_names
        ):
            drift = True

        cached_sites_by_name = {s["name"]: s for s in cached_bench.get("sites", [])}
        sites_info: list[dict] = []
        for site in fresh_sites:
            previous = cached_sites_by_name.get(site)
            # Carry the cached per-site installed apps and config forward; a
            # brand-new site has no cached entry, so it stays empty until a full
            # inspect (triggered by the drift this new site causes) populates it.
            site_data: dict = {
                "name": site,
                "installed_apps": list(previous["installed_apps"]) if previous else [],
            }
            if previous is not None and "site_config" in previous:
                site_data["site_config"] = previous["site_config"]
            sites_info.append(site_data)

        bench_data: dict = {
            "path": bench_dir,
            "sites": sites_info,
            "available_apps": fresh_available,
        }
        # Carry the cached user label forward. T2 is a cheap freshness pass and does
        # not re-read the marker; the label is preserved so a partial refresh never
        # drops it (a real label change goes through `label`/`inspect -i`, which
        # updates the cache directly).
        if cached_bench.get("label"):
            bench_data["label"] = cached_bench["label"]
        if "common_site_config" in cached_bench:
            bench_data["common_site_config"] = cached_bench["common_site_config"]
        refreshed.append(bench_data)

    return refreshed, drift


@handle_docker_errors
def inspect(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project to inspect.", autocompletion=complete_project_names
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose diagnostic output."
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Output the result as a JSON object."
    ),
    update: bool = typer.Option(
        False, "--update", "-u", help="Update the cache by re-inspecting the project."
    ),
    no_refresh: bool = typer.Option(
        False,
        "--no-refresh",
        help=(
            "Return cached data as-is, skipping the lightweight freshness pass. "
            "Fastest, but the result may be stale (e.g. miss a just-installed app)."
        ),
    ),
    show_apps: bool = typer.Option(
        False, "--show-apps", "-a", help="Show available apps in the output tree."
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Prompt to name each bench instance interactively."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Auto-start stopped containers without prompting (non-interactive).",
    ),
    prompt_to_start: bool = typer.Option(
        True,
        "--prompt-start/--no-prompt-start",
        hidden=True,
        help=(
            "Whether to interactively offer to start stopped containers. Disabled by "
            "non-interactive callers (e.g. the rm recache path) that run under a spinner."
        ),
    ),
):
    """
    Inspects a Project to find all Bench Instances, Sites, and Apps within it.
    Caches the results for faster subsequent inspects.
    """
    if verbose:
        console_err.print(f"VERBOSE: --- Inspecting Project: {project_name} ---")

    # Set only when a T2 drift-escalation forces a full inspect while a valid cache
    # exists; if that full inspect then can't discover any bench (e.g. a custom
    # search path was removed), we degrade to this cached data instead of failing.
    drift_fallback_benches = None

    if not update:
        cached_data = db_utils.get_cached_project_data(project_name)
        if cached_data:
            cached_benches = cached_data["bench_instances"]
            if verbose:
                console_err.print(
                    f"VERBOSE: Found cached data for this project from {cached_data['last_updated']}."
                )
            if no_refresh:
                # Tier 1: serve the cache verbatim, no container calls (fastest path).
                if verbose:
                    console_err.print("VERBOSE: --no-refresh set; serving cached data as-is.")
                bench_instances_data = cached_benches
            else:
                # Tier 2: a lightweight, read-only freshness pass over the known
                # benches. This only runs when the containers are already up; the
                # running check never prompts or starts anything (prompt=False), so a
                # cache hit can never block on a "start the containers?" question or
                # disturb a stopped project. The pass never writes the cache: on no
                # drift we serve the cached data unchanged; on drift we fall through to
                # the full inspect (Tier 3), which persists fully-fresh data. If
                # anything goes wrong we degrade to the cached data rather than failing
                # a previously-working read.
                bench_instances_data = cached_benches
                try:
                    if ensure_containers_running(
                        project_name,
                        require_running=True,
                        verbose=verbose,
                        prompt=False,
                        auto_start=yes,
                    ):
                        all_containers = get_project_containers(project_name)
                        frappe_container = next(
                            (
                                c
                                for c in (all_containers or [])
                                if c.labels.get("com.docker.compose.service") == "frappe"
                            ),
                            None,
                        )
                        if frappe_container is not None:
                            _refreshed, drift = partial_inspect_known_benches(
                                frappe_container, cached_benches, verbose
                            )
                            if drift:
                                # Escalate-on-drift: a full inspect (Tier 3) also refreshes
                                # the deep per-site installed-app lists and any new bench.
                                # Remember the cached benches so the full inspect can
                                # degrade to them rather than hard-failing if the bench is
                                # no longer discoverable (e.g. its search path was removed).
                                if verbose:
                                    console_err.print(
                                        "VERBOSE: Partial inspect detected drift; "
                                        "escalating to a full inspect."
                                    )
                                bench_instances_data = None
                                drift_fallback_benches = cached_benches
                            elif verbose:
                                console_err.print(
                                    "VERBOSE: Partial inspect found no drift; "
                                    "serving cached data unchanged."
                                )
                    elif verbose:
                        console_err.print(
                            "VERBOSE: Containers not running; serving cached data as-is."
                        )
                except typer.Exit:
                    raise
                except Exception as e:
                    if verbose:
                        console_err.print(
                            f"VERBOSE: Partial inspect failed ({e}); serving cached data."
                        )
                    bench_instances_data = cached_benches
        else:
            if verbose:
                console_err.print("VERBOSE: No cached data found, proceeding with inspect.")
            bench_instances_data = None
    else:
        if verbose:
            console_err.print("VERBOSE: Update option is true, ignoring cache.")
        bench_instances_data = None

    if bench_instances_data is None:
        # Ensure containers are running. With prompt_to_start (the default) the user
        # is offered to start stopped containers; with --no-prompt-start (used by the
        # rm recache path, which runs under a spinner) we never prompt and instead
        # bail cleanly when nothing is running - inspecting a stopped bench is
        # impossible because every probe runs `exec_run` inside the container.
        if not ensure_containers_running(
            project_name,
            require_running=True,
            verbose=verbose,
            prompt=prompt_to_start,
            auto_start=yes,
        ):
            console_err.print(
                f"Error: Containers for project '{project_name}' are not running; "
                "cannot inspect a stopped project."
            )
            raise typer.Exit(code=1)

        all_containers = get_project_containers(project_name)
        if not all_containers:
            console_err.print(f"Error: No containers found for project '{project_name}'.")
            raise typer.Exit(code=1)

        frappe_container = next(
            (c for c in all_containers if c.labels.get("com.docker.compose.service") == "frappe"),
            None,
        )
        if not frappe_container:
            console_err.print(
                f"Error: No 'frappe' service container found for project '{project_name}'."
            )
            raise typer.Exit(code=1)

        bench_instances_data = []
        show_tips = config_utils.get_show_tips()
        no_benches = False

        with TipSpinner(f"Inspecting '{project_name}'", console=console_err, enabled=show_tips):
            time.sleep(0.1)
            bench_paths = _find_bench_instances(frappe_container, verbose)
            if not bench_paths:
                no_benches = True
            else:
                for bench_path in bench_paths:
                    bench_data = _gather_bench_data(frappe_container, bench_path, verbose)
                    bench_instances_data.append(bench_data)

        if no_benches:
            # A drift-escalation that can't rediscover the bench has a valid cache to
            # fall back on; degrade to it (without persisting) instead of failing a
            # previously-working read. A --update / cache-miss run has no such fallback,
            # so it keeps the original hard error.
            if drift_fallback_benches is not None:
                if verbose:
                    console_err.print(
                        "VERBOSE: Drift escalation found no discoverable benches; "
                        "serving cached data."
                    )
                bench_instances_data = drift_fallback_benches
            else:
                console_err.print(f"Error: No Bench Instances found for project '{project_name}'.")
                raise typer.Exit(code=1)
        else:
            db_utils.cache_project_data(project_name, bench_instances_data)

    # Interactive naming: ask for a user label per bench before output. Labels are
    # validated (no purely-numeric labels, no duplicates within the project, safe
    # charset) and persisted to BOTH the SQLite cache and the per-bench marker file
    # so they survive a cache wipe (see utils/bench_labels.py). Writing the marker
    # needs the running frappe container, so we fetch it here (a cache-served
    # inspect may not have one in scope yet).
    if interactive:
        interactive_container = None
        try:
            interactive_container = get_frappe_container(project_name)
        except typer.Exit:
            console_err.print(
                "[yellow]Warning:[/yellow] containers are not available; labels will be saved "
                "to the cache only (marker files not written)."
            )

        for index, bench in enumerate(bench_instances_data):
            existing = bench.get("label")
            existing_hint = f" [current: '{existing}']" if existing else ""
            try:
                answer = questionary.text(
                    f"Bench [{index}] at {bench['path']} on '{project_name}'.{existing_hint}\n"
                    "Label (blank to keep/clear, letters/digits/.-_ only): "
                ).ask()
                if answer is None:  # User pressed Ctrl+C
                    console_err.print("\n[yellow]Interactive naming cancelled.[/yellow]")
                    break
            except KeyboardInterrupt:
                console_err.print("\n[yellow]Interactive naming cancelled.[/yellow]")
                break
            except Exception as e:
                console_err.print(f"\n[red]Error during interactive input: {e}[/red]")
                continue

            new_label = answer.strip()
            if not new_label:
                # Blank keeps the existing label (numeric-index-only if none).
                continue

            error = bench_labels.validate_user_label(new_label)
            if error is None:
                # Duplicate check against the labels already chosen for other benches.
                duplicate = any(
                    other is not bench and other.get("label") == new_label
                    for other in bench_instances_data
                )
                if duplicate:
                    error = f"Label '{new_label}' is already used by another bench in this project."
            if error:
                console_err.print(f"[red]{error}[/red] Keeping the previous label.")
                continue

            bench["label"] = new_label
            if interactive_container is not None:
                if not bench_labels.write_label_marker(
                    interactive_container, bench["path"], new_label, verbose
                ):
                    console_err.print(
                        f"[yellow]Warning:[/yellow] could not write marker file for "
                        f"{bench['path']}; label saved to cache only."
                    )

        db_utils.cache_project_data(project_name, bench_instances_data)

    if json_output:
        # Surface the positional numeric index alongside any user label so scripts
        # can address a bench with --bench <index|label>.
        benches_out = [
            {"index": index, **bench_instance}
            for index, bench_instance in enumerate(bench_instances_data)
        ]
        result = {"project_name": project_name, "bench_instances": benches_out}
        print(json.dumps(result, indent=2))
    else:
        tree = Tree(f"Project [bold cyan]{project_name}[/bold cyan]", guide_style="bright_blue")
        for index, bench_instance in enumerate(bench_instances_data):
            # Show the numeric index (its default label) and any user label, so the
            # user knows exactly what to pass to --bench.
            path = bench_instance["path"]
            user_label = bench_instance.get("label")
            label_part = f" [magenta]'{user_label}'[/magenta]" if user_label else ""
            bench_node = tree.add(
                f"Bench [cyan]\\[{index}][/cyan]{label_part} at [green]{path}[/green]"
            )

            apps_branch = bench_node.add(
                f"Available Apps ({len(bench_instance['available_apps'])})"
            )
            for app in bench_instance["available_apps"]:
                apps_branch.add(f"[dim]{app}[/dim]")

            # Get default site from common config
            default_site = None
            if "common_site_config" in bench_instance:
                default_site = bench_instance["common_site_config"].get("default_site")

            sites_branch = bench_node.add(f"Sites ({len(bench_instance['sites'])})")
            for site_data in bench_instance["sites"]:
                site_name = site_data["name"]
                # Label default site
                if default_site and site_name == default_site:
                    site_label = f"[yellow]{site_name}[/yellow] [dim](default)[/dim]"
                else:
                    site_label = f"[yellow]{site_name}[/yellow]"

                site_node = sites_branch.add(site_label)
                installed_apps_node = site_node.add(
                    f"Installed Apps ({len(site_data['installed_apps'])})"
                )
                for app_name in site_data["installed_apps"]:
                    installed_apps_node.add(f"[green]{app_name}[/green]")

        console_out.print(tree)
