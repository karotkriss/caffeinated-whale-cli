"""Two-faced end-of-run test-summary renderer (test-tooling only, no behaviour change).

One report, two faces, one code path: the same sections rendered from the same
data, with the colour knob on or off.

- **Cockpit** (colour) when stdout is a colour-capable terminal: a header verdict
  line, a highlighted panel for the one-time ``cwcli init`` / bench-build pole,
  a colour-graded slowest-tests table, a per-file rollup with bars, and a phase-
  totals panel. Colour is forced only on *this reporter's own* ``rich.Console``
  (``force_terminal=True``); we NEVER set ``FORCE_COLOR`` globally, because that
  leaks ANSI into the app-under-test's own stdout and breaks string-match
  assertions (the scout proved ~8 breakages that way).
- **Ledger** (plain) otherwise: the identical sections as aligned fixed-width
  columns under ``==== sectioned headers ====``, no boxes and no colour, so raw
  CI logs, pasted snippets, and ``grep`` all see the same clean text.

``build_report`` assembles the data once; ``render`` emits either face. The
caller (``tests/conftest.py``) decides the face from ``terminalreporter``'s own
markup capability and writes the returned block through the reporter.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

SLOWEST_N = 10
PER_FILE_N = 15
PLAIN_WIDTH = 78  # matches the approved Ledger sample; stable + greppable
MAX_WIDTH = 100
BAR_WIDTH = 24


@dataclass
class Report:
    """Everything the summary shows, computed once and rendered by either face."""

    tier: str
    verdict: str  # "PASSED" / "FAILED" / "NO TESTS"
    passed: int
    failed: int
    skipped: int
    deselected: int
    wall: float
    phase_totals: dict[str, float]
    # (display_name, seconds, description) sorted slowest-first.
    slowest: list[tuple[str, float, str]] = field(default_factory=list)
    # (filename, test_count, total_seconds) sorted slowest-first.
    per_file: list[tuple[str, int, float]] = field(default_factory=list)
    init_seconds: float | None = None


def build_report(terminalreporter, config, phase_totals, test_durations, descriptions) -> Report:
    """Assemble a :class:`Report` from the run's collected timing + pytest stats."""
    stats = terminalreporter.stats
    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", [])) + len(stats.get("error", []))
    skipped = len(stats.get("skipped", []))
    deselected = len(stats.get("deselected", []))

    if failed:
        verdict = "FAILED"
    elif passed:
        verdict = "PASSED"
    else:
        verdict = "NO TESTS"

    start = getattr(terminalreporter, "_sessionstarttime", None)
    if start is not None:
        import time

        wall = max(0.0, time.time() - start)
    else:
        wall = sum(phase_totals.values())

    init_seconds = getattr(config, "_cwcli_e2e_init_seconds", None)
    tier = "E2E tier" if init_seconds is not None else "fast (unit) tier"

    slowest = []
    for nodeid, secs in sorted(test_durations.items(), key=lambda kv: kv[1], reverse=True)[
        :SLOWEST_N
    ]:
        name = nodeid.split("::", 1)[1] if "::" in nodeid else nodeid
        slowest.append((name, secs, descriptions.get(nodeid, "")))

    per_file_acc: dict[str, list] = {}
    for nodeid, secs in test_durations.items():
        fname = os.path.basename(nodeid.split("::", 1)[0])
        acc = per_file_acc.setdefault(fname, [0, 0.0])
        acc[0] += 1
        acc[1] += secs
    per_file = sorted(
        ((f, c, t) for f, (c, t) in per_file_acc.items()),
        key=lambda row: row[2],
        reverse=True,
    )[:PER_FILE_N]

    return Report(
        tier=tier,
        verdict=verdict,
        passed=passed,
        failed=failed,
        skipped=skipped,
        deselected=deselected,
        wall=wall,
        phase_totals=dict(phase_totals),
        slowest=slowest,
        per_file=per_file,
        init_seconds=init_seconds,
    )


