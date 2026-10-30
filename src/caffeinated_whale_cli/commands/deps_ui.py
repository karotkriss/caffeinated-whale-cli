"""The human renderer for ``core.app_deps``: the plan, its two questions, its refusal.

Shared by ``apps install``/``update``/``plan``, ``new-site`` and ``new-bench`` so the
dependency check reads and asks the same way everywhere. Both modes, per the
project's standard: a TTY shows the ordered plan and asks once; a non-TTY without the
flag refuses with exit 1 and names the flag, never proceeding or hanging.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import questionary
import typer

from ..core import app_deps
from ..core.envelope import Result, Status
from ..core.errors import CwcliError
from ..utils.console import stderr_console

WITH_DEPS_HELP = (
    "Consent to fetch and install the apps this one requires (required_apps), "
    "dependencies first, on the same sites. Without it a terminal asks; a script refuses."
)
DEP_SOURCE_HELP = (
    "APP=URL: the repository to fetch a required app from when the apps catalog holds "
    "several with that name. Repeatable."
)
IGNORE_RANGE_HELP = (
    "Install although the app's declared Frappe range ([tool.bench.frappe-dependencies]) "
    "does not fit this bench. Use only when the declared range is wrong; a sibling app's "
    "range is never overridden."
)

DEP_KINDS = frozenset({"confirm_required_apps", "select_app_source"})


def parse_dep_sources(values: list[str] | None) -> dict[str, str]:
    """``--dep-source app=url`` values as a dict; a malformed one is a usage error."""
    found: dict[str, str] = {}
    for value in values or []:
        app, sep, url = str(value).partition("=")
        if not sep or not app.strip() or not url.strip():
            stderr_console.print(
                f"[bold red]Error:[/bold red] --dep-source takes APP=URL, got '{value}'."
            )
            raise typer.Exit(code=2)
        found[app.strip()] = url.strip()
    return found


def _mark(ok: Any) -> str:
    return "[green]✓[/green]" if ok is True else "[red]✗[/red]" if ok is False else ""


def render_rows(rows: list[dict], *, frappe: str | None = None, order: str | None = None) -> None:
    """The plan as the ordered list the board specifies, to stderr."""
    head = "Compatibility and required apps"
    stderr_console.print(
        f"[bold]{head}[/bold]" + (f" [dim](bench runs Frappe {frappe})[/dim]" if frappe else "")
    )
    for row in rows:
        indent = "  └ " if row.get("required_by") else "  "
        rng = (
            f"frappe {row['frappe_range']} {_mark(row.get('range_ok'))}"
            if row.get("frappe_range")
            else ""
        )
        where = f"{row.get('source') or ''}" + (f" @ {row['ref']}" if row.get("ref") else "")
        why = f"[dim]required by {row['required_by']}[/dim]" if row.get("required_by") else ""
        stderr_console.print(
            f"{indent}[cyan]{row['app']}[/cyan]  [bold]{row['action']}[/bold]  {where}  {rng}  {why}".rstrip()
        )
    if order:
        stderr_console.print(f"  [dim]{order[0].upper() + order[1:]}[/dim]")


def render_plan(plan: app_deps.DependencyPlan) -> None:
    render_rows(app_deps.plan_rows(plan), frappe=plan.frappe_version, order=app_deps.describe(plan))
    for problem in plan.problems:
        color = {"blocker": "red", "consent": "cyan", "choice": "yellow"}.get(
            problem.severity, "dim"
        )
        stderr_console.print(f"  [{color}]{problem.severity}[/{color}] {problem.message}")
        if problem.remedy and problem.severity in ("blocker", "choice"):
            stderr_console.print(f"    [dim]{problem.remedy}[/dim]")


def render_error(error: CwcliError) -> None:
    """A refusal: the plan it carries (when it carries one), then the message."""
    plan = (error.detail or {}).get("plan")
    if isinstance(plan, dict) and plan.get("nodes"):
        rows = [
            {
                "app": n["app"],
                "action": n["action"],
                "required_by": n.get("parent") or "",
                "source": n.get("source") or n.get("target"),
                "ref": n.get("ref") or "",
                "frappe_range": next(
                    (r["spec"] for r in n.get("ranges") or [] if r["dep"] == "frappe"), ""
                ),
                "range_ok": next(
                    (r["ok"] for r in n.get("ranges") or [] if r["dep"] == "frappe"), None
                ),
            }
            for n in plan["nodes"]
        ]
        render_rows(rows, frappe=plan.get("frappe_version"))
    stderr_console.print(f"[bold red]Error:[/bold red] {error.message}")
    if error.hint:
        stderr_console.print(f"[dim]{error.hint}[/dim]")


def answer(result: Result, *, interactive: bool) -> dict[str, Any]:
    """The answer to a dependency question, as the core parameters to re-invoke with.

    A TTY shows the plan and asks once; anything else refuses with exit 1 naming the
    flag that answers it.
    """
    choice = result.choice
    assert choice is not None
    if choice.kind == "confirm_required_apps":
        rows = [o for o in choice.options or [] if "action" in o]
        if not interactive:
            names = ", ".join(
                r["app"] for r in rows if r.get("required_by") and r["action"] != "none"
            )
            stderr_console.print(
                f"[bold red]Error:[/bold red] {choice.prompt.split(' Fetch and install')[0]} "
                "Nothing was fetched or installed."
            )
            stderr_console.print(
                f"[dim]Re-run with --with-deps to fetch and install {names or 'them'} first, "
                "or fetch each yourself with 'cwcli apps install <project> <repository URL> "
                "--fetch-only'.[/dim]"
            )
            raise typer.Exit(code=1)
        render_rows(
            rows,
            order=choice.prompt.rsplit("(", 1)[-1].rstrip(")") if "(" in choice.prompt else None,
        )
        try:
            ok = questionary.confirm(choice.prompt.split(" (")[0], default=False).ask()
        except (KeyboardInterrupt, EOFError):
            ok = False
        if not ok:
            stderr_console.print("[yellow]Nothing was fetched or installed.[/yellow]")
            raise typer.Exit(code=1)
        return {"with_deps": True}
    if choice.kind == "select_app_source":
        options = choice.options or []
        app = options[0].get("app") if options else "the app"
        if not interactive:
            stderr_console.print(f"[bold red]Error:[/bold red] {choice.prompt}")
            for o in options:
                stderr_console.print(f"  [dim]{o['value']}  ({o['label']})[/dim]")
            stderr_console.print(f"[dim]Re-run with --dep-source {app}=<url>.[/dim]")
            raise typer.Exit(code=1)
        try:
            picked = questionary.select(
                choice.prompt,
                choices=[
                    questionary.Choice(
                        f"{o['label']}"
                        + ("" if o.get("ok") else "  (its Frappe range does not fit)"),
                        value=o["value"],
                    )
                    for o in options
                ],
                default=next((o["value"] for o in options if o["value"] == choice.default), None),
            ).ask()
        except (KeyboardInterrupt, EOFError):
            picked = None
        if not picked:
            stderr_console.print("[yellow]Nothing was fetched or installed.[/yellow]")
            raise typer.Exit(code=1)
        return {"dep_sources": {app: picked}}
    raise AssertionError(choice.kind)


def run_with_answers(
    call: Callable[..., Result],
    *,
    with_deps: bool,
    dep_sources: dict[str, str],
    interactive: bool,
) -> Result:
    """Call ``call(with_deps=..., dep_sources=...)`` until no dependency question is
    left: each is answered once (a TTY) or refused (anything else)."""
    kwargs: dict[str, Any] = {"with_deps": with_deps, "dep_sources": dict(dep_sources)}
    for _ in range(10):  # each answer settles one question; ten is beyond any real plan
        result = call(**kwargs)
        if result.status is not Status.NEEDS_CHOICE or result.choice is None:
            return result
        if result.choice.kind not in DEP_KINDS:
            return result
        got = answer(result, interactive=interactive)
        if "dep_sources" in got:
            kwargs["dep_sources"] = {**kwargs["dep_sources"], **got["dep_sources"]}
        else:
            kwargs.update(got)
    return result


def interactive(json_output: bool = False) -> bool:
    return not json_output and sys.stdin.isatty()
