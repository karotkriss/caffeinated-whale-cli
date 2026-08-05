"""``cwcli init`` - the renderer over the ``core.init`` two-call slice.

The logic lives in :mod:`..core.init` (``init_instance`` then ``init_bench``);
everything here is presentation and TTY-coupled UX: the Typer signature, the
``--frappe-branch``/``--version`` flag fusion, the project-name prompt, the
admin-password generate-or-refuse gate (a generated secret must never land in
a captured log), spinners driven by the core's typed events, the resolution of
the three choice surfaces (``confirm_start`` on both stages, the
``confirm_reuse_bench`` prompt-and-rename loop), and the success/password
block.

The site's administrator password is generated and printed once when
``--admin-password`` is omitted in an interactive run; a non-interactive run
must pass ``--admin-password``. A supplied value is used verbatim.
"""

import secrets
import sys
import time
from collections.abc import Callable

import questionary
import typer

from ..core import docker as core_docker
from ..core import init as core_init
from ..core import resolvers as core_resolvers
from ..core import start as core_start
from ..core.envelope import Choice, Status
from ..core.errors import CwcliError
from ..utils import cache, config_utils
from ..utils.completion_utils import complete_project_names
from ..utils.console import console, stderr_console
from ..utils.docker_utils import handle_docker_errors
from ..utils.tips import TipSpinner
from .utils import ensure_containers_running

# The execs whose output the verbose renderer writes raw (the streamed ones);
# the short set-config/probe execs never echoed their command or output, and
# still don't.
_LONG_PHASES = {"bench_init", "new_site", "erpnext_get", "erpnext_install"}
_OUTPUT_PHASES = _LONG_PHASES | {"pull", "up"}

# Which TipSpinner a phase runs under (non-verbose). One spinner per group,
# relabeled lazily on the group boundary - this is what collapsed the old four
# dual verbose/non-verbose blocks into one event-driven renderer.
_SPINNER_GROUP = {
    "project_dir": "stage1",
    "customize_ports": "stage1",
    "resolve_bench_tag": "stage1",
    "pull": "stage1",
    "up": "stage1",
    "wait_ready": "stage1",
    # align_uid starts stage 2 after stage 1 closes its spinner, so it needs
    # a group of its own to keep progress visible during the blocking alignment.
    "align_uid": "align_uid",
    "python_install": "python_install",
    "node_install": "node_install",
    "bench_init": "bench_init",
    "configure_bench": "configure_bench",
    "new_site": "new_site",
    "finalize": "finalize",
    "erpnext_get": "erpnext",
    "erpnext_install": "erpnext",
}


