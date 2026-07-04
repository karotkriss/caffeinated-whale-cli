"""The ``label`` command: assign, clear, or list per-bench user labels.

A user label is a durable, human-friendly handle for a bench in a multi-bench
project (numeric indices are positional and can shift). Setting a label writes it
to BOTH the SQLite cache and the per-bench marker file
``<bench-root>/.cwcli/.bench-label``, so it survives a cache wipe and can be
rebuilt by ``inspect`` from the live bench. See ``utils/bench_labels.py`` for the
label model and marker format.
"""

import typer

from ..utils import bench_labels, db_utils
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import get_frappe_container, handle_docker_errors


def _print_bench_list(project_name: str, benches: list[dict]) -> None:
    console.print(f"Benches in project [bold cyan]{project_name}[/bold cyan]:")
    for index, bench in enumerate(benches):
        label = bench.get("label")
        label_part = f" [magenta]'{label}'[/magenta]" if label else " [dim](no label)[/dim]"
        console.print(f"  [cyan]\\[{index}][/cyan]{label_part}  {bench['path']}")


@handle_docker_errors
def label(
    project_name: str = typer.Argument(
        ..., help="The Docker Compose project name.", autocompletion=complete_project_names
    ),
    bench_selector: str = typer.Argument(
        None,
        help="Which bench to label: its numeric index or an existing label. Omit to list benches.",
    ),
    new_label: str = typer.Argument(
        None,
        help="The new label to assign. Omit and pass --clear to remove the label.",
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the selected bench's user label (revert to numeric index)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
):
    """
    Assign, clear, or list per-bench labels for a project.

    Examples:

        cwcli label my-project                 # list benches with their indices/labels

        cwcli label my-project 1 staging       # label bench index 1 as 'staging'

        cwcli label my-project staging prod     # rename label 'staging' to 'prod'

        cwcli label my-project 1 --clear       # remove bench 1's label
    """
    cached_data = db_utils.get_cached_project_data(project_name)
    benches = (cached_data or {}).get("bench_instances") or []

    if not benches:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No cached benches for project '{project_name}'. "
            f"Run 'cwcli inspect {project_name}' first."
        )
        raise typer.Exit(code=1)

    # No selector -> list mode (read-only, no container needed).
    if bench_selector is None:
        _print_bench_list(project_name, benches)
        return

    chosen = bench_labels.resolve_bench(benches, bench_selector)
    if chosen is None:
        stderr_console.print(
            f"[bold red]Error:[/bold red] No bench '{bench_selector}' in project '{project_name}'."
        )
        stderr_console.print("Available benches (address by index or label):")
        stderr_console.print(bench_labels.format_bench_list(benches))
        raise typer.Exit(code=1)

    if not clear:
        if new_label is None:
            stderr_console.print(
                "[bold red]Error:[/bold red] Provide a new label, or pass --clear to remove one."
            )
            raise typer.Exit(code=1)
        new_label = new_label.strip()
        error = bench_labels.validate_user_label(new_label)
        if error is None:
            duplicate = any(
                other is not chosen and other.get("label") == new_label for other in benches
            )
            if duplicate:
                error = f"Label '{new_label}' is already used by another bench in this project."
        if error:
            stderr_console.print(f"[bold red]Error:[/bold red] {error}")
            raise typer.Exit(code=1)

    # Both set and clear must write the marker inside the container, so the bench
    # must be reachable. Do NOT auto-start; a label change should not spin up a
    # stopped project.
    frappe_container = get_frappe_container(project_name)
    frappe_container.reload()
    if frappe_container.status != "running":
        stderr_console.print(
            f"[bold red]Error:[/bold red] Frappe container for '{project_name}' is not running. "
            "Start the project first - the label marker is stored inside the bench."
        )
        raise typer.Exit(code=1)

    bench_path = chosen["path"]

    if clear:
        # Clear the marker FIRST and only touch the DB on success. The marker is
        # the source of truth for label recovery, so clearing the DB while the
        # marker survives would let a later full inspect resurrect the old label.
        marker_ok = bench_labels.clear_label_marker(frappe_container, bench_path, verbose)
        if not marker_ok:
            stderr_console.print(
                "[bold red]Error:[/bold red] could not remove the marker file inside the "
                "container; label left unchanged to avoid the marker resurrecting it later."
            )
            raise typer.Exit(code=1)
        if not db_utils.set_bench_label(project_name, bench_path, None):
            raise typer.Exit(code=1)
        console.print(f"[bold green]✓[/bold green] Cleared label for bench at {bench_path}.")
    else:
        marker_ok = bench_labels.write_label_marker(
            frappe_container, bench_path, new_label, verbose
        )
        if not marker_ok:
            stderr_console.print(
                "[bold red]Error:[/bold red] Failed to write the marker file inside the "
                "container; label not saved."
            )
            raise typer.Exit(code=1)
        db_utils.set_bench_label(project_name, bench_path, new_label)
        console.print(
            f"[bold green]✓[/bold green] Labeled bench at {bench_path} as "
            f"[magenta]'{new_label}'[/magenta]."
        )

    # Show the refreshed list so the user sees the result.
    refreshed = db_utils.get_cached_project_data(project_name)
    _print_bench_list(project_name, (refreshed or {}).get("bench_instances") or [])