def render(report: Report, *, color: bool, width: int | None = None) -> str:
    """Render the whole summary block. ``color`` picks Cockpit vs Ledger."""
    if color:
        return _render_cockpit(report, min(width or MAX_WIDTH, MAX_WIDTH))
    return _render_ledger(report)


# --------------------------------------------------------------------------- #
# Cockpit (colour)
# --------------------------------------------------------------------------- #
def _time_text(secs: float) -> Text:
    if secs >= 1.0:
        style = "bold red"
    elif secs >= 0.5:
        style = "yellow"
    elif secs >= 0.1:
        style = "green"
    else:
        style = "dim"
    return Text(f"{secs:.2f}s", style=style)


def _render_cockpit(report: Report, width: int) -> str:
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        width=width,
        highlight=False,
        emoji=False,
    )

    # Header verdict line.
    console.print(Rule(style="cyan"))
    header = Table.grid(expand=True)
    header.add_column(justify="left")
    header.add_column(justify="right")
    v_style = "bold green" if report.verdict == "PASSED" else "bold red"
    left = Text.assemble(("cwcli", "bold cyan"), " test suite  ", (report.tier, "dim"))
    right = Text.assemble((report.verdict, v_style), (f"  {report.passed} passed", "green"))
    if report.failed:
        right.append(f"  {report.failed} failed", style="red")
    if report.skipped:
        right.append(f"  {report.skipped} skipped", style="yellow")
    if report.deselected:
        right.append(f"  {report.deselected} deselected", style="dim")
    right.append(f"  {report.wall:.2f}s", style="dim")
    header.add_row(left, right)
    console.print(header)

    # One-time E2E init pole - highlighted, and flagged as NOT per-test time.
    if report.init_seconds is not None:
        body = Text()
        body.append(f"{report.init_seconds:.0f}s", style="bold yellow")
        body.append("  one-time ")
        body.append("cwcli init", style="bold")
        body.append(" + bench build\n")
        body.append("This one session build dominates the E2E matrix - it is ")
        body.append("NOT", style="bold")
        body.append(" per-test time.")
        console.print(
            Panel(
                body,
                title="One-time E2E cost",
                title_align="left",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    # Slowest tests.
    slow = Table(
        title="Slowest tests",
        box=box.SIMPLE_HEAD,
        header_style="bold",
        expand=True,
        pad_edge=False,
    )
    slow.add_column("time", justify="right", no_wrap=True, width=7)
    slow.add_column("test", justify="left", no_wrap=True, overflow="ellipsis", ratio=3)
    slow.add_column("description", justify="left", style="italic dim", ratio=4)
    for name, secs, desc in report.slowest:
        slow.add_row(_time_text(secs), name, desc)
    console.print(slow)

    # Per-file rollup with bars.
    rollup = Table(
        title="Per-file rollup",
        box=box.SIMPLE_HEAD,
        header_style="bold",
        expand=True,
        pad_edge=False,
    )
    rollup.add_column("file", justify="left", no_wrap=True, overflow="ellipsis", ratio=1)
    rollup.add_column("tests", justify="right", width=5)
    rollup.add_column("time", justify="right", width=7)
    rollup.add_column("", justify="left", no_wrap=True)
    max_secs = max((secs for _, _, secs in report.per_file), default=0.0) or 1.0
    for fname, count, secs in report.per_file:
        bar_len = max(1, round(BAR_WIDTH * secs / max_secs))
        rollup.add_row(fname, str(count), f"{secs:.2f}s", Text("█" * bar_len, style="magenta"))
    console.print(rollup)

    # Phase totals footer.
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="right")
    grid.add_row("fixture setup", f"{report.phase_totals.get('setup', 0.0):.2f}s")
    grid.add_row("test call", f"{report.phase_totals.get('call', 0.0):.2f}s")
    grid.add_row("teardown", f"{report.phase_totals.get('teardown', 0.0):.2f}s")
    console.print(
        Panel(grid, title="Phase totals", title_align="left", border_style="dim", expand=False)
    )

    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Ledger (plain)
# --------------------------------------------------------------------------- #
_TIME_W = 7
_TEST_W = 40
_COUNT_W = 5
_FILE_W = 30


def _sep(title: str) -> str:
    return f" {title} ".center(PLAIN_WIDTH, "=")


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _render_ledger(report: Report) -> str:
    out: list[str] = []
    w = out.append

    w(_sep(f"cwcli test suite - {report.tier}"))
    line = f"  result   : {report.verdict}   {report.passed} passed"
    if report.failed:
        line += f"  {report.failed} failed"
    if report.skipped:
        line += f"  {report.skipped} skipped"
    if report.deselected:
        line += f"  {report.deselected} deselected"
    w(line)
    w(f"  wall time: {report.wall:7.2f}s")
    w("")

    if report.init_seconds is not None:
        w(_sep("one-time E2E cost (NOT per-test time)"))
        w(f"  cwcli init + bench build : {report.init_seconds:7.2f}s")
        w("  ^ one session build dominates the E2E matrix; it is not per-test time.")
        w("")

    w(_sep("slowest tests"))
    w(f"  {'time':>{_TIME_W}}  {'test':<{_TEST_W}}  description")
    desc_w = PLAIN_WIDTH - (2 + _TIME_W + 2 + _TEST_W + 2)
    w(f"  {'-' * _TIME_W}  {'-' * _TEST_W}  {'-' * desc_w}")
    for name, secs, desc in report.slowest:
        tstr = f"{secs:.2f}s"
        w(f"  {tstr:>{_TIME_W}}  {_trunc(name, _TEST_W):<{_TEST_W}}  {_trunc(desc, desc_w)}")
    w("")

    w(_sep("per-file rollup (by total time)"))
    w(f"  {'time':>{_TIME_W}}  {'tests':>{_COUNT_W}}  file")
    w(f"  {'-' * _TIME_W}  {'-' * _COUNT_W}  {'-' * _FILE_W}")
    for fname, count, secs in report.per_file:
        tstr = f"{secs:.2f}s"
        w(f"  {tstr:>{_TIME_W}}  {count:>{_COUNT_W}}  {fname}")
    w("")

    w(_sep("phase totals"))
    w(f"  fixture setup : {report.phase_totals.get('setup', 0.0):7.2f}s")
    w(f"  test call     : {report.phase_totals.get('call', 0.0):7.2f}s")
    w(f"  teardown      : {report.phase_totals.get('teardown', 0.0):7.2f}s")
    w("=" * PLAIN_WIDTH)

    return "\n".join(out) + "\n"


def _demo() -> None:
    """Self-check: both faces render, plain has no ANSI, colour does, and the
    E2E pole shows only when its duration is present."""
    r = Report(
        tier="fast (unit) tier",
        verdict="PASSED",
        passed=570,
        failed=0,
        skipped=0,
        deselected=30,
        wall=9.02,
        phase_totals={"setup": 0.12, "call": 9.02, "teardown": 0.04},
        slowest=[
            ("TestCache::test_cache_expires_after_ttl", 2.10, "Re-queries Docker after the TTL."),
            ("test_reworded_name_only", 0.71, "reworded name only"),
        ],
        per_file=[("test_apps.py", 30, 4.56), ("test_tips.py", 24, 0.46)],
    )
    plain = render(r, color=False)
    colour = render(r, color=True, width=100)
    assert "\x1b[" not in plain, "plain face must carry no ANSI"
    assert "\x1b[" in colour, "colour face must carry ANSI"
    assert "slowest tests" in plain and "reworded name only" in plain
    assert "one-time E2E cost" not in plain, "no init pole without a recorded duration"

    r.init_seconds = 84.0
    assert "one-time E2E cost" in render(r, color=False)
    print(render(r, color=True, width=100))
    print(render(r, color=False))
    print("ok")


if __name__ == "__main__":
    _demo()