class _InitRenderer:
    """Renders ``core.init``'s typed events in this CLI's historical style.

    Non-verbose runs each phase group under a ``TipSpinner`` whose label and
    updates reproduce the old hardcoded spinner texts; verbose prints the old
    ``[dim]`` traces, the ``$ command`` echo (long execs only, as before), and
    the raw exec output with its stdout/stderr split and carriage returns
    preserved. Prompts never run while a spinner is live: the core returns
    choices instead of prompting, and the command resolves them only after
    :meth:`close` has run.
    """

    def __init__(self, *, verbose: bool, show_tips: bool, project: str):
        self.verbose = verbose
        self.show_tips = show_tips
        self.project = project
        self.bench_name: str | None = None
        self.site_name: str | None = None
        self._spinner: TipSpinner | None = None
        self._group: str | None = None
        self._phase: str | None = None

    def _group_label(self, group: str) -> str:
        return {
            "stage1": f"Setting up project '{self.project}'",
            "align_uid": f"Preparing bench workspace for '{self.project}'",
            "python_install": f"Installing Python for bench '{self.bench_name}'",
            "node_install": f"Installing Node.js for bench '{self.bench_name}'",
            "bench_init": f"Initializing bench '{self.bench_name}'",
            "configure_bench": f"Configuring bench '{self.bench_name}'",
            "new_site": f"Creating site '{self.site_name}'",
            "finalize": f"Finalizing setup for '{self.site_name}'",
            "erpnext": "Installing ERPNext",
        }[group]

    def close(self) -> None:
        """Stop any live spinner; called after every core call returns so the
        prompts that resolve a returned choice own the terminal."""
        if self._spinner is not None:
            self._spinner.__exit__(None, None, None)
            self._spinner = None
        self._group = None

    def __call__(self, event) -> None:
        if isinstance(event, core_init.InitStepStart):
            self._on_start(event)
        elif isinstance(event, core_init.InitOutput):
            self._on_output(event)
        elif isinstance(event, core_init.InitStepEnd):
            self._phase = None
        elif isinstance(event, core_init.InitNotice):
            self._on_notice(event)
        elif isinstance(event, core_init.InitTrace):
            self._on_trace(event)

    def _on_start(self, event) -> None:
        self._phase = event.phase
        if self.verbose:
            # The old verbose narration: dim status lines for the customization
            # steps, a blank stdout line before each streamed exec.
            if event.phase == "customize_ports":
                stderr_console.print(f"[dim]{event.message}[/dim]")
            elif event.phase in ("resolve_bench_tag", "pull"):
                stderr_console.print(f"[dim]{event.message}...[/dim]")
            elif event.phase in _LONG_PHASES:
                console.print()
            elif event.message:
                # A message-bearing phase must remain visible even when it has
                # no specialized verbose renderer.
                stderr_console.print(f"[dim]{event.message}[/dim]")
            return

        group = _SPINNER_GROUP[event.phase]
        if group != self._group:
            self.close()
            self._group = group
            self._spinner = TipSpinner(
                self._group_label(group), console=stderr_console, enabled=self.show_tips
            )
            self._spinner.__enter__()
        if event.message and self._spinner is not None:
            self._spinner.update(event.message)

    def _on_output(self, event) -> None:
        if not self.verbose or event.phase not in _OUTPUT_PHASES:
            return
        # Raw writes, not console.print: this is the command's own output, and
        # rich would re-wrap it and eat the carriage returns its progress bars
        # depend on. The stdout/stderr split is preserved.
        stream = sys.stdout if event.stream == "stdout" else sys.stderr
        stream.write(event.text)
        stream.flush()

    def _on_notice(self, event) -> None:
        code = event.code
        if code == "search_path.added":
            console.print(f"[green]Added '{event.text}' to custom search paths.[/green]")
        elif code == "search_path.exists":
            console.print(f"[yellow]'{event.text}' already exists in custom search paths.[/yellow]")
        elif code in ("python.installing", "node.installing"):
            if not self.verbose:
                stderr_console.print(f"[yellow]{event.text}[/yellow]")
        elif code in ("python.install_failed", "node.install_failed"):
            stderr_console.print(f"[bold red]Error:[/bold red] {event.text}")
        elif code in ("yarn.install_failed", "setuptools.pin_failed", "init.uid_align_failed"):
            stderr_console.print(f"[yellow]Warning: {event.text}[/yellow]")
        elif code == "instance.already_running" and self.verbose:
            stderr_console.print(f"[dim]{event.text}[/dim]")
        elif code == "ports.retry":
            # Unmissable in both rendering modes: a caller waiting on this run
            # needs to know it's retrying, not stuck.
            stderr_console.print(f"[yellow]{event.text}[/yellow]")

    def _on_trace(self, event) -> None:
        if not self.verbose:
            return
        if event.code == "exec.command":
            # The command echo, exactly where the old `$ {command}` printed:
            # long execs only - the short set-configs never echoed.
            if self._phase in _LONG_PHASES:
                stderr_console.print(f"[dim]$ {event.text}[/dim]")
            return
        stderr_console.print(f"[dim]{event.text}[/dim]")


