import importlib.metadata

import click
import typer

from .commands import apps as apps_cmd
from .commands import axi as axi_cmd
from .commands import config as config_cmd
from .commands import list as list_cmd
from .commands import restart as restart_cmd
from .commands import rm as rm_cmd
from .commands import start as start_cmd
from .commands import stop as stop_cmd
from .commands.backup import backup as _backup_cmd
from .commands.doctor import doctor as _doctor_cmd
from .commands.init import init as _init_cmd
from .commands.inspect import inspect as inspect_cmd_func
from .commands.label import label as _label_cmd
from .commands.logs import logs as _logs_cmd
from .commands.open import open_bench as _open_cmd
from .commands.restore import restore as _restore_cmd
from .commands.rm_site import rm_site as _rm_site_cmd
from .commands.run import run as _run_cmd
from .commands.scale import scale as _scale_cmd
from .commands.self_update import self_update as _self_update_cmd
from .commands.status import status as _status_cmd
from .commands.unlock import unlock as _unlock_cmd
from .commands.update import update as _update_cmd
from .commands.where import where as _where_cmd

__version__ = importlib.metadata.version("caffeinated-whale-cli")


_READ_ONLY_DOCTOR = "cwcli.read_only_doctor"


class RootGroup(axi_cmd.ToonGroup):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta[_READ_ONLY_DOCTOR] = args[:1] == ["doctor"] or args[:2] == [
            "axi",
            "doctor",
        ]
        return super().parse_args(ctx, args)


app = typer.Typer(
    # The axi-aware group so `cwcli axi <verb>` parse failures (mounted under this
    # top-level command) render as TOON on stdout; non-axi commands keep Typer's
    # default rich rendering. See commands/axi.py:ToonGroup.
    cls=RootGroup,
    help="""
    A command-line tool to help you create, manage, and back up
    your Frappe and ERPNext Docker instances.
    """,
    rich_markup_mode="markdown",
)


def _build_suffix() -> str:
    """The parenthetical that says WHICH BUILD this is, not just which release.

    A bare version number cannot tell a published artifact from a working-tree
    build, so a probe of a released cwcli once concluded a feature did not exist
    when it was merged and sitting at the tip of ``develop``. The release form
    stays clean (``(release build)``, no git, no paths); a tree build names its
    commit and whether the tree was dirty. The ``Caffeinated Whale CLI Version:
    <pep440>`` prefix is unchanged, so anything already parsing the version out
    of this line keeps working. Fail-open: never let ``--version`` crash.
    """
    try:
        from .core.version import build_info

        build = build_info()
        if build.source == "release":
            return "(release build)"
        parts = ["editable source build" if build.editable else "source build"]
        parts.append(f"git {build.commit}" if build.commit else "git unknown")
        if build.dirty:
            parts.append("dirty")
        return f"({', '.join(parts)})"
    except Exception:
        return "(build unknown)"


def version_callback(value: bool):
    if value:
        print(f"Caffeinated Whale CLI Version: {__version__} {_build_suffix()}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the application's version and exit.",
    ),
):
    # Initialize context object
    ctx.ensure_object(dict)

    # Doctor is exempt because its strict read-only contract forbids even a
    # detached refresh of the version cache.
    if not ctx.meta.get(_READ_ONLY_DOCTOR):
        from .update_notice import notify_if_outdated

        notify_if_outdated()


app.command("init")(_init_cmd)
app.command("inspect")(inspect_cmd_func)
app.command("label")(_label_cmd)

app.add_typer(list_cmd.app, name="ls")
app.add_typer(start_cmd.app, name="start")
app.add_typer(stop_cmd.app, name="stop")
app.add_typer(restart_cmd.app, name="restart")
app.add_typer(rm_cmd.app, name="rm")
app.command("rm-site")(_rm_site_cmd)
app.add_typer(config_cmd.app, name="config")
app.add_typer(apps_cmd.app, name="apps")
app.add_typer(axi_cmd.app, name="axi")

app.command("where")(_where_cmd)

# `run` passes its argv through to bench, so an option cwcli does not define
# (`--branch`, `--force`, ...) belongs to bench and must reach it as an argument
# rather than exiting 2. Options cwcli DOES define stay cwcli's wherever they
# appear, keeping `cwcli run p migrate --bench staging` working; `--` remains the
# escape hatch for a bench flag that collides with one of cwcli's own names.
app.command("run", context_settings={"ignore_unknown_options": True})(_run_cmd)
app.command("update")(_update_cmd)
app.command("self-update")(_self_update_cmd)
app.command("status")(_status_cmd)
app.command("open")(_open_cmd)
app.command("logs")(_logs_cmd)
app.command("unlock")(_unlock_cmd)
app.command("scale")(_scale_cmd)
app.command("restore")(_restore_cmd)
app.command("backup")(_backup_cmd)
app.command("doctor")(_doctor_cmd)


def cli():
    """
    The main entry point function for the CLI application.
    This is what `pyproject.toml` calls.
    """
    app()


if __name__ == "__main__":
    cli()
