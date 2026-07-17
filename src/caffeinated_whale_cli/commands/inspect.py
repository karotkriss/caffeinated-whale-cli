"""The human ``inspect`` frontend: a renderer over ``core.inspect``.

The 3-tier freshness machine, the discovery/gather fan-out, and the cache write
all live in :mod:`caffeinated_whale_cli.core.inspect` (openspec
``migrate-inspect-core``). This module keeps only what is genuinely frontend:

- the backup pattern around the ``confirm_start`` fork: the core call runs under
  the ``TipSpinner``; when it returns ``NEEDS_CHOICE`` the choice is resolved
  interactively OUTSIDE the spinner (via ``ensure_containers_running``, which
  owns the prompt/refusal/actual start), then the core is re-invoked - capped at
  ONE re-invoke, so a start that claims success but leaves the container down
  fails closed instead of looping;
- the ``-v`` rendering of the core's typed events (today's ``VERBOSE:`` lines);
- the ``-i`` interactive labeling loop: it PROMPTS (frontend) and hands the
  collected answers to ``core.label.set_labels``, which owns the shared
  validate/uniqueness/marker/cache rule (one owner, no live Docker object here);
- the byte-identical ``--json`` and tree renderers, fed from
  ``core.inspect.inspect_raw``'s cache-shaped dicts (the typed ``InspectReport``
  deliberately cannot reproduce those bytes - key order and configs differ).
"""

import json

import questionary
import typer
from rich.console import Console
from rich.tree import Tree

from ..core import inspect as core_inspect
from ..core import label as core_label
from ..core.envelope import Status
from ..core.errors import CwcliError, ErrorKind
from ..utils import config_utils
from ..utils.completion_utils import complete_project_names
from ..utils.docker_utils import handle_docker_errors
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running

console_out = Console()
console_err = Console(stderr=True)


def _render_event(event: core_inspect.InspectEvent, verbose: bool) -> None:
    """Render a core inspect event as today's stderr diagnostics.

    ``InspectWarning`` renders unconditionally (e.g. a failed ``bench list-apps``);
    everything else is ``-v``-gated, matching the pre-migration output.
    """
    if isinstance(event, core_inspect.InspectWarning):
        console_err.print(f"[yellow]Warning:[/yellow] {event.text}")
    elif not verbose:
        return
    elif isinstance(event, core_inspect.InspectCommand):
        console_err.print(f"[dim]$ {event.command}[/dim]")
    elif isinstance(event, core_inspect.InspectCommandDone):
        console_err.print(f"[bold yellow]VERBOSE: Exit Code:[/bold yellow] {event.exit_code}")
        console_err.print(f"[bold yellow]VERBOSE: Output:[/bold yellow]\n---\n{event.output}\n---")
    elif isinstance(event, core_inspect.InspectTrace):
        console_err.print(f"VERBOSE: {event.text}")


def render_error_exit(project_name: str, error: CwcliError) -> typer.Exit:
    """Map a core inspect error to today's exact stderr line, returning the Exit to raise.

    Shared (not module-private) because every no-cache fallback populate that
    calls ``core_inspect.inspect`` directly (open/update/restore) needs the same
    pre-migration abort-on-hard-failure rendering, not just this command.
    """
    if error.kind is ErrorKind.NOT_RUNNING:
        console_err.print(
            f"Error: Containers for project '{project_name}' are not running; "
            "cannot inspect a stopped project."
        )
    elif error.code == "bench.none_found":
        console_err.print(f"Error: {error.message}")
    else:
        console_err.print(f"[bold red]Error:[/bold red] {error.message}")
    return typer.Exit(code=1)


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

    refresh = "full" if update else ("cache_only" if no_refresh else "auto")

    def on_event(event: core_inspect.InspectEvent) -> None:
        _render_event(event, verbose)

    show_tips = config_utils.get_show_tips()
    started = False
    while True:
        try:
            with TipSpinner(f"Inspecting '{project_name}'", console=console_err, enabled=show_tips):
                result = core_inspect.inspect_raw(
                    project_name,
                    refresh=refresh,
                    offer_choice=prompt_to_start,
                    on_event=on_event,
                )
        except CwcliError as e:
            raise render_error_exit(project_name, e) from None

        if result.status is not Status.NEEDS_CHOICE:
            break

        # confirm_start (the only fork inspect can return): resolve it OUTSIDE the
        # spinner - ensure_containers_running owns the prompt, the non-TTY refusal,
        # --yes auto-start, and the actual UI-coupled container start. Capped at ONE
        # re-invoke: a second confirm_start after a claimed start means the start
        # did not take (crash loop / teardown race), so fail closed.
        if started or not ensure_containers_running(
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
        started = True

    assert result.data is not None  # OK/WARNING always carries a RawInspect
    bench_instances_data = result.data.benches

    # Interactive naming: ask for a user label per bench before output. The
    # prompting is frontend; the validate/uniqueness/marker/cache RULE belongs to
    # core.label.set_labels (the batched sibling of `set_label`), so a future rule
    # change applies here too. We collect the raw answers, then apply them in one
    # call; core owns the running-container fetch (degrading to cache-only when the
    # project is stopped), so no live Docker object crosses the core boundary.
    if interactive:
        assignments: list[tuple[str, str]] = []
        for index, bench in enumerate(bench_instances_data):
            existing = bench.get("label")
            existing_hint = f" [current: '{existing}']" if existing else ""
            try:
                answer = questionary.text(
                    f"Bench [{index}] at {bench['path']} on '{project_name}'.{existing_hint}\n"
                    "Label (blank to keep, letters/digits/.-_ only): "
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

            if answer.strip():  # Blank keeps the existing label.
                assignments.append((bench["path"], answer))

        label_result = core_label.set_labels(project_name, assignments)
        for warning in label_result.warnings:
            console_err.print(f"[yellow]Warning:[/yellow] {warning.text}")
        # Reflect outcomes into the dicts the tree/JSON renderer below reads, and
        # report each rejection with today's wording.
        applied_by_path = {b["path"]: b for b in bench_instances_data}
        for outcome in label_result.data or []:
            if outcome.error:
                console_err.print(f"[red]{outcome.error}[/red] Keeping the previous label.")
                continue
            if outcome.applied:
                applied_by_path[outcome.bench_path]["label"] = outcome.label
                if not outcome.marker_written and not label_result.warnings:
                    console_err.print(
                        f"[yellow]Warning:[/yellow] could not write marker file for "
                        f"{outcome.bench_path}; label saved to cache only."
                    )

    if json_output:
        # Surface the positional numeric index alongside any user label so scripts
        # can address a bench with --bench <index|label>.
        benches_out = [
            {"index": index, **bench_instance}
            for index, bench_instance in enumerate(bench_instances_data)
        ]
        result_doc = {"project_name": project_name, "bench_instances": benches_out}
        print(json.dumps(result_doc, indent=2))
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

            # Resolve the default site from EITHER common_site_config.json's
            # `default_site` OR the currentsite.txt pointer (recorded as
            # `current_site`). A plain `bench use`d dev bench only has the latter.
            default_site = None
            if "common_site_config" in bench_instance:
                default_site = bench_instance["common_site_config"].get("default_site")
            if not default_site:
                default_site = bench_instance.get("current_site")

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
