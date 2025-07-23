import typer
import docker
import json
import time
from rich.console import Console
from rich.tree import Tree
from typing import List, Optional, Tuple, Dict
from .utils import get_project_containers

console_out = Console()
console_err = Console(stderr=True)


def _run_command(
    container: docker.models.containers.Container,
    cmd: str,
    verbose: bool = False,
    workdir: Optional[str] = None
) -> Tuple[int, str]:
    if verbose:
        console_err.print(f"[dim]$ {cmd}[/dim]")
    exit_code, output = container.exec_run(cmd, workdir=workdir)
    stdout_bytes = output[0] if isinstance(output, tuple) else output
    decoded_output = stdout_bytes.decode('utf-8').strip()
    if verbose:
        console_err.print(f"[bold yellow]VERBOSE: Exit Code:[/bold yellow] {exit_code}")
        console_err.print(f"[bold yellow]VERBOSE: Output:[/bold yellow]\n---\n{decoded_output}\n---")
    return exit_code, decoded_output

def _is_bench_directory(container: docker.models.containers.Container, path: str, verbose: bool = False) -> bool:
    check_command = f'sh -c "test -d {path}/sites && test -d {path}/apps && test -f {path}/sites/common_site_config.json"'
    exit_code, _ = _run_command(container, check_command, verbose)
    return exit_code == 0

def _get_sites(container: docker.models.containers.Container, bench_dir: str, verbose: bool = False) -> List[str]:
    exit_code, output = _run_command(container, f"ls -1 {bench_dir}/sites", verbose)
    if exit_code != 0: return []
    excluded = {'apps.txt', 'assets', 'common_site_config.json', 'example.com', 'apps.json'}
    return [item for item in output.split('\n') if item and item not in excluded]

def _get_installed_apps(container: docker.models.containers.Container, bench_dir: str, site: str, verbose: bool = False) -> List[str]:
    cmd = f"bench --site {site} list-apps"
    exit_code, output = _run_command(container, cmd, verbose, workdir=bench_dir)
    if exit_code != 0: return [f"Error fetching apps for site {site}"]
    return [app for app in output.split('\n') if app]

def _get_available_apps(container: docker.models.containers.Container, bench_dir: str, verbose: bool = False) -> List[str]:
    exit_code, output = _run_command(container, f"ls -1 {bench_dir}/apps", verbose)
    if exit_code != 0: return []
    return [app for app in output.split('\n') if app]

def _find_bench_instances(container: docker.models.containers.Container, verbose: bool = False) -> List[str]:
    """Finds all potential bench directories within common search paths."""
    benches_found = []
    search_roots = ["/home/frappe", "/workspace/development"]
    for root in search_roots:
        if verbose: console_err.print(f"VERBOSE: Searching for benches in '{root}'...")
        # Find directories named 'apps' which are a reliable indicator of a bench's parent.
        find_cmd = f"find {root} -maxdepth 2 -type d -name 'apps'"
        exit_code, output = _run_command(container, find_cmd, verbose)
        if exit_code == 0:
            for path in output.strip().split('\n'):
                if path:
                    # The bench dir is the parent of the 'apps' dir
                    bench_dir = path.removesuffix('/apps')
                    if _is_bench_directory(container, bench_dir, verbose):
                        benches_found.append(bench_dir)
    return list(set(benches_found)) # Use set to ensure uniqueness

def _gather_bench_data(
    frappe_container: docker.models.containers.Container,
    bench_dir: str,
    verbose: bool
) -> Dict:
    """Gathers sites and apps for a single bench instance."""
    if verbose: console_err.print(f"VERBOSE: Inspecting Bench Instance: {bench_dir}")
    available_apps = _get_available_apps(frappe_container, bench_dir, verbose)
    sites = _get_sites(frappe_container, bench_dir, verbose)
    sites_info = []
    for site in sites:
        if verbose: console_err.print(f"VERBOSE:   - Found Site: {site}")
        installed_apps = _get_installed_apps(frappe_container, bench_dir, site, verbose)
        sites_info.append({"name": site, "installed_apps": installed_apps})
    
    return {
        "path": bench_dir,
        "sites": sites_info,
        "available_apps": available_apps
    }


def inspect(
    project_name: str = typer.Argument(..., help="The Docker Compose project to inspect."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose diagnostic output."),
    json_output: bool = typer.Option(False, "--json", help="Output the result as a JSON object.")
):
    """
    Inspects a Project to find all Bench Instances, Sites, and Apps within it.
    """
    if verbose: console_err.print(f"VERBOSE: --- Inspecting Project: {project_name} ---")

    all_containers = get_project_containers(project_name)
    if not all_containers:
        console_err.print(f"Error: No containers found for project '{project_name}'.")
        raise typer.Exit(code=1)
        
    frappe_container = next((c for c in all_containers if c.labels.get('com.docker.compose.service') == 'frappe'), None)
    if not frappe_container:
        console_err.print(f"Error: No 'frappe' service container found for project '{project_name}'.")
        raise typer.Exit(code=1)
        
    bench_instances_data = []
    with console_err.status(f"Inspecting '{project_name}'...", spinner="dots"):
        time.sleep(0.1)
        bench_paths = _find_bench_instances(frappe_container, verbose)
        if not bench_paths:
            console_err.print(f"Error: No Bench Instances found for project '{project_name}'.")
            raise typer.Exit(code=1)
            
        for bench_path in bench_paths:
            bench_data = _gather_bench_data(frappe_container, bench_path, verbose)
            bench_instances_data.append(bench_data)

    if json_output:
        result = { "project_name": project_name, "bench_instances": bench_instances_data }
        print(json.dumps(result, indent=2))
    else:
        tree = Tree(f"Project [bold cyan]{project_name}[/bold cyan]", guide_style="bright_blue")
        for bench_instance in bench_instances_data:
            bench_node = tree.add(f"Bench Instance at [green]{bench_instance['path']}[/green]")
            
            apps_branch = bench_node.add(f"Available Apps ({len(bench_instance['available_apps'])})")
            if verbose:
                for app in bench_instance['available_apps']:
                    apps_branch.add(f"[dim]{app}[/dim]")
            
            sites_branch = bench_node.add(f"Sites ({len(bench_instance['sites'])})")
            for site_data in bench_instance['sites']:
                site_node = sites_branch.add(f"[yellow]{site_data['name']}[/yellow]")
                installed_apps_node = site_node.add(f"Installed Apps ({len(site_data['installed_apps'])})")
                for app_name in site_data['installed_apps']:
                    installed_apps_node.add(f"[green]{app_name}[/green]")
        
        console_out.print(tree)