def _render_error_exit(e: CwcliError, project_name: str, *, verbose: bool = False) -> typer.Exit:
    """Map a typed core error to this CLI's historical stderr lines + exit 1."""
    stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
    if e.code == "ports.in_use":
        stderr_console.print(
            "\n[yellow]Tip:[/yellow] If an instance using these ports was just removed, "
            "they may still be releasing - wait a few seconds and retry."
        )
        stderr_console.print(
            "[yellow]Tip:[/yellow] Otherwise, use the [cyan]--port[/cyan] flag to select "
            "a different starting port."
        )
        stderr_console.print(f"[dim]Example: cwcli init {project_name} --port 10000[/dim]")
    elif e.code == "compose.failed":
        # Verbose mode already streamed this stderr live via InitOutput events
        # (_InitRenderer._on_output); printing e.detail["output"] here too would
        # duplicate it. Only non-verbose mode (which drops InitOutput events)
        # needs it printed here.
        if not verbose:
            output = (e.detail or {}).get("output")
            if output:
                stderr_console.print(output)
    elif (
        e.code in ("exec.stream_lost", "exec.exit_code_unknown", "bench_parent.mismatch") and e.hint
    ):
        # The contract's honest lost-stream errors are new on this surface;
        # their hint says what the user should actually do. Same for the
        # frozen-compose --bench-parent mismatch guard's remedy hint.
        stderr_console.print(f"[dim]{e.hint}[/dim]")
    return typer.Exit(code=1)


def _validate_or_exit(validator: Callable[[str], str], value: str) -> str:
    """Run a core validator, mapping its typed USAGE error to today's print+exit."""
    try:
        return validator(value)
    except CwcliError as e:
        stderr_console.print(f"[bold red]Error:[/bold red] {e.message}")
        raise typer.Exit(code=1) from None


def _prompt_for_inputs(
    project_name: str | None, bench_name: str, site_name: str
) -> tuple[str, str, str]:
    """Prompt the user for the project name if missing, then validate all three
    names up front (the fail-fast ordering the old code had)."""
    try:
        if project_name is None:
            container_answer = questionary.text(
                "Docker project / container prefix (e.g. frappe-app)",
                default="",
                validate=lambda text: bool(text.strip()),
            ).ask()
            if container_answer is None:
                raise typer.Exit(code=0)
        else:
            container_answer = project_name
    except KeyboardInterrupt:
        stderr_console.print("\n[yellow]Operation cancelled.[/yellow]")
        raise typer.Exit(code=0) from None

    return (
        _validate_or_exit(core_init.validate_project_slug, container_answer),
        _validate_or_exit(core_init.validate_bench_slug, bench_name),
        _validate_or_exit(core_init.validate_new_site_name, site_name),
    )


def _generate_admin_password() -> str:
    """Generate a strong, shell-safe admin password.

    ``token_urlsafe`` yields ``[A-Za-z0-9_-]`` only, so it never needs quoting.
    """
    return secrets.token_urlsafe(18)


def _is_interactive_session() -> bool:
    """True only for a real interactive terminal (both stdin and stdout TTYs).

    Gates whether a generated admin password may be shown to a human. If either
    stream is redirected the password would leak into a captured log, so init
    refuses instead and requires ``--admin-password``.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _bench_name_validation(value: str) -> bool | str:
    """Validate a replacement bench name typed at the reuse-decline prompt.

    Returns ``True`` when valid, or an error string so questionary re-prompts in
    place. A blank value is treated as valid so the caller's cancel path can
    handle it (leaving the prompt blank cancels). The non-blank rules match the
    core's ``validate_bench_slug``.
    """
    cleaned = value.strip()
    if not cleaned:
        return True

    lowered = cleaned.lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not all(char in allowed for char in lowered):
        return "Bench name must contain only lowercase letters, numbers, dashes, or underscores."
    if lowered[0] in "-_" or lowered[-1] in "-_":
        return "Bench name cannot start or end with '-' or '_'."
    return True


def _resolve_frappe_branch(frappe_branch: str | None, version: str | None) -> str:
    """Resolve the effective Frappe git ref from the mutually-exclusive
    ``--frappe-branch`` / ``--version`` flags.

    The flag fusion and mutual-exclusion error stay HERE (flag UX belongs to
    one frontend); the shape resolution itself is the core's
    :func:`~..core.init.resolve_frappe_ref`. Defaults to
    ``core.init.DEFAULT_FRAPPE_BRANCH`` when neither is given.
    """
    if frappe_branch is not None and version is not None:
        stderr_console.print(
            "[bold red]Error:[/bold red] --frappe-branch and --version are "
            "mutually exclusive; pass only one."
        )
        raise typer.Exit(code=1)
    if version is not None:
        try:
            return core_init.resolve_frappe_ref(version)
        except CwcliError as exc:
            stderr_console.print(f"[bold red]Error:[/bold red] {exc.message}")
            raise typer.Exit(code=1) from None
    if frappe_branch is not None:
        return frappe_branch
    return core_init.DEFAULT_FRAPPE_BRANCH


def _refresh_cache(project: str, verbose: bool) -> None:
    """Populate the bench cache right after a fresh bench is created.

    ``init_bench`` only CLEARS the project's cache (a stale entry would be
    worse than none); nothing else repopulates it. Every bench-resolving verb
    (``status``, ``run``, ``apps``, ...) falls back to a hardcoded default
    bench path when the cache is empty, which only happens to match a bench
    built under the DEFAULT ``--bench-parent`` - a custom parent's very first
    post-init command would silently resolve the wrong path. Mirrors
    ``apps.py``'s post-mutation ``_refresh_cache``: degrades to a warning,
    never fails init (the bench itself already succeeded).
    """
    if not cache.recache_project(project, verbose=verbose):
        stderr_console.print(
            "[yellow]Warning:[/yellow] bench created, but caching its bench path failed; "
            "run 'cwcli inspect --update' to refresh."
        )


def _bench_web_url(project: str, bench_path: str, site: str) -> str | None:
    """The new bench's REAL host URL for the success banner, or None.

    The banner used to hardcode ``http://{site}:8000``, which is wrong twice over on
    any real instance: 8000 is a CONTAINER port (a ``--port 21000`` instance
    publishes it as 21000), and every bench past the first serves its own
    ``make_ports`` assignment (bench 1 is 8001 -> 21001). Both hops now come from
    ``resolvers.resolve_host_web_url``, the one reader of that question. Degrades to
    None - and the banner then simply omits the line - rather than printing an
    address the user cannot reach.
    """
    try:
        container = core_docker.get_frappe_container(project)
        return core_resolvers.resolve_host_web_url(container, bench_path, site=site)
    except Exception:  # noqa: BLE001 - a banner hint must never fail a successful init
        return None


def _start_services(project: str, bench_path: str) -> tuple[bool, bool | None]:
    """Start the just-created bench's dev services over ``core.start``.

    Reuses the ``cwcli start`` path (``core.start``) rather than reinventing the
    supervisord launch. The bench path is known exactly (``report.bench_path``),
    so it is passed verbatim and ``core.start`` never forks to a multi-bench
    choice. Returns ``(running, web_ready)``: ``running`` is True when the launch
    itself succeeded; a failure degrades to a warning (init already succeeded at
    creating the bench) and returns ``(False, None)``. ``web_ready`` mirrors
    ``core.start``'s own honest signal (True served / False timed out / None not
    probed) so the caller can avoid claiming "running" on a web-readiness timeout.
    """
    try:
        with stderr_console.status(
            f"[bold green]Starting dev services for '{project}'...[/bold green]", spinner="dots"
        ):
            result = core_start.start(project, bench_path=bench_path)
    except Exception as e:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] Bench created, but its dev services could not be "
            f"started: {getattr(e, 'message', str(e))}"
        )
        return False, None
    # core.start now blocks until the web server binds this bench's assigned port, so "running" is
    # honest by the time we return. If it timed out, or the port never got published
    # to the host at all, surface the warning so the user isn't told the web is up
    # when it isn't reachable.
    for warning in result.warnings:
        if warning.code in ("start.web_not_ready", "start.no_host_port"):
            stderr_console.print(f"[yellow]Warning:[/yellow] {warning.text}")
    running = result.status is not Status.NEEDS_CHOICE
    web_ready = result.data.web_ready if running and result.data is not None else None
    return running, web_ready


def _resolve_reuse_choice(choice: Choice, bench_name: str) -> tuple[str, bool | None]:
    """Resolve the ``confirm_reuse_bench`` choice exactly as the old resolver did.

    On a TTY: today's reuse confirm (``auto_enter=False``); Yes means reuse
    (returns ``reuse_bench=True``); No prompts for a replacement name and
    returns it with ``reuse_bench=None``, so an also-existing name surfaces the
    choice again - the rename loop is the natural fixpoint of re-invocation
    (issue #20's decline-must-continue flow). A cancelled prompt or a blank
    name exits 0 with "No changes made.", the deliberate interactive-cancel.
    On a non-TTY: the honest exit-1 refusal naming both flags (issue #41),
    never a hang or an ``EOFError`` inside questionary.
    """
    bench_full_path = (choice.options or [{}])[0].get("label", bench_name)

    if not sys.stdin.isatty():
        stderr_console.print(
            f"[bold red]Error:[/bold red] Bench '{bench_full_path}' already exists and this is "
            "a non-interactive session. Pass --reuse-bench to reuse it, or --no-reuse-bench "
            "with a different --bench name to create a fresh bench."
        )
        raise typer.Exit(code=1)

    console.print(f"[yellow]Bench '{bench_name}' already exists at {bench_full_path}.[/yellow]")
    reuse = questionary.confirm(choice.prompt, default=True, auto_enter=False).ask()
    if reuse is None:
        # Prompt cancelled (e.g. Ctrl-C).
        console.print("[yellow]No changes made.[/yellow]")
        raise typer.Exit(code=0)
    if reuse:
        return bench_name, True

    # The user declined to reuse the existing bench. Let them name a different
    # bench and continue setup instead of dead-ending.
    new_name = questionary.text(
        "Enter a different bench name to create (leave blank to cancel):",
        default="",
        validate=_bench_name_validation,
    ).ask()
    if not new_name or not new_name.strip():
        console.print("[yellow]No changes made.[/yellow]")
        raise typer.Exit(code=0)

    return new_name.strip().lower(), None


@handle_docker_errors
def init(
    project_name: str | None = typer.Argument(
        None,
        help="Docker Compose project name. If not provided, will prompt interactively.",
        autocompletion=complete_project_names,
    ),
    port: int = typer.Option(
        8000,
        "--port",
        "-P",
        help="Starting port for the project. Creates ports {port}-{port+5} for web servers and {port+1000}-{port+1005} for socketio.",
    ),
    bench_name: str = typer.Option(
        "frappe-bench",
        "--bench",
        "-b",
        help="Bench directory to create inside the container. Defaults to 'frappe-bench'.",
    ),
    site_name: str = typer.Option(
        "development.localhost",
        "--site",
        "-s",
        help="Primary site to create (must end with .localhost). Defaults to 'development.localhost'.",
    ),
    bench_parent: str = typer.Option(
        "/workspace",
        "--bench-parent",
        help="Directory inside the container where the bench will be created.",
    ),
    frappe_branch: str | None = typer.Option(
        None,
        "--frappe-branch",
        help=(
            "Frappe branch or tag for bench init (e.g. version-16 or v16.26.3). "
            "Mutually exclusive with --version. Default: version-16."
        ),
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        help=(
            "Frappe version for bench init, resolved by shape: a bare major "
            "(16 -> version-16 branch) or a full semantic version "
            "(16.26.3 -> v16.26.3 tag). Mutually exclusive with --frappe-branch."
        ),
    ),
    db_root_password: str = typer.Option(
        "123",
        "--db-root-password",
        help="MariaDB root password used for bench new-site.",
    ),
    admin_password: str | None = typer.Option(
        None,
        "--admin-password",
        help=(
            "Administrator password for the new site, used verbatim (no strength "
            "check). If omitted, a strong password is generated and printed once "
            "in an interactive run; a non-interactive run must supply this flag."
        ),
    ),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help="Automatically start containers if they are not running.",
    ),
    start_services: bool = typer.Option(
        True,
        "--start/--no-start",
        help=(
            "After creating the bench+site, start its dev services (supervisord over "
            "'bench start') so init leaves a running dev environment. --no-start creates "
            "without starting, for automation/CI. Distinct from --auto-start, which only "
            "controls Docker container startup."
        ),
    ),
    reuse_bench: bool | None = typer.Option(
        None,
        "--reuse-bench/--no-reuse-bench",
        help=(
            "When the target bench directory already exists: --reuse-bench reuses it "
            "(skips bench init), --no-reuse-bench requires a fresh --bench name and errors "
            "if it exists. Default: ask interactively (refuse on a non-TTY). Distinct from "
            "--auto-start, which controls container startup."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show verbose docker exec output for quick commands.",
    ),
    install_erpnext: bool = typer.Option(
        False,
        "--install-erpnext",
        help="Install the ERPNext application onto the created site after initialization.",
    ),
    erpnext_branch: str = typer.Option(
        "version-16",
        "--erpnext-branch",
        help="ERPNext branch to use when fetching the app (used with --install-erpnext).",
    ),
) -> None:
    """
    Initialize a new Frappe project with bench and site.

    Creates project directory, downloads compose files, starts containers,
    initializes bench, and creates a site. Optionally installs ERPNext.

    If project_name is not provided, prompts interactively.

    The site's administrator password is generated and printed once when
    --admin-password is omitted in an interactive run; a non-interactive run
    must pass --admin-password. A supplied value is used verbatim.

    Examples:
        cwcli init
        cwcli init my-project
        cwcli init my-project --bench my-bench --site mysite.localhost
        cwcli init my-project --version 16 --install-erpnext
        cwcli init my-project --version 16.26.3
        cwcli init my-project --frappe-branch version-16 --admin-password mypass
    """
    # Resolve the Frappe git ref first so a malformed --version fails fast,
    # before any project dir / container work.
    frappe_branch = _resolve_frappe_branch(frappe_branch, version)

    # Resolve the admin password before any container work so a missing one
    # fails fast. A supplied value is used verbatim (bench is the only backstop);
    # when omitted, an interactive run gets a generated one (printed once at the
    # end), while a non-interactive run refuses rather than leak a generated
    # secret into a captured log.
    admin_password_generated = False
    if admin_password is None:
        if _is_interactive_session():
            admin_password = _generate_admin_password()
            admin_password_generated = True
        else:
            stderr_console.print(
                "[bold red]Error:[/bold red] No --admin-password supplied and this is a "
                "non-interactive session. Pass --admin-password to set the site's "
                "administrator password."
            )
            raise typer.Exit(code=1)

    start_time = time.time()

    # Prompt for the project name if not provided; validate all three names up
    # front (fail-fast ordering preserved - a bad site name fails before ANY work).
    project, bench, site = _prompt_for_inputs(project_name, bench_name, site_name)

    renderer = _InitRenderer(
        verbose=verbose, show_tips=config_utils.get_show_tips(), project=project
    )

    if verbose:
        console.print()

    # ---- Stage 1: the instance (project dir, compose, containers up).
    # Always called with auto_start=False first; on a poll timeout the prompt or
    # refusal runs OUTSIDE any spinner via ensure_containers_running (today's
    # exact prompt/refusal/start path), then ONE re-invoke with auto_start=True.
    # A second failure is the core's typed NOT_RUNNING - the structural cap.
    try:
        try:
            result = core_init.init_instance(
                project,
                port=port,
                bench_parent=bench_parent,
                stream_output=verbose,
                on_event=renderer,
            )
        finally:
            renderer.close()
        if result.status is Status.NEEDS_CHOICE:
            ensure_containers_running(project, require_running=True, auto_start=auto_start)
            try:
                core_init.init_instance(
                    project,
                    port=port,
                    bench_parent=bench_parent,
                    auto_start=True,
                    stream_output=verbose,
                    on_event=renderer,
                )
            finally:
                renderer.close()
    except CwcliError as e:
        raise _render_error_exit(e, project, verbose=verbose) from None

    # ---- Stage 2: the bench + site, re-invoked per resolved choice.
    renderer.bench_name = bench
    renderer.site_name = site
    started = False
    reuse_answer = reuse_bench
    while True:
        try:
            try:
                bench_result = core_init.init_bench(
                    project,
                    bench_name=bench,
                    site_name=site,
                    bench_parent=bench_parent,
                    frappe_ref=frappe_branch,
                    db_root_password=db_root_password,
                    admin_password=admin_password,
                    reuse_bench=reuse_answer,
                    install_erpnext=install_erpnext,
                    erpnext_branch=erpnext_branch,
                    auto_start=auto_start,
                    stream_output=verbose,
                    on_event=renderer,
                )
            finally:
                renderer.close()
        except CwcliError as e:
            raise _render_error_exit(e, project, verbose=verbose) from None

        if bench_result.status is not Status.NEEDS_CHOICE:
            break
        choice = bench_result.choice
        assert choice is not None  # NEEDS_CHOICE always carries a Choice

        if choice.kind == "confirm_start":
            # The race backstop: re-invoke at most ONCE after an attempted
            # start (the backup.py/run.py capped pattern). A second
            # confirm_start after ensure_containers_running claimed success
            # means the start didn't take - fail closed, don't spin.
            if started:
                stderr_console.print(
                    "[bold red]Error:[/bold red] Frappe container for project "
                    f"'{project}' failed to start."
                )
                raise typer.Exit(code=1)
            ensure_containers_running(project, require_running=True, auto_start=auto_start)
            started = True
            continue

        # confirm_reuse_bench: prompt (or refuse on a non-TTY), then re-invoke.
        # A decline-and-rename round costs only subsecond probes.
        bench, reuse_answer = _resolve_reuse_choice(choice, bench)
        renderer.bench_name = bench

    report = bench_result.data
    assert report is not None  # OK/WARNING always carries an InitReport

    # Calculate elapsed time
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)
    time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    # Inform the user of success
    console.print()
    console.print(
        f"[bold green]✓[/bold green] Successfully initialized bench '{report.bench_name}' in {time_str}"
    )
    console.print(f"[dim]Bench path: {report.bench_path}[/dim]")

    _refresh_cache(project, verbose)

    # Auto-start the bench dev services (default) so init leaves a running dev
    # environment. Containers are already up (stage 1 brought them up), so no
    # port-conflict check is needed and core.start never forks to confirm_start.
    # A start failure does NOT fail init (the bench is created); it degrades to a
    # warning telling the user to run `cwcli start` themselves.
    services_running = False
    web_ready: bool | None = None
    if start_services:
        services_running, web_ready = _start_services(project, report.bench_path)

    web_url = _bench_web_url(project, report.bench_path, report.site_name)

    console.print()
    if services_running and web_ready is not False:
        console.print(f"[bold green]✓[/bold green] Dev services are running for '{project}'.")
        if web_url:
            console.print(f"[dim]Open:    {web_url}  (or `cwcli open {project}`)[/dim]")
        else:
            console.print(f"[dim]Open:    cwcli open {project}[/dim]")
        console.print(f"[dim]Logs:    cwcli logs {project}[/dim]")
        console.print(f"[dim]Stop:    cwcli stop {project}[/dim]")
        console.print(f"[dim]Restart: cwcli restart {project}[/dim]")
        if install_erpnext and web_url:
            console.print(f"[dim]ERPNext is installed at {web_url}.[/dim]")
        elif install_erpnext:
            console.print("[dim]ERPNext is installed.[/dim]")
    else:
        if start_services:
            console.print(f"[dim]Dev services are not running for '{project}'.[/dim]")
        else:
            console.print("[dim]Dev services were not started (--no-start).[/dim]")
        console.print(f"[dim]Start them: cwcli start {project}[/dim]")
        if web_url:
            console.print(f"[dim]Then open {web_url} (or `cwcli open {project}`).[/dim]")
        else:
            console.print(f"[dim]Then run `cwcli open {project}`.[/dim]")
        if install_erpnext and web_url:
            console.print(
                f"[dim]ERPNext is installed; it will be reachable at "
                f"{web_url} once services are running.[/dim]"
            )
        elif install_erpnext:
            console.print("[dim]ERPNext is installed.[/dim]")

    # Show the generated admin password once - and only when a site was actually
    # created this run. On an idempotent re-run bench new-site is skipped
    # (report.site_created is False), so no password was set; printing a fresh
    # one there would be a lie. Never echo a user-SUPPLIED password back.
    if admin_password_generated and report.site_created:
        console.print()
        console.print(
            f"[bold yellow]Administrator password (generated):[/bold yellow] {admin_password}"
        )
        console.print(
            "[dim]Shown once and not stored anywhere. To change it later, run "
            f"`cwcli run {project} --site {report.site_name} set-admin-password "
            "<new-password>`.[/dim]"
        )
